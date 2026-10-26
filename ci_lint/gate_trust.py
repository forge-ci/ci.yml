"""GATE-008: an attested PR head may stand in for its remote quick-gate jobs.

zackees/ci.yml#190 (pilot #189, design docs/designs/pr-critical-path.md).
GATE-003 makes every PR head carry a tree-bound `Local-Gate:` trailer, and
GATE-001 makes the mirrored remote jobs run exactly the gate command. On a
repository that opts in, `ci-lint local-gate verify --trust` decides whether
those remote jobs may be *skipped* for this head: the job-level `if:` of
each declared `[gate.trust].skip` job consumes the verify job's `trusted`
output. The decision fails closed -- every row below that is not
`trusted` means the jobs run exactly as they did before.

The decision reads the opt-in, the required lanes and the surfaces from the
**base** commit's declaration, never the head's, so a PR cannot weaken its
own gate and then attest against the weaker one. It never trusts a push,
schedule or dispatch run: the default-branch push after a merge always runs
the jobs for real, which is the post-merge catch for a bad attestation or a
merge skew, ahead of the release gate's exact-SHA full run.

Reason codes (`trust_reason` output): `trusted`, `shadow` (would trust;
mode = shadow), `not-pull-request`, `not-opted-in`, `base-unavailable`,
`full-label`, `fork`, `author-not-trusted`, `head-not-attested`,
`lanes-missing`, `surface-changed`, `audit-sample`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.gate_python_surfaces import python_surfaces
from ci_lint.toml_cursor import Cursor, TomlValue

TRUST_MODES: tuple[str, ...] = ("never", "shadow", "enforce")
# `author_association` values of people who can push to the repository.
TRUSTED_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_FULL_LABELS: tuple[str, ...] = ("ci-full",)
DEFAULT_AUDIT_RATE = 10
TRUSTED_OUTPUT = "trusted"
_JOB_REF = re.compile(r"^[^/:]+:[^:]+$")
_LOCAL_USES = re.compile(r"""uses:\s*['"]?\./(?P<path>[^\s'"@]+)""")


@dataclass(frozen=True)
class CoveredBy:
    """A skip job that is not a GATE-001 mirror runs more than the gate
    command; this names the local lanes the owner asserts cover it."""

    job: str
    lanes: tuple[str, ...]


@dataclass(frozen=True)
class TrustConfig:
    mode: str
    skip: tuple[str, ...]  # `<workflow file>:<job id>`
    covered_by: tuple[CoveredBy, ...]
    surfaces: tuple[str, ...]
    full_labels: tuple[str, ...]
    audit_rate: int  # 1 in N trusted heads still runs remotely; 0 disables

    def lanes_for(self, job: str) -> tuple[str, ...] | None:
        for entry in self.covered_by:
            if entry.job == job:
                return entry.lanes
        return None


def parse_trust(raw: dict[str, TomlValue], *, path: str, source: str, findings: list[Finding]) -> TrustConfig | None:
    sub = Cursor(raw, path, findings, source)
    mode = sub.str_("mode", required=False, default="never") or "never"
    skip = sub.list_str("skip", required=False)
    covered = sub.dict_str_list_str("covered-by", required=False)
    surfaces = sub.list_str("surfaces", required=False)
    full_labels = sub.list_str("full-labels", required=False, default=DEFAULT_FULL_LABELS)
    audit_rate = sub.int_("audit-rate", required=False, default=DEFAULT_AUDIT_RATE)
    sub.finish()

    def bad(message: str, fix: str) -> None:
        findings.append(Finding(rule="GATE-008", path=source, message=f"{path}: {message}", fix=fix))

    if mode not in TRUST_MODES:
        bad(f"'mode' is {mode!r}", f"set it to one of {', '.join(TRUST_MODES)}")
        mode = "never"
    rate = audit_rate if audit_rate is not None else DEFAULT_AUDIT_RATE
    if rate < 0 or rate == 1:
        bad(f"'audit-rate' is {rate}", "use 0 (no audit sample) or N >= 2 (1 in N attested heads still runs remotely)")
        rate = DEFAULT_AUDIT_RATE
    for job in (*skip, *covered):
        if not _JOB_REF.match(job):
            bad(f"job {job!r} is not '<workflow file>:<job id>'", "write it as e.g. 'ci.yml:lint'")
    if mode != "never" and not skip:
        bad("trust is enabled but 'skip' names no job", "list the remote quick-gate jobs the attestation stands in for")
    return TrustConfig(
        mode=mode,
        skip=tuple(skip),
        covered_by=tuple(CoveredBy(e.key, e.values) for e in covered.entries),
        surfaces=tuple(surfaces),
        full_labels=tuple(full_labels),
        audit_rate=rate,
    )


@dataclass(frozen=True)
class TrustInput:
    """What the PR event says, plus the base commit to read policy from."""

    event: str
    head_sha: str
    base_sha: str | None
    author_association: str | None
    head_repo: str | None
    base_repo: str | None
    labels: tuple[str, ...]
    title_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrustDecision:
    trusted: bool  # skip the declared jobs
    would_trust: bool  # what enforce mode would have decided (shadow reporting)
    reason: str
    detail: str

    def render(self) -> str:
        verdict = "skip remote quick gate" if self.trusted else "run remote quick gate"
        return f"[GATE-008] {verdict} ({self.reason}): {self.detail}"


def _no(reason: str, detail: str) -> TrustDecision:
    return TrustDecision(False, False, reason, detail)


def glob_matches(pattern: str, path: str) -> bool:
    from ci_lint.lane_cache import glob_to_regex  # noqa: PLC0415 -- avoid an import cycle

    return glob_to_regex(pattern).match(path) is not None


def audit_sampled(head_sha: str, rate: int) -> bool:
    """Deterministic per head: every new push is a fresh 1-in-N draw, and a
    rerun of the same head gets the same answer."""

    if rate <= 0:
        return False
    return int(hashlib.sha256(head_sha.encode()).hexdigest()[:8], 16) % rate == 0


def automatic_surfaces(repo: Path, rev: str, config_source: str, gate_run: tuple[str, ...],
                       lane_runs: tuple[tuple[str, ...], ...], workflows: tuple[str, ...]) -> tuple[str, ...]:
    """Files whose change alters what "passed locally" or the skipped jobs
    mean: the declaration, every repository file named in the gate's or a
    lane's argv, their statically resolvable tracked Python imports, each
    named workflow, and every local reusable workflow or
    action those workflows reference (transitively, over-approximated by a
    text scan of the whole file)."""

    from ci_lint.local_gate import GitError, _git  # noqa: PLC0415

    try:
        tracked = set(_git(repo, "ls-tree", "-r", "--name-only", rev).splitlines())
    except GitError:
        tracked = set()
    out: set[str] = {config_source, "ci-attestations.yml"}
    for argv in (gate_run, *lane_runs):
        out.update(token for token in argv if token in tracked)
    out.update(python_surfaces(repo, rev, tracked, tuple(sorted(out))))
    todo = [f".github/workflows/{wf}" for wf in workflows]
    seen: set[str] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        out.add(path)
        try:
            text = _git(repo, "show", f"{rev}:{path}")
        except GitError:
            continue
        for match in _LOCAL_USES.finditer(text):
            target = match.group("path").rstrip("/")
            if target.endswith((".yml", ".yaml")):
                todo.append(target)
            else:
                out.add(f"{target}/**")
                for name in ("action.yml", "action.yaml"):
                    todo.append(f"{target}/{name}")
    return tuple(sorted(out))


def decide(repo: Path, inp: TrustInput) -> TrustDecision:  # noqa: C901
    from ci_lint.local_gate import (  # noqa: PLC0415 -- local_gate imports this module
        GitError,
        _git,
        check_commit,
        load_gate_config_at,
        parse_attestation,
    )

    if inp.event not in ("pull_request", "pull_request_target"):
        return _no("not-pull-request", f"event '{inp.event}' always runs the remote jobs (the post-merge catch)")
    if not inp.base_sha:
        return _no("base-unavailable", "no base commit in the event payload")
    try:
        merge_base = _git(repo, "merge-base", inp.base_sha, inp.head_sha).strip()
    except GitError as exc:
        return _no("base-unavailable", f"cannot relate base {inp.base_sha[:12]} to head (fetch full history): {exc}")
    loaded = load_gate_config_at(repo, inp.base_sha)
    base = loaded.config
    if base is None or base.trust is None or base.trust.mode == "never":
        return _no("not-opted-in", "the base commit's gate declaration does not enable [gate.trust]")
    trust = base.trust
    hit = sorted(set((*inp.labels, *inp.title_tags)) & set(trust.full_labels))
    if hit:
        return _no("full-label", f"selection {hit[0]!r} forces the full remote run")
    if inp.head_repo and inp.base_repo and inp.head_repo != inp.base_repo:
        return _no("fork", f"head repository {inp.head_repo} is a fork of {inp.base_repo}")
    if (inp.author_association or "") not in TRUSTED_ASSOCIATIONS:
        return _no("author-not-trusted", f"author association {inp.author_association or 'unknown'!r} has no write access")
    state = check_commit(repo, inp.head_sha)
    if state.state != "attested":
        # `attested-parent` (GitHub's "Update branch" merge) is fine for
        # GATE-003 but not here: its tree includes base changes the local
        # gate never saw together with this PR's.
        return _no("head-not-attested", f"{inp.head_sha[:12]} is {state.state}: {state.detail}")
    if base.lanes:
        att = parse_attestation(_git(repo, "log", "-1", "--format=%B", inp.head_sha))
        items = [item.split(":", 1) for item in (att.lanes or "").split(",") if item] if att else []
        provenance = {i[0] for i in items if len(i) == 2 and i[1] != "n/a"}
        not_applicable = {i[0] for i in items if len(i) == 2 and i[1] == "n/a"}
        # An `optional` lane that could not run on the author's host counts
        # as present for the head-level check; its gates simply carry no
        # attestation (GATE-010: omission = not run, so CI runs them).
        missing = [lane.id for lane in base.lanes
                   if lane.id not in provenance and not (lane.optional and lane.id in not_applicable)]
        if missing:
            return _no("lanes-missing", f"the trailer lacks base-declared lane(s) {', '.join(missing)}")
    workflows = tuple(sorted({job.split(":", 1)[0] for job in trust.skip}
                             | ({base.verify.workflow} if base.verify else set())))
    surfaces = (*trust.surfaces, *automatic_surfaces(
        repo, inp.base_sha, base.source, base.run, tuple(lane.run for lane in base.lanes), workflows))
    try:
        changed = [p for p in _git(repo, "diff", "--name-only", merge_base, inp.head_sha).splitlines() if p]
    except GitError as exc:
        return _no("base-unavailable", f"cannot diff the PR: {exc}")
    for path in changed:
        for pattern in surfaces:
            if path == pattern or glob_matches(pattern, path):
                return _no("surface-changed", f"the PR changes {path} (surface {pattern}); only a remote run can validate it")
    if audit_sampled(inp.head_sha, trust.audit_rate):
        return _no("audit-sample", f"1-in-{trust.audit_rate} audit: this head runs remotely to measure attestation honesty")
    lanes = f"; lanes {', '.join(lane.id for lane in base.lanes)}" if base.lanes else ""
    detail = f"{inp.head_sha[:12]} attested for its tree, {len(changed)} changed path(s), no gate surface touched{lanes}"
    if trust.mode == "shadow":
        return TrustDecision(False, True, "shadow", detail + " (mode = shadow: reported, not applied)")
    return TrustDecision(True, True, "trusted", detail)


@dataclass(frozen=True)
class WorkflowJob:
    """What the static check needs to know about one workflow job."""

    workflow: str  # file name under .github/workflows/
    job_id: str
    condition: str | None  # the job-level `if:`, when it is a string
    runs_verify: bool = False  # a step runs `local-gate verify`


@dataclass(frozen=True)
class WorkflowFacts:
    jobs: tuple[WorkflowJob, ...]
    push_triggered: frozenset[str]  # workflow files with an `on: push` trigger

    def job(self, workflow: str, job_id: str) -> WorkflowJob | None:
        for job in self.jobs:
            if job.workflow == workflow and job.job_id == job_id:
                return job
        return None


def _decision_findings(job: WorkflowJob, workflow: str, workflows: WorkflowFacts, needle: str, per_job: str,
                       path: str) -> list[Finding]:
    """The skip job's `if:` reads a trust decision, and every
    `needs.<id>.outputs.*` it reads is produced by a job `<id>` in the SAME
    workflow that runs `local-gate verify`: `needs:` cannot cross workflow
    files, so a missing or non-verify producer leaves the decision empty."""

    decision_re = re.compile(rf"needs\.([A-Za-z_][A-Za-z0-9_-]*)\.outputs\.(?:{TRUSTED_OUTPUT}|skip_{re.escape(job.job_id)})\b")
    producers = sorted(set(decision_re.findall(job.condition or "")))
    if not producers:
        return [Finding(rule="GATE-008", path=path,
                        message=f"skip job '{job.job_id}' has no job-level if: consuming {per_job} (or {needle}), so trust never skips it",
                        fix=f"add `{per_job} != 'true'` to its if:")]
    out: list[Finding] = []
    for producer in producers:
        backing = workflows.job(workflow, producer)
        if backing is None or not backing.runs_verify:
            what = "no such job" if backing is None else "that job never runs `local-gate verify`"
            out.append(Finding(rule="GATE-008", path=path, status=Status.VIOLATION,
                               message=f"skip job '{job.job_id}' consumes needs.{producer}.outputs.* but {workflow} has {what}",
                               fix=f"add a job '{producer}' to {workflow} that runs `ci-lint local-gate verify --trust --github-output`"))
    return out


def check_trust_static(trust: TrustConfig | None, *, verify_job: str | None, mirrors: tuple[str, ...],
                       lane_ids: tuple[str, ...], source: str, workflows: WorkflowFacts) -> list[Finding]:
    """GATE-008 static half: every skip job consumes the verify job's
    `trusted` output, non-mirror skip jobs name their covering lanes, and
    the workflow still runs on default-branch pushes (the post-merge catch)."""

    if trust is None or trust.mode == "never":
        return []
    out: list[Finding] = []
    if verify_job is None:
        out.append(Finding(rule="GATE-008", path=source, message="[gate.trust] needs a 'verify' job to decide trust",
                           fix="declare verify = '<workflow>:<job>' (GATE-002)"))
        return out
    verify_id = verify_job.split(":", 1)[1]
    for ref in trust.skip:
        workflow, _, job_id = ref.partition(":")
        path = f".github/workflows/{workflow}"
        job = workflows.job(workflow, job_id)
        if job is None:
            out.append(Finding(rule="GATE-008", path=source, message=f"skip job '{ref}' does not exist",
                               fix=f"point [gate.trust].skip at a real job in {path}"))
            continue
        needle = f"needs.{verify_id}.outputs.{TRUSTED_OUTPUT}"
        per_job = f"needs.{verify_id}.outputs.skip_{job_id}"  # GATE-010 per-job decision
        out.extend(_decision_findings(job, workflow, workflows, needle, per_job, path))
        if ref not in mirrors:
            lanes = trust.lanes_for(ref)
            if lanes is None:
                out.append(Finding(rule="GATE-008", path=source,
                                   message=f"skip job '{ref}' is not a GATE-001 mirror and names no covering lanes",
                                   fix=f"add [gate.trust].covered-by.\"{ref}\" = [<lane ids>] after confirming those lanes run what it runs"))
            else:
                unknown = [lane for lane in lanes if lane not in lane_ids]
                if unknown:
                    out.append(Finding(rule="GATE-008", path=source,
                                       message=f"covered-by for '{ref}' names undeclared lane(s) {', '.join(unknown)}",
                                       fix="name lanes declared under [gate.lanes]"))
        if workflow not in workflows.push_triggered:
            out.append(Finding(rule="GATE-008", path=path, status=Status.VIOLATION,
                               message=f"{workflow} has no push trigger, so a skipped PR run is never re-run after merge",
                               fix="trigger the workflow on pushes to the default branch; that run is the post-merge catch"))
    return out
