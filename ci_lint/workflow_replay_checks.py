"""One source-derived check contract shared by static and execution proof."""

import re

from ci_lint.workflow_replay import ReplayJob
from ci_lint.workflow_replay_conditions import condition_excludes
from ci_lint.workflow_replay_dependencies import _needs
from ci_lint.workflow_replay_cache_save import approved_cache_save, approved_pr_cache_save
from ci_lint.workflow_replay_expansion import ExpandedJob
from ci_lint.workflow_replay_input_skips import excluded_input_steps
from ci_lint.workflow_replay_inputs import bound_name
from ci_lint.workflow_replay_minimal_skip import classify_minimal_skip
from ci_lint.workflow_scan import as_dict, steps_of
from ci_lint.workflow_replay_maintenance import source_maintenance
from ci_lint.yaml_io import YamlValue

# `steps.<id>.outputs.<name>` reads in a step's `if:`: the value is decided
# at run time by whatever produced it (a pass-marker lookup, a cache restore),
# so neither bound inputs nor prior exclusions can prove the guard either way.
_DYNAMIC_OUTPUT_RE = re.compile(r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.outputs\.")


def conditional_step_names(job: dict[str, YamlValue], skip_names: set[str]) -> tuple[str, ...]:
    """#415: source steps whose guard reads a runtime-dynamic step output of
    a step that RUNS (e.g. `steps.pass.outputs.cache-hit != 'true'` where
    `pass` is an unconditional marker lookup). The step may skip by design
    or execute on a miss, so the execution proof accepts exactly one Main
    section either way instead of demanding a success a marker hit can
    never produce. A producer that is itself excluded lives in `skip_names`
    and stays the finite-exclusion class's business, not this one."""

    steps = steps_of(job)
    named = {str(step["name"]) for step in steps if isinstance(step.get("name"), str)}
    ids = {step["id"] for step in steps if isinstance(step.get("id"), str)}
    skipped_ids = {
        step["id"] for step in steps
        if isinstance(step.get("id"), str) and str(step.get("name")) in skip_names
    }
    running = ids - skipped_ids
    names: list[str] = []
    for step in steps:
        name = step.get("name")
        if not isinstance(name, str) or "${{" in name or name not in named or name in skip_names:
            continue
        match = _DYNAMIC_OUTPUT_RE.search(str(step.get("if") or ""))
        if match is not None and match.group(1) in running:
            names.append(name)
    return tuple(names)


def _named_job(expanded: ExpandedJob) -> dict[str, YamlValue]:
    job = as_dict(expanded.job)
    steps: list[YamlValue] = []
    for step in steps_of(job):
        if "uses" not in step and "run" not in step:
            continue
        steps.append({**step, "name": bound_name(step.get("name"), expanded.inputs)})
    return {**job, "steps": steps}


def derive_checks(expanded: ExpandedJob, *, mode: str, event: str) -> ReplayJob:
    """Require all source steps, with only existing finite skip classifications.

    Every exclusion still requires exactly one explicit skipped Main section;
    every other step requires successful execution -- except conditional
    steps (#415), whose runtime-dynamic output guard may skip by design or
    execute, proved by exactly one section either way. Names must be unique.
    """
    maintenance = source_maintenance(as_dict(expanded.job), as_dict(expanded.document))
    if condition_excludes(as_dict(expanded.job).get("if"), expanded.inputs, set(), event=event,
                          outputs=expanded.outputs, dependencies=_needs(as_dict(expanded.job)),
                          scope=expanded.identity[:-1]):
        return ReplayJob(expanded.key, (), identity=expanded.identity, excluded=True,
                         remote_maintenance=maintenance)
    job = _named_job(expanded)
    names = tuple(str(step["name"]) for step in steps_of(job))
    if not names or len(set(names)) != len(names):
        raise ValueError("source-derived replay requires distinct named executable steps")
    if maintenance:
        return ReplayJob(expanded.key, names, identity=expanded.identity, remote_maintenance=True)
    inputs = excluded_input_steps(job, expanded.inputs, successful=True, event=event,
                                  outputs=expanded.outputs, scope=expanded.identity[:-1])
    minimal = tuple(skip for name in names
                    if mode == "minimal" and event == "pull_request"
                    and (skip := classify_minimal_skip(job, name)) is not None and name not in inputs)
    producers = {skip.producer for skip in minimal}
    if len(producers) > 1:
        raise ValueError("source-derived minimal exclusions have ambiguous producers")
    minimal_names = tuple(skip.name for skip in minimal)
    pr_saves = tuple(name for name in names if event == "pull_request" and approved_pr_cache_save(job, name)
                     and name not in (*inputs, *minimal_names))
    cache_saves = tuple(name for name in names if approved_cache_save(job, name)
                        and name not in (*inputs, *minimal_names, *pr_saves))
    conditional = conditional_step_names(
        job, {*inputs, *minimal_names, *pr_saves, *cache_saves}
    )
    return ReplayJob(expanded.key, names, cache_saves, minimal_names, next(iter(producers), ""),
                     pr_saves, inputs, expanded.identity, conditional_steps=conditional)
