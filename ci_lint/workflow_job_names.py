"""Source-derived hosted names for native callers; no supplied alias tables.

Qualified expansion owns the caller/input/matrix graph. This projection only
renders its GitHub display names and rejects ambiguous or unsupported naming.
"""

import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.workflow_gate import _outputs, _source
from ci_lint.workflow_replay_conditions import condition_excludes
from ci_lint.workflow_replay_dependencies import _needs
from ci_lint.workflow_replay_expansion import ExpandedJob, _callee_path, _document, expand_selection
from ci_lint.workflow_replay_inputs import BoundInput, bind_call_inputs, bound_name
from ci_lint.workflow_replay_outputs import ReplayOutput
from ci_lint.workflow_scan import ParsedYamlFile, jobs_of, load_workflows
from ci_lint.yaml_io import YamlValue


def _automatic_axes(raw: YamlValue, matrix: dict[str, YamlValue]) -> tuple[str, ...]:
    """Recover display order from source, never canonical identity JSON's key order."""
    if isinstance(raw, dict) and set(raw) == set(matrix):
        return tuple(raw)
    if isinstance(raw, dict) and set(raw) == {"include"}:
        rows = raw["include"]
        if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows):
            first = rows[0]
            if isinstance(first, dict) and all(tuple(row) == tuple(first) for row in rows):
                if set(first) == set(matrix) and matrix in rows:
                    return tuple(first)
    raise ValueError("automatic matrix display name is not statically proven")


def _component(job_id: str, job: dict[str, YamlValue], inputs: tuple[BoundInput, ...],
               matrix: YamlValue) -> str:
    template = job.get("name", job_id)
    name = bound_name(template, inputs, matrix)
    if not isinstance(matrix, dict) or not matrix:
        return name
    if isinstance(template, str) and re.search(r"\$\{\{\s*matrix\.", template):
        return name
    strategy = job.get("strategy")
    axes = strategy.get("matrix") if isinstance(strategy, dict) else None
    values = [matrix[axis] for axis in _automatic_axes(axes, matrix)]
    if any(not isinstance(value, (str, bool, int)) for value in values):
        raise ValueError("automatic matrix display name has unsupported values")
    return name + " (" + ", ".join(str(value).lower() if isinstance(value, bool) else str(value)
                                   for value in values) + ")"


def _display(files: tuple[ParsedYamlFile, ...], path: str, leaf: ExpandedJob) -> str:
    inputs: tuple[BoundInput, ...] = ()
    components: list[str] = []
    for index, part in enumerate(leaf.identity):
        document = _document(files, path)
        job = jobs_of(document)[part.job_id]
        matrix = json.loads(part.matrix)
        components.append(_component(part.job_id, job, inputs, matrix))
        if index < len(leaf.identity) - 1:
            path = _callee_path(job.get("uses"))
            inputs = bind_call_inputs(_document(files, path), job, inputs, matrix)
    return " / ".join(components)


@dataclass(frozen=True)
class HostedCallerNames:
    job_id: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowJobNames:
    files: tuple[ParsedYamlFile, ...]
    path: str
    outputs: tuple[ReplayOutput, ...]

    @classmethod
    def from_gate(cls, repo: Path, workflow: str, gate: str, needs: dict[str, JsonValue]) -> "WorkflowJobNames":
        document = _source(repo, workflow, gate)
        return cls(tuple(load_workflows(repo)), f".github/workflows/{workflow}",
                   _outputs(needs, jobs_of(document)))

    @cached_property
    def _callers(self) -> tuple[HostedCallerNames, ...]:
        expansion = expand_selection(self.files, self.path, None, qualified=True,
                                     outputs=self.outputs, event="pull_request")
        if expansion.problem:
            raise ValueError(expansion.problem)
        names: dict[str, list[str]] = {}
        seen: set[str] = set()
        for leaf in expansion.jobs:
            if not isinstance(leaf.job, dict):
                raise ValueError("hosted leaf has no source job")
            if condition_excludes(leaf.job.get("if"), leaf.inputs, set(), successful=True,
                                  event="pull_request", outputs=leaf.outputs, dependencies=_needs(leaf.job),
                                  scope=leaf.identity[:-1]):
                continue
            name = _display(self.files, self.path, leaf)
            if name in seen:
                raise ValueError("hosted workflow has ambiguous display names")
            seen.add(name)
            names.setdefault(leaf.identity[0].job_id, []).append(name)
        return tuple(HostedCallerNames(job, tuple(values)) for job, values in names.items())

    def for_job(self, job_id: str) -> tuple[str, ...]:
        names = next((caller.names for caller in self._callers if caller.job_id == job_id), ())
        if not names:
            raise ValueError("native caller has no proving PR leaf")
        return names
