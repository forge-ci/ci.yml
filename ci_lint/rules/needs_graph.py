"""GEN-024 / GEN-025: `needs:` edges that only add a serial hop.

GEN-024 (zackees/ci.yml#83): a job on a Linux runner `needs:` another job
only for ordering. "Gate first, then fan out" saves scarce macOS, Windows and
ARM runners; a Linux job serialized behind the Linux gate just lengthens the
PR critical path. Static signals, per (job, needed job) edge:

- violation: a literal Linux `runs-on` (ubuntu-*/`linux`, not an ARM label),
  every `needs.<X>.` reference in the job is `needs.<X>.outputs.{mode,sha,ref}`
  (deterministic, event-derived -- the job can re-derive them), at least one
  such reference exists, and the job neither downloads an artifact nor
  restores a cache (the two ways it could consume the upstream job's writes).
- needs_review: the same, but the job restores a cache (the upstream job may
  be its writer), or the edge has no reference at all and the job downloads
  nothing (pure ordering -- or a cache/side-effect dependency ci-lint cannot
  see).

Not a finding: any other `needs.<X>.` use (`result`, other outputs), a
non-Linux or dynamic `runs-on`, or a job that downloads an artifact.

GEN-025 (zackees/ci.yml#86): a trailing aggregate/sentinel job adds a
runner-queue hop. A job is an aggregate when nothing `needs:` it, it `needs:`
other jobs, every step is a `run:` step (no checkout, no action), and its
steps or `if:` test `needs.*` (`.result`, `toJSON(needs)`, `contains(needs`).
needs_review -- the row's own proposed detection -- when every needed job has
no `if:` and no `strategy.matrix`: those jobs cannot be silently skipped, so
they could be required directly (option a) or carry the check as a last step
(option b). Exempt: a `ci-lint gate` / `ci_lint gate` aggregator, the
policy's sanctioned single `CI OK` check, and any aggregate over a needed job
that is conditional or a matrix (a plan-dependent set, which needs one stable
check name).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import ParsedYamlFile, as_dict, as_list, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

_NEEDS_REF = re.compile(r"needs\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)")
_DETERMINISTIC = re.compile(r"outputs\.(mode|sha|ref)$")
_AGG_NEEDS = re.compile(r"needs\.[A-Za-z0-9_*-]+\.result|toJSON\(\s*needs\s*\)|contains\(\s*needs\b", re.I)
_GATE_CMD = re.compile(r"ci[-_]lint\s+gate\b")
_ARM = re.compile(r"arm|aarch64", re.I)


def _needs(job: dict[str, YamlValue]) -> tuple[str, ...]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return (raw,)
    return tuple(x for x in as_list(raw) if isinstance(x, str))


def _is_linux(job: dict[str, YamlValue]) -> bool:
    raw = job.get("runs-on")
    labels = [raw] if isinstance(raw, str) else [x for x in as_list(raw) if isinstance(x, str)]
    if not labels or any("${{" in label for label in labels):
        return False
    if any(_ARM.search(label) for label in labels):
        return False
    return any(label.startswith("ubuntu") or label.lower() == "linux" for label in labels)


def _uses(step: dict[str, YamlValue]) -> str:
    uses = step.get("uses")
    return uses.lower() if isinstance(uses, str) else ""


def _downloads_artifact(job: dict[str, YamlValue]) -> bool:
    return any("download-artifact" in _uses(s) for s in steps_of(job))


def _restores_cache(job: dict[str, YamlValue]) -> bool:
    return any(
        u.startswith("actions/cache") or "setup-soldr" in u or "cache" in u.split("@", 1)[0]
        for u in (_uses(s) for s in steps_of(job))
    )


def check_gen_024(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for jid, job in jobs_of(as_dict(wf.document)).items():
            needs = _needs(job)
            if not needs or not _is_linux(job) or _downloads_artifact(job):
                continue
            text = json.dumps(job)
            cache = _restores_cache(job)
            for need in needs:
                refs = [m.group(2) for m in _NEEDS_REF.finditer(text) if m.group(1) == need]
                if any(not _DETERMINISTIC.search(r) for r in refs):
                    continue
                if refs and not cache:
                    status = Status.VIOLATION
                    why = (
                        f"uses only {', '.join(sorted({f'needs.{need}.{r}' for r in refs}))} -- "
                        "deterministic, event-derived values it can re-derive itself"
                    )
                elif refs:
                    status = Status.NEEDS_REVIEW
                    why = "uses only deterministic outputs, but restores a cache the upstream job may write"
                else:
                    status = Status.NEEDS_REVIEW
                    why = "references nothing from it and downloads no artifact (ordering only, or an unseen cache dependency)"
                findings.append(
                    Finding(
                        rule="GEN-024",
                        status=status,
                        path=wf.path,
                        message=f"Linux job '{jid}' needs '{need}' but {why}; the edge serializes it onto the PR critical path",
                        fix=(
                            "drop the needs: edge and re-derive mode/sha/ref with the same script the gate "
                            "uses (e.g. ci/ci_mode.py); keep it only when the job consumes the upstream "
                            "job's artifacts, cache writes, or a non-deterministic output"
                        ),
                    )
                )
    return findings


def _is_aggregate(job: dict[str, YamlValue]) -> bool:
    steps = steps_of(job)
    if not steps or any("uses" in s or not isinstance(s.get("run"), str) for s in steps):
        return False
    text = json.dumps(job)
    if _GATE_CMD.search(text):
        return False
    return _AGG_NEEDS.search(text) is not None


def check_gen_025(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        jobs = jobs_of(as_dict(wf.document))
        needed_by_someone = {n for job in jobs.values() for n in _needs(job)}
        for jid, job in jobs.items():
            needs = _needs(job)
            if not needs or jid in needed_by_someone or not _is_aggregate(job):
                continue
            targets = [jobs.get(n) for n in needs]
            if any(t is None or "if" in t or "matrix" in as_dict(t.get("strategy")) for t in targets):
                continue
            findings.append(
                Finding(
                    rule="GEN-025",
                    status=Status.NEEDS_REVIEW,
                    path=wf.path,
                    message=(
                        f"job '{jid}' only aggregates needs.* results of {', '.join(repr(n) for n in needs)}, "
                        "none of which is conditional or a matrix -- a trailing hop that waits for a "
                        "runner after the real work finishes"
                    ),
                    fix=(
                        "fold the check into the last step of the slowest work job (option b), or require "
                        "the work jobs directly (option a); a separate aggregate is justified only as the "
                        "single `ci-lint gate` / CI OK check over a plan-dependent job set"
                    ),
                )
            )
    return findings


def check_needs_graph(repo_root: Path) -> list[Finding]:
    workflows = load_workflows(repo_root)
    return [*check_gen_024(workflows), *check_gen_025(workflows)]
