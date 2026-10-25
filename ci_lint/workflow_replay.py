"""Validate Bosn's terminal workflow evidence before emitting local proof.

This module validates execution evidence. It does not establish that a
repository's declared jobs cover every remote check; static coverage is separate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.execution_pins import ExecutionPins, parse_execution_pins
from ci_lint.workflow_replay_identity import JobIdentity, matrix_json, parse_identity
from ci_lint.workflow_replay_outputs import ReplayOutput, proved_outputs, require_output_producer
from ci_lint.workflow_replay_maintenance import prove_maintenance


@dataclass(frozen=True)
class ReplayJob:
    key: str
    steps: tuple[str, ...]
    cache_save_steps: tuple[str, ...] = ()
    minimal_skip_steps: tuple[str, ...] = ()
    minimal_mode_step: str = ""
    pr_cache_save_steps: tuple[str, ...] = ()
    input_skip_steps: tuple[str, ...] = ()
    identity: tuple[JobIdentity, ...] = ()
    excluded: bool = False
    remote_maintenance: bool = False
    # #415: source-derived steps guarded by a runtime-dynamic step output
    # (a pass-marker cache-hit lookup): proved by exactly one Main section
    # that either succeeded or explicitly skipped -- never absent, never
    # failed. Appended last so every existing positional construction is
    # unaffected; only `derive_checks` populates it today.
    conditional_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayInput:
    name: str
    value: str


@dataclass(frozen=True)
class ReplayExpectation:
    repository: str
    workspace: Path
    sha: str
    git_tree: str
    workflow: str
    selected_job: str | None
    mode: str
    required_jobs: tuple[ReplayJob, ...]
    trigger: str = "pr"
    event: str = "pull_request"
    inputs: tuple[ReplayInput, ...] = ()
    execution_pins: ExecutionPins | None = None


@dataclass(frozen=True)
class ReplayProof:
    jobs: tuple[str, ...]
    outputs: tuple[ReplayOutput, ...] = ()


def _inputs(raw: dict[str, JsonValue], expected: ReplayExpectation) -> None:
    if len({item.name for item in expected.inputs}) != len(expected.inputs):
        raise ValueError("workflow replay input expectations are duplicated")
    if any(not item.name.strip() for item in expected.inputs):
        raise ValueError("workflow replay input name is empty")
    params = raw.get("params")
    actual = params.get("inputs") if isinstance(params, dict) else None
    wanted = {item.name: item.value for item in expected.inputs}
    if actual is None and not wanted:
        return
    if not isinstance(actual, dict) or actual != wanted:
        raise ValueError("workflow replay inputs differ from the declared selection")


def _metadata(raw: dict[str, JsonValue], expected: ReplayExpectation) -> None:
    if expected.execution_pins is not None:
        if parse_execution_pins(raw.get("execution_pins")) != expected.execution_pins:
            raise ValueError("workflow replay execution provider differs from the queried daemon")
        if raw.get("act_version") != expected.execution_pins.act_version:
            raise ValueError("workflow replay act version differs from execution provider pins")
    scalars: dict[str, str | int | None] = {
        "schema_version": 1, "provider": "github", "repository": expected.repository, "engine": "act",
        "sha": expected.sha, "git_tree": expected.git_tree, "dirty": None,
        "workflow": expected.workflow, "job": expected.selected_job,
        "trigger": expected.trigger, "event": expected.event, "mode": expected.mode, "state": "done",
        "conclusion": "success", "exit_code": 0, "act_exit_code": 0,
        "cleanup": "removed",
    }
    for key, value in scalars.items():
        if key not in raw or type(raw[key]) is not type(value) or raw[key] != value:
            raise ValueError(f"workflow replay metadata mismatch: {key}")
    workspace = raw.get("workspace")
    if not isinstance(workspace, str) or Path(workspace).resolve() != expected.workspace.resolve():
        raise ValueError("workflow replay ran another workspace")
    digest = raw.get("tree_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("workflow replay has no source snapshot digest")
    version = raw.get("act_version")
    match = re.fullmatch(r"\d+\.\d+\.\d+-act2\.(\d+)", version) if isinstance(version, str) else None
    if match is None or int(match.group(1)) < 3:
        raise ValueError("workflow replay requires act2.3 or later executed-step evidence")


@dataclass(frozen=True)
class _JobDocument:
    key: str
    raw: dict[str, JsonValue]
    identity: tuple[JobIdentity, ...] = ()


def _jobs(raw: JsonValue, *, qualified: bool = False) -> tuple[_JobDocument, ...]:
    # These maps are parsed JSON documents; callers receive validated proof records.
    if not isinstance(raw, dict) or type(raw.get("malformed_lines")) is not int or raw["malformed_lines"] != 0:
        raise ValueError("workflow replay job tree is malformed")
    groups = raw.get("groups")
    if not isinstance(groups, list):
        raise ValueError("workflow replay groups are missing")
    jobs: list[_JobDocument] = []
    identities: set[tuple[JobIdentity, ...]] = set()
    keys: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("jobs"), list):
            raise ValueError("workflow replay group is malformed")
        for job in group["jobs"]:
            if not isinstance(job, dict):
                raise ValueError("workflow replay job is malformed")
            key = job.get("key")
            identity = parse_identity(job.get("identity")) if qualified else ()
            duplicate = identity in identities if qualified else key in keys
            if not isinstance(key, str) or not key or duplicate:
                raise ValueError("workflow replay job identity is missing or duplicated")
            jobs.append(_JobDocument(key, job, identity))
            identities.add(identity)
            keys.add(key)
    return tuple(jobs)


def _step(section: JsonValue, name: str) -> bool:
    if not isinstance(section, dict) or section.get("name") != name:
        return False
    first = section.get("first_seq")
    last = section.get("last_seq")
    return (section.get("stage") == "Main" and section.get("status") == "completed"
            and section.get("conclusion") == "success" and type(first) is int and type(last) is int
            and 0 < first <= last)


def _step_accepted(name: str, expected: ReplayJob, section: dict[str, JsonValue]) -> bool:
    """Does this single matched Main section prove `name` for its class?

    Finite exclusions demand an explicit skip; conditional steps (#415)
    accept success *or* an explicit skip (a runtime-dynamic output guard,
    e.g. a pass-marker cache-hit, skips them by design); cache-save steps
    accept either as their save may be conditionally withheld; every other
    required check demands unambiguous success.
    """

    skipped = section.get("status") == "completed" and section.get("conclusion") == "skipped"
    if name in (*expected.minimal_skip_steps, *expected.pr_cache_save_steps, *expected.input_skip_steps):
        return skipped
    if name in expected.conditional_steps:
        return _step(section, name) or skipped
    return _step(section, name) or (name in expected.cache_save_steps and skipped)


def _prove_job(raw: dict[str, JsonValue], expected: ReplayJob) -> None:
    if expected.identity and (raw.get("job_id") != expected.identity[-1].job_id
                              or matrix_json(raw.get("matrix")) != expected.identity[-1].matrix):
        raise ValueError(f"workflow replay leaf differs from qualified identity: {expected.key}")
    if expected.excluded:
        if (raw.get("status") != "completed" or raw.get("conclusion") != "skipped"
                or raw.get("sections") != []):
            raise ValueError(f"workflow replay excluded job has no explicit empty skip: {expected.key}")
        return
    if expected.remote_maintenance:
        prove_maintenance(raw, expected.key, expected.steps)
        return
    if raw.get("status") != "completed" or raw.get("conclusion") != "success":
        raise ValueError(f"workflow replay job did not pass: {expected.key}")
    if (len(set(expected.cache_save_steps)) != len(expected.cache_save_steps)
            or not set(expected.cache_save_steps).issubset(expected.steps)):
        raise ValueError("cache-save steps must be distinct declared steps")
    sections = raw.get("sections")
    if not isinstance(sections, list):
        raise ValueError(f"workflow replay job has no executed steps: {expected.key}")
    for name in expected.steps:
        named = [section for section in sections if isinstance(section, dict) and section.get("name") == name
                 and section.get("stage") == "Main"]
        if len(named) != 1 or not _step_accepted(name, expected, named[0]):
            raise ValueError(f"workflow replay lacks an unambiguous executed check: {expected.key}: {name}")


def _minimal_skips(expected: ReplayExpectation) -> None:
    for job in expected.required_jobs:
        skips = set(job.minimal_skip_steps)
        if not skips:
            if job.minimal_mode_step:
                raise ValueError("minimal mode producer requires declared exclusions")
            continue
        if (expected.mode != "minimal" or expected.event != "pull_request"
                or len(skips) != len(job.minimal_skip_steps) or not skips.issubset(job.steps)
                or skips.intersection(job.cache_save_steps)
                or not job.minimal_mode_step or job.minimal_mode_step not in job.steps
                or job.minimal_mode_step in skips or job.minimal_mode_step in job.cache_save_steps):
            raise ValueError("minimal exclusions require a mandatory producer and a minimal PR selection")


def _pr_cache_saves(expected: ReplayExpectation) -> None:
    for job in expected.required_jobs:
        saves = set(job.pr_cache_save_steps)
        if saves and (expected.event != "pull_request" or expected.trigger != "pr"
                      or len(saves) != len(job.pr_cache_save_steps) or not saves.issubset(job.steps)
                      or saves.intersection((*job.cache_save_steps, *job.minimal_skip_steps))
                      or job.minimal_mode_step in saves):
            raise ValueError("PR-only cache saves require distinct exclusions and a PR selection")


def _input_skips(expected: ReplayExpectation) -> None:
    for job in expected.required_jobs:
        skips = set(job.input_skip_steps)
        if (len(skips) != len(job.input_skip_steps) or not skips.issubset(job.steps)
                or skips.intersection((*job.cache_save_steps, *job.minimal_skip_steps, *job.pr_cache_save_steps))
                or job.minimal_mode_step in skips):
            raise ValueError("input exclusions require distinct disjoint declared steps")


def _conditional_steps(expected: ReplayExpectation) -> None:
    """#415: conditional steps are a source-derived class only (the
    declared-proof TOML cannot express them); they must be distinct, inside
    `steps`, disjoint from every finite exclusion -- whose section-shape
    contracts are stricter -- and absent from excluded/maintenance jobs."""

    for job in expected.required_jobs:
        steps = set(job.conditional_steps)
        if (len(steps) != len(job.conditional_steps) or not steps.issubset(set(job.steps))
                or steps.intersection((*job.cache_save_steps, *job.minimal_skip_steps,
                                       *job.pr_cache_save_steps, *job.input_skip_steps))
                or ((job.excluded or job.remote_maintenance) and steps)):
            raise ValueError("conditional steps require distinct disjoint declared steps")


def _valid_checks(job: ReplayJob) -> bool:
    if job.excluded:
        return bool(job.identity) and not (job.steps or job.cache_save_steps or job.minimal_skip_steps
                                          or job.minimal_mode_step or job.pr_cache_save_steps or job.input_skip_steps)
    if job.remote_maintenance and (not job.identity or job.cache_save_steps or job.minimal_skip_steps
                                   or job.minimal_mode_step or job.pr_cache_save_steps or job.input_skip_steps):
        return False
    return bool(job.steps) and len(set(job.steps)) == len(job.steps)


def prove_replay(raw: JsonValue, expected: ReplayExpectation) -> ReplayProof:
    if not isinstance(raw, dict):
        raise ValueError("workflow replay evidence must be a JSON object")
    qualified = any(job.identity for job in expected.required_jobs)
    distinct = len({job.identity if qualified else job.key for job in expected.required_jobs})
    if not expected.required_jobs or distinct != len(expected.required_jobs) or (qualified and any(
            not job.identity for job in expected.required_jobs)):
        raise ValueError("workflow replay requires distinct named jobs")
    _minimal_skips(expected)
    _pr_cache_saves(expected)
    _input_skips(expected)
    _conditional_steps(expected)
    if (all(job.excluded or job.remote_maintenance for job in expected.required_jobs)
            or any(not job.key or not _valid_checks(job) for job in expected.required_jobs)):
        raise ValueError("workflow replay requires distinct executed checks in each job")
    if re.fullmatch(r"[0-9a-f]{40}", expected.sha) is None or re.fullmatch(r"[0-9a-f]{40}", expected.git_tree) is None:
        raise ValueError("workflow replay requires exact source and tree object IDs")
    if (expected.trigger, expected.event) not in (("pr", "pull_request"), ("workflow_dispatch", "workflow_dispatch")):
        raise ValueError("workflow replay requires a declared PR or dispatch selection")
    _inputs(raw, expected)
    _metadata(raw, expected)
    jobs = _jobs(raw.get("tree"), qualified=qualified)
    if any(item.raw.get("output_evidence") is not None for item in jobs):
        require_output_producer(str(raw["act_version"]))
    by_identity = {item.identity: item for item in jobs}
    by_key = {item.key: item for item in jobs}
    outputs: list[ReplayOutput] = []
    for job in expected.required_jobs:
        actual = by_identity.get(job.identity) if qualified else by_key.get(job.key)
        if actual is None:
            raise ValueError(f"workflow replay required job is missing: {job.key}")
        _prove_job(actual.raw, job)
        outputs.extend(proved_outputs(actual.raw, job.identity, excluded=job.excluded or job.remote_maintenance))
    return ReplayProof(tuple(job.key for job in expected.required_jobs if not job.remote_maintenance), tuple(outputs))
