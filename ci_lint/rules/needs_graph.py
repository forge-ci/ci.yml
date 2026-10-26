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

Exempt legitimate ordering-only edges, identified by the commands the jobs
run (a `run:` line, or one level into a referenced `ci/*.py` script), never
by job name:

- (a) cache maintenance: the needed job runs `ci-lint cache
  preprune`/`janitor`/`trim` and the dependent job uses a cache (it is a
  writer, so its save must land after the prune).
- (b) the GEN-022 end-of-queue janitor: the dependent job itself runs
  `ci-lint cache janitor`, which GEN-022 requires to wait on every job
  (see ci_lint/rules/merge_queue.py).

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
_CACHE_MAINT = re.compile(r"\bcache[\"',\s]+(?:preprune|janitor|trim)\b")
_JANITOR = re.compile(r"\bcache[\"',\s]+janitor\b")
_CI_SCRIPT = re.compile(r"(?<![\w/.-])(?:\./)?(ci/[A-Za-z0-9_./-]+\.py)\b")
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


def _uses_cache(job: dict[str, YamlValue], repo_root: Path | None) -> bool:
    """A cache restore/save step, including setup-uv and a local composite action wrapping one."""
    if _restores_cache(job):
        return True
    for uses in (_uses(s) for s in steps_of(job)):
        if uses.startswith("astral-sh/setup-uv"):
            return True
        if uses.startswith("./") and repo_root is not None and ".." not in uses:
            for name in ("action.yml", "action.yaml"):
                path = repo_root / uses[2:] / name
                if path.is_file() and re.search(
                    r"actions/cache|setup-soldr|setup-uv", path.read_text(encoding="utf-8", errors="replace")
                ):
                    return True
    return False


def _command_text(job: dict[str, YamlValue], repo_root: Path | None) -> str:
    """The job's `run:` text plus, one level deep, each referenced `ci/*.py` script."""
    runs = [s["run"] for s in steps_of(job) if isinstance(s.get("run"), str)]
    parts = list(runs)
    if repo_root is not None:
        for script in sorted({m.group(1) for r in runs for m in _CI_SCRIPT.finditer(r)}):
            path = repo_root / script
            if ".." not in script and path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _exempt_job(job: dict[str, YamlValue], repo_root: Path | None) -> bool:
    """Not Linux, downloads an artifact, or shape (b): GEN-022's end-of-queue janitor needs every job."""
    if not _is_linux(job) or _downloads_artifact(job):
        return True
    return _JANITOR.search(_command_text(job, repo_root)) is not None


def _prune_ordered(job: dict[str, YamlValue], upstream: dict[str, YamlValue] | None, repo_root: Path | None) -> bool:
    """Shape (a): a cache-using job needs a job that runs `cache preprune`/`janitor`/`trim`."""
    if upstream is None or not _uses_cache(job, repo_root):
        return False
    return _CACHE_MAINT.search(_command_text(upstream, repo_root)) is not None


def check_gen_024(workflows: list[ParsedYamlFile], repo_root: Path | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        jobs = jobs_of(as_dict(wf.document))
        for jid, job in jobs.items():
            needs = _needs(job)
            if not needs or _exempt_job(job, repo_root):
                continue
            text = json.dumps(job)
            cache = _restores_cache(job)
            for need in needs:
                refs = [m.group(2) for m in _NEEDS_REF.finditer(text) if m.group(1) == need]
                if any(not _DETERMINISTIC.search(r) for r in refs):
                    continue
                if _prune_ordered(job, jobs.get(need), repo_root):
                    continue  # (a) a cache writer saves after the prune it needs
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
    return [*check_gen_024(workflows, repo_root), *check_gen_025(workflows)]
