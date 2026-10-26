"""Strict TOML declaration of workflow jobs whose execution must be proved."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ci_lint.finding import Finding
from ci_lint.execution_pins import ExecutionPins
from ci_lint.toml_cursor import Cursor, TomlValue
from ci_lint.workflow_replay import ReplayInput, ReplayJob
from ci_lint.workflow_replay_outputs import valid_output_name

WORKFLOW = r"[A-Za-z0-9_.-]+\.ya?ml"
JOB_REF = rf"{WORKFLOW}:[A-Za-z0-9_-]+"


@dataclass(frozen=True)
class DeclaredReplayJob:
    source_job: str
    proof: ReplayJob
    lanes: tuple[str, ...]
    derive_checks: bool = False
    capture_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplaySelection:
    lane: str
    selected_job: str | None
    event: str
    inputs: tuple[ReplayInput, ...]
    workflow: str | None = None


@dataclass(frozen=True)
class ReplayConfig:
    repository: str
    workflow: str
    mode: str
    jobs: tuple[DeclaredReplayJob, ...]
    selections: tuple[ReplaySelection, ...] = ()
    report_source: str = "file"
    qualified: bool = False
    provider_query: tuple[str, ...] = ()
    execution_pins: ExecutionPins | None = None


def _bad(findings: list[Finding], source: str, path: str, message: str) -> None:
    findings.append(Finding(rule="GATE-001", path=source, message=f"{path}: {message}",
                            fix="declare the exact workflow jobs, executed check names and covered lanes"))


def _capture(cursor: Cursor, *, qualified: bool, source: str, path: str,
             findings: list[Finding]) -> tuple[str, ...]:
    capture = cursor.list_str("capture-outputs", required=False)
    if capture and (not qualified or len(capture) > 256 or len(set(capture)) != len(capture)
                    or any(not valid_output_name(name) for name in capture)):
        _bad(findings, source, path, "capture-outputs requires distinct public output names and qualified replay")
    return tuple(capture)


def _job(raw: dict[str, TomlValue], *, source: str, path: str,
         findings: list[Finding], qualified: bool = False) -> DeclaredReplayJob | None:
    start = len(findings)
    cursor = Cursor(raw, path, findings, source)
    ref = cursor.str_("source-job")
    key = cursor.str_("key", required=not qualified)
    if key is None and qualified:
        key = ref
    derive_checks = qualified and "steps" not in raw
    steps = cursor.list_str("steps", required=not qualified)
    cache_saves = cursor.list_str("cache-save-steps", required=False)
    minimal_skips = cursor.list_str("minimal-skip-steps", required=False)
    mode_step = cursor.str_("minimal-mode-step", required=False) or ""
    pr_saves = cursor.list_str("pr-cache-save-steps", required=False)
    input_skips = cursor.list_str("input-skip-steps", required=False)
    lanes = cursor.list_str("lanes", required=False)
    capture = _capture(cursor, qualified=qualified, source=source, path=path, findings=findings)
    cursor.finish()
    if ref is None or re.fullmatch(JOB_REF, ref) is None:
        _bad(findings, source, path, "source-job must be a workflow basename and job id")
    if not key or not key.strip() or (not steps and not derive_checks) or len(set(steps)) != len(steps) or any(not step.strip() for step in steps):
        _bad(findings, source, path, "key and distinct executed check names must be nonempty")
    if len(set(cache_saves)) != len(cache_saves) or not set(cache_saves).issubset(steps):
        _bad(findings, source, path, "cache-save-steps must be distinct declared steps")
    if (len(set(minimal_skips)) != len(minimal_skips) or not set(minimal_skips).issubset(steps)
            or set(minimal_skips).intersection(cache_saves)
            or bool(minimal_skips) != bool(mode_step)
            or (mode_step and (mode_step not in steps or mode_step in minimal_skips or mode_step in cache_saves))):
        _bad(findings, source, path, "minimal exclusions require distinct declared steps and a mandatory mode producer")
    if len(set(lanes)) != len(lanes) or any(not lane.strip() for lane in lanes):
        _bad(findings, source, path, "lane names must be nonempty and distinct")
    if (len(set(pr_saves)) != len(pr_saves) or not set(pr_saves).issubset(steps)
            or set(pr_saves).intersection((*cache_saves, *minimal_skips, mode_step))):
        _bad(findings, source, path, "PR-only cache saves must be distinct disjoint declared steps")
    if (len(set(input_skips)) != len(input_skips) or not set(input_skips).issubset(steps)
            or set(input_skips).intersection((*cache_saves, *minimal_skips, *pr_saves, mode_step))):
        _bad(findings, source, path, "input exclusions must be distinct disjoint declared steps")
    if len(findings) != start or ref is None or key is None:
        return None
    return DeclaredReplayJob(ref, ReplayJob(key, tuple(steps), tuple(cache_saves), tuple(minimal_skips), mode_step,
                                          tuple(pr_saves), tuple(input_skips)), tuple(lanes), derive_checks, tuple(capture))


def _selected_job(cursor: Cursor, source: str, path: str,
                  findings: list[Finding]) -> str | None:
    selected = cursor.str_("job", required=False)
    all_jobs = cursor.bool_("all-jobs", required=False, default=False)
    if all_jobs:
        if selected is not None:
            _bad(findings, source, path, "all-jobs and job are mutually exclusive")
    elif selected is None or re.fullmatch(r"[A-Za-z0-9_-]+", selected) is None:
        _bad(findings, source, path, "selection requires a literal job or all-jobs = true")
    return selected


def _selection(raw: dict[str, TomlValue], *, source: str, path: str,
               findings: list[Finding]) -> ReplaySelection | None:
    start = len(findings)
    cursor = Cursor(raw, path, findings, source)
    lane = cursor.str_("lane")
    selected = _selected_job(cursor, source, path, findings)
    workflow = cursor.str_("workflow", required=False)
    event = cursor.str_("event")
    inputs = cursor.dict_str_str("inputs", required=False)
    cursor.finish()
    if not lane or not lane.strip():
        _bad(findings, source, path, "selection requires a lane")
    if workflow is not None and re.fullmatch(WORKFLOW, workflow) is None:
        _bad(findings, source, path, "selection workflow must be a workflow basename")
    if event not in ("pull_request", "workflow_dispatch"):
        _bad(findings, source, path, "selection event must be pull_request or workflow_dispatch")
    if any(not key.strip() for key in inputs):
        _bad(findings, source, path, "input names must be nonempty")
    if len(findings) != start or lane is None or event is None:
        return None
    return ReplaySelection(lane, selected, event, tuple(ReplayInput(e.key, e.value) for e in inputs.entries),
                           f".github/workflows/{workflow}" if workflow is not None else None)


def _selections(cursor: Cursor, source: str, path: str,
                findings: list[Finding]) -> tuple[ReplaySelection, ...]:
    selections: list[ReplaySelection] = []
    for index, item in enumerate(cursor.array_of_tables("selections")):
        selection = _selection(item, source=source, path=f"{path}.selections[{index}]", findings=findings)
        if selection is not None:
            selections.append(selection)
    return tuple(selections)


def _provider_query(cursor: Cursor, raw: dict[str, TomlValue], findings: list[Finding],
                    source: str, path: str, qualified: bool) -> tuple[str, ...]:
    query = tuple(cursor.list_str("provider-query", required=False))
    if "provider-query" in raw and (not query or any(not token.strip() for token in query)):
        _bad(findings, source, path, "provider-query must be a nonempty argv")
    if query and not qualified:
        _bad(findings, source, path, "provider-query requires qualified workflow replay")
    return query


def parse_replay(raw: dict[str, TomlValue], *, source: str, path: str,
                 findings: list[Finding]) -> ReplayConfig | None:
    start = len(findings)
    cursor = Cursor(raw, path, findings, source)
    repository = cursor.str_("repository")
    workflow = cursor.str_("workflow")
    mode = cursor.str_("mode")
    report_source = cursor.str_("report-source", required=False, default="file")
    qualified = bool(cursor.bool_("qualified", required=False, default=False))
    provider_query = _provider_query(cursor, raw, findings, source, path, qualified)
    raw_jobs = cursor.array_of_tables("jobs")
    selections = _selections(cursor, source, path, findings)
    cursor.finish()
    if repository is None or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        _bad(findings, source, path, "repository must be owner/name")
    if workflow is None or re.fullmatch(WORKFLOW, workflow) is None:
        _bad(findings, source, path, "workflow must be a workflow basename")
    if mode not in ("minimal", "full"):
        _bad(findings, source, path, "mode must be minimal or full")
    if report_source not in ("file", "stdout"):
        _bad(findings, source, path, "report-source must be file or stdout")
    jobs: list[DeclaredReplayJob] = []
    for index, item in enumerate(raw_jobs):
        job = _job(item, source=source, path=f"{path}.jobs[{index}]", findings=findings, qualified=qualified)
        if job is not None:
            jobs.append(job)
    covered_lanes = {lane for job in jobs for lane in job.lanes}
    if len({item.lane for item in selections}) != len(selections) or any(item.lane not in covered_lanes for item in selections):
        _bad(findings, source, path, "selections must name distinct replay-covered lanes")
    if (not jobs or len({job.proof.key for job in jobs}) != len(jobs)
            or (qualified and len({job.source_job for job in jobs}) != len(jobs))):
        _bad(findings, source, path, "declare distinct execution keys and, in qualified mode, distinct source-jobs")
    if len(findings) != start or repository is None or workflow is None or mode is None or report_source is None:
        return None
    return ReplayConfig(repository, f".github/workflows/{workflow}", mode, tuple(jobs), tuple(selections), report_source,
                        qualified, provider_query)
