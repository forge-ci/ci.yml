"""Resolve literal local reusable calls into concrete execution identities."""

import re
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ci_lint.workflow_replay_dependencies import _needs
from ci_lint.workflow_replay_conditions import condition_excludes, condition_boolean
from ci_lint.workflow_replay_inputs import BoundInput, bind_call_inputs, bound_name
from ci_lint.workflow_replay_identity import JobIdentity, identity_part, bounded_identity
from ci_lint.workflow_replay_matrix import job_matrices
from ci_lint.workflow_replay_outputs import ReplayOutput, PendingOutput, valid_output_name
from ci_lint.workflow_scan import ParsedYamlFile, get_on_section, jobs_of
from ci_lint.yaml_io import YamlValue


@dataclass(frozen=True)
class ExpandedJob:
    source_job: str
    key: str
    path: str
    document: YamlValue
    job: YamlValue
    inputs: tuple[BoundInput, ...] = ()
    identity: tuple[JobIdentity, ...] = ()
    outputs: tuple[ReplayOutput, ...] = ()


OutputProver = Callable[[ExpandedJob], tuple[ReplayOutput, ...]]


@dataclass(frozen=True)
class ReplayExpansion:
    jobs: tuple[ExpandedJob, ...]
    problem: str | None = None
    pending: bool = False
    outputs: tuple[ReplayOutput, ...] = ()


def _literal_name(value: YamlValue) -> str:
    if not isinstance(value, str) or not value.strip() or "${{" in value:
        raise ValueError("execution identity has a nonliteral workflow or job name")
    return value


def _callee_path(value: YamlValue) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\./\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", value) is None:
        raise ValueError("reusable workflow is not a literal repository-local path")
    return value[2:]


def _document(files: tuple[ParsedYamlFile, ...], path: str) -> dict[str, YamlValue]:
    matching = [item for item in files if item.path == path]
    if len(matching) != 1 or not isinstance(matching[0].document, dict):
        raise ValueError(f"workflow {path} is missing, ambiguous or unparsed")
    return matching[0].document


def _resolve_job(files: tuple[ParsedYamlFile, ...], path: str, job_id: str, prefix: str,
                 inputs: tuple[BoundInput, ...], callers: tuple[JobIdentity, ...] | None = None,
                 matrix: str = "null") -> ExpandedJob:
    document = _document(files, path)
    job = jobs_of(document).get(job_id)
    if job is None:
        raise ValueError(f"dependency job {path}:{job_id} does not exist")
    if "strategy" in job and callers is None:
        raise ValueError("matrix expansion is not statically proven")
    basename = path.rsplit("/", 1)[-1]
    key = (prefix + basename + "/" + job_id if callers is not None else
           prefix + _literal_name(document.get("name", basename)) + "/" + bound_name(job.get("name", job_id), inputs))
    identity = bounded_identity(callers + (identity_part(job_id, json.loads(matrix)),)) if callers is not None else ()
    return ExpandedJob(f"{basename}:{job_id}", key, path, document, job, inputs, identity)


@dataclass(frozen=True)
class _ExpansionState:
    files: tuple[ParsedYamlFile, ...]
    jobs: list[ExpandedJob]
    visited: set[str]
    active: set[str]
    caller_prefixes: set[str]
    qualified: bool = False
    outputs: tuple[ReplayOutput, ...] = ()
    mapped_outputs: list[ReplayOutput] = field(default_factory=list)
    prove_outputs: OutputProver | None = None
    event: str | None = None


def _record(state: _ExpansionState, resolved: ExpandedJob) -> None:
    resolved = replace(resolved, outputs=state.outputs + tuple(state.mapped_outputs))
    if state.qualified:
        if not any(job.identity == resolved.identity for job in state.jobs):
            if len(state.jobs) >= 512:
                raise ValueError("reusable expansion exceeds the bounded graph limit")
            if state.prove_outputs is not None:
                state.mapped_outputs.extend(state.prove_outputs(resolved))
            state.jobs.append(resolved)
        return
    previous = next((job for job in state.jobs if job.source_job == resolved.source_job), None)
    if previous is not None:
        if previous.key != resolved.key:
            raise ValueError(f"reusable job {resolved.source_job} has multiple execution identities")
        return
    state.jobs.append(resolved)


def _begin(state: _ExpansionState, identity: str) -> bool:
    if identity in state.active:
        raise ValueError("dependency graph contains a cycle")
    if identity in state.visited:
        return False
    if len(state.visited) + len(state.active) >= 512:
        raise ValueError("reusable expansion exceeds the bounded graph limit")
    state.active.add(identity)
    return True


def _matrices(state: _ExpansionState, job: dict[str, YamlValue], inputs: tuple[BoundInput, ...],
              callers: tuple[JobIdentity, ...]) -> tuple[str, ...]:
    if not state.qualified:
        return ("null",)
    outputs = state.outputs + tuple(state.mapped_outputs)
    # GitHub evaluates a concrete job's condition before matrix expansion.
    # A bound false condition retains one explicit skipped job, never zero jobs.
    if "uses" not in job and condition_excludes(job.get("if"), inputs, set(), event=state.event,
                                                outputs=outputs, dependencies=_needs(job), scope=callers):
        return ("null",)
    return job_matrices(job, outputs=outputs, scope=callers)


def _visit(state: _ExpansionState, current: str, job_id: str,
           prefix: str, ancestry: tuple[str, ...], inputs: tuple[BoundInput, ...] = (),
           callers: tuple[JobIdentity, ...] = ()) -> None:
    identity = f"{repr(callers) if state.qualified else prefix}|{current}:{job_id}"
    if not _begin(state, identity):
        return
    if len(ancestry) > 8:
        raise ValueError("reusable expansion exceeds the bounded graph limit")
    job = jobs_of(_document(state.files, current)).get(job_id)
    if job is None:
        raise ValueError(f"dependency job {current}:{job_id} does not exist")
    for dependency in _needs(job):
        _visit(state, current, dependency, prefix, ancestry, inputs, callers)
    for matrix in _matrices(state, job, inputs, callers):
        resolved = _resolve_job(state.files, current, job_id, prefix, inputs,
                                callers if state.qualified else None, matrix)
        if "uses" in job:
            _called(state, current, job, job_id, prefix, ancestry, inputs, resolved.identity)
        else:
            _record(state, resolved)
    state.active.remove(identity)
    state.visited.add(identity)



def _mapped_outputs(document: dict[str, YamlValue], callers: tuple[JobIdentity, ...],
                    outputs: tuple[ReplayOutput, ...]) -> tuple[ReplayOutput, ...]:
    contract = get_on_section(document).get("workflow_call")
    declared = contract.get("outputs", {}) if isinstance(contract, dict) else {}
    if not isinstance(declared, dict) or len(declared) > 256:
        raise ValueError("called workflow outputs are not a bounded declaration")
    mapped: list[ReplayOutput] = []
    for name, definition in declared.items():
        if not isinstance(name, str) or not valid_output_name(name) or not isinstance(definition, dict):
            raise ValueError("called workflow output lacks a source mapping")
        expression = definition.get("value")
        match = re.fullmatch(r"\s*\$\{\{\s*jobs\.([A-Za-z_][A-Za-z0-9_-]*)"
                             r"\.outputs\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}\s*", expression if isinstance(expression, str) else "")
        if match is None:
            raise ValueError("called workflow output mapping is not statically proven")
        job = jobs_of(document).get(match[1])
        job_outputs = job.get("outputs") if job is not None else None
        if job is None or ("uses" not in job and (not isinstance(job_outputs, dict) or match[2] not in job_outputs)):
            raise ValueError("called workflow output references an undeclared job output")
        candidates = [item for item in outputs if item.identity and item.identity[:-1] == callers
                      and item.identity[-1].job_id == match[1] and item.name == match[2]]
        if not candidates:
            continue  # Unrequested public outputs are not execution evidence.
        if len(candidates) != 1 or candidates[0].identity[-1].matrix != "null":
            raise ValueError("called workflow output has ambiguous producer identity")
        item = candidates[0]
        mapped.append(replace(item, identity=callers, name=name, origin=item.origin or item.identity))
    return tuple(mapped)


def _called(state: _ExpansionState, current: str, job: dict[str, YamlValue],
            job_id: str, prefix: str, ancestry: tuple[str, ...], inputs: tuple[BoundInput, ...],
            callers: tuple[JobIdentity, ...] = ()) -> None:
    callee = _callee_path(job.get("uses"))
    if callee in ancestry or callee == current:
        raise ValueError("reusable workflow graph contains a cycle")
    document = _document(state.files, callee)
    jobs = jobs_of(document)
    if "workflow_call" not in get_on_section(document) or not jobs:
        raise ValueError("called workflow has no workflow_call contract or executable jobs")
    bound = bind_call_inputs(
        document, job, inputs, json.loads(callers[-1].matrix) if callers else None,
        boolean_value=lambda expression: condition_boolean(expression, inputs, event=state.event))
    caller_name = job_id if state.qualified else bound_name(job.get("name", job_id), inputs)
    caller_prefix = prefix + caller_name + "/"
    if not state.qualified and caller_prefix in state.caller_prefixes:
        raise ValueError("reusable callers have an ambiguous display-name prefix")
    state.caller_prefixes.add(caller_prefix)
    for child in jobs:
        _visit(state, callee, child, caller_prefix, ancestry + (current,), bound, callers)
    if state.qualified and (state.outputs or state.mapped_outputs):
        state.mapped_outputs.extend(_mapped_outputs(document, callers, state.outputs + tuple(state.mapped_outputs)))


def expand_selection(files: tuple[ParsedYamlFile, ...], path: str, selected: str | None, *,
                     qualified: bool = False, inputs: tuple[BoundInput, ...] = (),
                     outputs: tuple[ReplayOutput, ...] = (),
                     prove_outputs: OutputProver | None = None, event: str | None = None) -> ReplayExpansion:
    """Exclude virtual callers; require all their concrete jobs and prerequisites.

    Qualified mode derives caller IDs and every literal matrix leg from the
    source, optionally resolving matrices from previously proved outputs.
    Legacy display-name mode cannot disambiguate repeated callers.
    """
    state = _ExpansionState(files, [], set(), set(), set(), qualified, outputs,
                            prove_outputs=prove_outputs, event=event)
    try:
        roots = (selected,) if selected is not None else tuple(jobs_of(_document(files, path)))
        if not roots:
            raise ValueError("workflow has no executable jobs")
        for root in roots:
            _visit(state, path, root, "", (), inputs)
    except PendingOutput as exc:
        return ReplayExpansion((), str(exc), pending=True)
    except ValueError as exc:
        return ReplayExpansion((), str(exc))
    return ReplayExpansion(tuple(sorted(state.jobs, key=lambda job: job.source_job)),
                           outputs=state.outputs + tuple(state.mapped_outputs))
