"""`ci-lint reuse-check` / `ci-lint reuse-report`: verified reuse on
default-branch pushes (GEN-021, zackees/ci.yml#156; design:
docs/designs/default-branch-verified-reuse.md).

A default-branch push run may skip a required job only when this module can
PROVE, from GitHub's own API data, that the pushed tree was already
validated by a pull request:

  1. Tree identity: exactly one merged PR is associated with the pushed
     commit (`merged_at` set, `base.ref` = the default branch,
     `merge_commit_sha` = the pushed SHA -- GitHub documents that field as
     the squash commit, the merge commit, or the rebase tip), its head is in
     this repository, and the pushed commit's tree SHA equals the PR head
     commit's tree SHA.
  2. Per-job proof: for every named workflow, the newest decisive
     `pull_request` run on that head (cancelled/skipped/stale runs carry no
     signal and are passed over; a newer failed or unfinished run
     disqualifies) concluded `success`, and every caller-listed required job
     name matches a job of the selected run(s) whose conclusion is `success`
     -- not skipped, cancelled, or neutral. An iteration-mode run that
     skipped the Linux lanes therefore never qualifies.
  3. Freshness: every proving job completed within `max_age_hours` before
     the evaluation clock (and not after it -- `reuse-report` evaluates past
     pushes with the clock set to each push run's creation time).
  4. Fail closed: any API error, rate limit, missing or mistyped field, or
     ambiguity yields "no reuse" with a reason code; nothing here raises
     out of `decide`.

This is a distinct mechanism from `ci_lint.reuse` (round-3A title-edit
reuse on the SAME pull_request head, keyed by lane digests), whose behavior
is unchanged. Every GitHub call is a GET through an injectable
`FetchStatusFn` (`ci_lint.github_api`), so the unit tests replay recorded
responses (`ci-lint reuse-check --record/--replay`) and never touch the
network.
"""

from __future__ import annotations

import json

import re
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import FetchStatusFn, GitHubApiError

API_ROOT = "https://api.github.com"
SCHEMA_VERSION = 1
DEFAULT_MAX_AGE_HOURS = 24.0
# A single decisive run with a verified safe skip is enough to promote.
# The per-run decision is sound on its own terms -- one merged PR, an
# identical tree, a green and fresh proving run -- so requiring a shadow
# *quota* before trusting it bought confidence in the tool rather than
# soundness in the decision, and permanently locked low-traffic
# repositories out of the mechanism entirely. Raise it with `--min-runs N`
# when staging a rollout deliberately.
DEFAULT_MIN_RUNS = 1
PER_PAGE = 100
# Pagination cap for one listing (runs on a head SHA, jobs of a run, push
# runs in a report page walk). Hitting it fails closed rather than
# evaluating a truncated listing.
MAX_PAGES = 3

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Run conclusions that carry no pass/fail signal about the tree: a newer run
# with one of these is passed over when picking the newest decisive run.
NO_SIGNAL_CONCLUSIONS: frozenset[str] = frozenset({"cancelled", "skipped", "stale"})

VERIFIED = "verified"
# Every reason code `decide` can return. Documented one by one in
# docs/designs/default-branch-verified-reuse.md ("Fail-closed table") and
# docs/ci-toml.md; `test_default_branch_reuse` asserts the two stay aligned.
REASONS: tuple[str, ...] = (
    VERIFIED,
    "event-not-push",
    "ref-not-default-branch",
    "no-token",
    "api-error",
    "rate-limited",
    "api-malformed",
    "no-associated-pr",
    "ambiguous-pr",
    "fork-head",
    "tree-mismatch",
    "too-many-runs",
    "run-in-progress",
    "newest-run-not-success",
    "no-successful-run",
    "fork-run",
    "too-many-jobs",
    "required-job-missing",
    "ambiguous-required-job",
    "required-job-not-success",
    "stale-run",
    "run-after-decision",
)


def normalize_workflow(value: str) -> str:
    """`ci.yml` and `.github/workflows/ci.yml` both name the same file; a
    run's `path` field is always the repo-relative form."""

    value = value.strip()
    if value.startswith("./"):
        value = value[2:]
    return value if "/" in value else f".github/workflows/{value}"


def parse_time(value: JsonValue) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ReuseRequest:
    repo: str
    sha: str
    workflows: tuple[str, ...]  # normalized `.github/workflows/<file>` paths
    required_jobs: tuple[str, ...]
    now: datetime
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS
    mode: str = "enforce"  # "enforce" | "shadow"
    default_branch: str = "main"
    event_name: str = "push"
    ref: str = "refs/heads/main"
    # Also accept a green merge_group run on the pushed commit itself when the
    # PR head's proof fails (a queue merges a PR that was behind the default
    # branch, so the merged tree differs from the head's). Opt-in.
    queue_evidence: bool = False


@dataclass(frozen=True)
class RunEvidence:
    workflow: str
    run_id: int
    run_url: str
    run_attempt: int
    created_at: str


@dataclass(frozen=True)
class JobEvidence:
    name: str
    job_id: int
    run_id: int
    conclusion: str
    completed_at: str
    html_url: str


@dataclass(frozen=True)
class ReuseDecision:
    request: ReuseRequest
    verdict: bool  # the proof held (independent of mode)
    reason: str
    detail: str
    pr: int | None = None
    pr_head_sha: str | None = None
    tree: str | None = None
    runs: tuple[RunEvidence, ...] = ()
    jobs: tuple[JobEvidence, ...] = ()
    api_calls: int = 0

    @property
    def reuse(self) -> bool:
        """What the workflow acts on: only ever true in enforce mode."""

        return self.verdict and self.request.mode == "enforce"

    @property
    def would_reuse(self) -> bool:
        return self.verdict


class _Stop(Exception):
    """Internal: a fail-closed branch. Carries the reason code + detail."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class _Api:
    fetch_status: FetchStatusFn
    token: str
    calls: int = 0
    cache: dict[str, tuple[int, JsonValue]] = field(default_factory=dict)

    def get(self, path: str) -> JsonValue:
        """GET `API_ROOT + path`; any non-200, transport failure, or rate
        limit raises `_Stop` (fail closed). Identical URLs are served from a
        per-process cache (`reuse-report` evaluates many pushes that share
        PR heads)."""

        url = f"{API_ROOT}{path}"
        cached = self.cache.get(url)
        if cached is None:
            self.calls += 1
            try:
                cached = self.fetch_status(url, self.token)
            except GitHubApiError as exc:
                raise _Stop("api-error", f"GET {path} failed: {exc}") from exc
            self.cache[url] = cached
        status, body = cached
        if status == 200:
            return body
        message = body.get("message") if isinstance(body, dict) else None
        if status == 429 or (status == 403 and isinstance(message, str) and "rate limit" in message.lower()):
            raise _Stop("rate-limited", f"GET {path} -> HTTP {status} (rate limited)")
        raise _Stop("api-error", f"GET {path} -> HTTP {status}" + (f": {message}" if isinstance(message, str) else ""))


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _paged(api: _Api, path: str, key: str, too_many: str) -> list[JsonValue]:
    """Walk `?per_page=100&page=N` until `total_count` is covered; more than
    MAX_PAGES pages is `too_many` (fail closed, never a truncated read)."""

    items: list[JsonValue] = []
    sep = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        body = _as_dict(api.get(f"{path}{sep}per_page={PER_PAGE}&page={page}"))
        batch = body.get(key)
        total = _int(body.get("total_count"))
        if not isinstance(batch, list) or total is None:
            raise _Stop("api-malformed", f"GET {path}: no '{key}' list / 'total_count'")
        items.extend(batch)
        if len(items) >= total or len(batch) < PER_PAGE:
            return items
    raise _Stop(too_many, f"GET {path}: more than {MAX_PAGES * PER_PAGE} entries")


def _tree_of(api: _Api, repo: str, sha: str) -> str:
    body = _as_dict(api.get(f"/repos/{repo}/git/commits/{sha}"))
    tree = _str(_as_dict(body.get("tree")).get("sha"))
    if tree is None or not SHA_RE.match(tree):
        raise _Stop("api-malformed", f"git/commits/{sha}: no 40-hex tree.sha")
    return tree


def _associated_pr(api: _Api, req: ReuseRequest) -> tuple[int, str]:
    pulls = api.get(f"/repos/{req.repo}/commits/{req.sha}/pulls?per_page={PER_PAGE}")
    if not isinstance(pulls, list):
        raise _Stop("api-malformed", f"commits/{req.sha}/pulls: not a list")
    matches: list[dict[str, JsonValue]] = []
    for raw in pulls:
        pr = _as_dict(raw)
        if (
            _str(pr.get("merged_at"))
            and _str(_as_dict(pr.get("base")).get("ref")) == req.default_branch
            and _str(pr.get("merge_commit_sha")) == req.sha
        ):
            matches.append(pr)
    if not matches:
        raise _Stop(
            "no-associated-pr",
            f"{len(pulls)} PR(s) associated with {req.sha[:12]}, none merged into "
            f"'{req.default_branch}' with merge_commit_sha == the pushed SHA (direct push, or the "
            "association has not been indexed yet)",
        )
    if len(matches) > 1:
        nums = ", ".join(f"#{_int(p.get('number'))}" for p in matches)
        raise _Stop("ambiguous-pr", f"{len(matches)} merged PRs map to {req.sha[:12]}: {nums}")
    pr = matches[0]
    number = _int(pr.get("number"))
    head = _as_dict(pr.get("head"))
    head_sha = _str(head.get("sha"))
    if number is None or head_sha is None or not SHA_RE.match(head_sha):
        raise _Stop("api-malformed", "associated PR has no number / 40-hex head.sha")
    head_repo = _str(_as_dict(head.get("repo")).get("full_name"))
    if head_repo is None or head_repo.lower() != req.repo.lower():
        raise _Stop(
            "fork-head",
            f"PR #{number}'s head is in '{head_repo or '<deleted repository>'}', not '{req.repo}' -- a fork "
            "head's CI ran with a restricted token/secrets and is never reused",
        )
    return number, head_sha


def _select_run(  # noqa: C901
    runs: list[dict[str, JsonValue]],
    workflow: str,
    req: ReuseRequest,
    head_sha: str,
    event: str = "pull_request",
) -> dict[str, JsonValue]:
    candidates: list[tuple[datetime, int, dict[str, JsonValue]]] = []
    for run in runs:
        # The listing is already filtered by head_sha/event server-side;
        # re-check both so a mis-filtered response can never widen the proof.
        if _str(run.get("path")) != workflow or _str(run.get("head_sha")) != head_sha:
            continue
        if _str(run.get("event")) != event:
            continue
        created = parse_time(run.get("created_at"))
        run_id = _int(run.get("id"))
        if created is None or run_id is None:
            raise _Stop("api-malformed", f"a {workflow} run has no created_at/id")
        if created > req.now:
            continue  # did not exist yet at the evaluation clock
        candidates.append((created, run_id, run))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    for _created, run_id, run in candidates:
        status = _str(run.get("status"))
        conclusion = _str(run.get("conclusion"))
        if status != "completed":
            raise _Stop(
                "run-in-progress",
                f"{workflow} {event} run {run_id} on {head_sha[:12]} is '{status}': its outcome is unknown",
            )
        if conclusion in NO_SIGNAL_CONCLUSIONS:
            continue
        if conclusion != "success":
            raise _Stop(
                "newest-run-not-success",
                f"the newest decisive {workflow} {event} run on {head_sha[:12]} ({run_id}) concluded '{conclusion}'",
            )
        head_repo = _str(_as_dict(run.get("head_repository")).get("full_name"))
        if head_repo is None or head_repo.lower() != req.repo.lower():
            raise _Stop("fork-run", f"{workflow} run {run_id} ran for '{head_repo}', not '{req.repo}'")
        return run
    raise _Stop("no-successful-run", f"no completed, successful {workflow} {event} run on {head_sha[:12]}")


def _check_jobs(api: _Api, req: ReuseRequest, runs: list[RunEvidence]) -> tuple[JobEvidence, ...]:  # noqa: C901
    by_name: dict[str, list[JobEvidence]] = {}
    for run in runs:
        for raw in _paged(api, f"/repos/{req.repo}/actions/runs/{run.run_id}/jobs?filter=latest", "jobs", "too-many-jobs"):
            job = _as_dict(raw)
            name = _str(job.get("name"))
            job_id = _int(job.get("id"))
            if name is None or job_id is None:
                raise _Stop("api-malformed", f"run {run.run_id}: a job has no name/id")
            conclusion = _str(job.get("conclusion")) if _str(job.get("status")) == "completed" else None
            by_name.setdefault(name, []).append(
                JobEvidence(
                    name=name,
                    job_id=job_id,
                    run_id=run.run_id,
                    conclusion=conclusion or f"<{_str(job.get('status')) or 'unknown'}>",
                    completed_at=_str(job.get("completed_at")) or "",
                    html_url=_str(job.get("html_url")) or "",
                )
            )
    proven: list[JobEvidence] = []
    oldest_ok = req.now - timedelta(hours=req.max_age_hours)
    for name in req.required_jobs:
        matches = by_name.get(name)
        if not matches:
            raise _Stop(
                "required-job-missing",
                f"required job '{name}' is not a job of the proving run(s) (renamed, matrix/reusable-workflow "
                "display name mismatch, or a different CI tier)",
            )
        not_ok = [j for j in matches if j.conclusion != "success"]
        if not_ok and len(matches) > 1:
            # A display name that maps to more than one job cannot say WHICH
            # job proved what. soldr's `ci.yml` has two jobs both named `Lint`
            # (`lint-docs`, a deliberate no-op for docs-only and attested
            # heads per soldr#3318, and the real `lint`); the no-op skips by
            # design, so the generic `required-job-not-success` sent the
            # reader hunting in the test suite instead of at the duplicate
            # name. Still fail-closed -- this is the same refusal, named.
            detail = ", ".join(
                f"id {j.job_id} concluded '{j.conclusion}'" for j in matches
            )
            raise _Stop(
                "ambiguous-required-job",
                f"required job name '{name}' matches {len(matches)} different jobs in the proving "
                f"run ({detail}); give each a distinct display name so the proof names one job",
            )
        for job in matches:
            if job.conclusion != "success":
                raise _Stop("required-job-not-success", f"required job '{name}' concluded '{job.conclusion}'")
            completed = parse_time(job.completed_at)
            if completed is None:
                raise _Stop("api-malformed", f"required job '{name}' has no completed_at")
            if completed > req.now:
                raise _Stop(
                    "run-after-decision",
                    f"required job '{name}' completed at {job.completed_at}, after the evaluation clock "
                    f"{_iso(req.now)}",
                )
            if completed < oldest_ok:
                raise _Stop(
                    "stale-run",
                    f"required job '{name}' completed at {job.completed_at}, more than "
                    f"{req.max_age_hours:g} h before {_iso(req.now)}",
                )
            proven.append(job)
    return tuple(proven)


def decide(req: ReuseRequest, fetch_status: FetchStatusFn | None, token: str | None) -> ReuseDecision:
    """Never raises for API/data problems: every one is a fail-closed
    `ReuseDecision(verdict=False, reason=...)`. Checks run cheapest first;
    the typical verified decision costs 5 GET calls."""

    api = _Api(fetch_status, token) if fetch_status is not None and token else None
    return _decide(req, api)


# PR-head proof failures a green merge_group run on the pushed commit answers.
QUEUE_FALLBACK_REASONS = frozenset({"tree-mismatch", "no-successful-run"})


def _run_evidence(api: _Api, req: ReuseRequest, sha: str, event: str) -> list[RunEvidence]:
    runs = [
        _as_dict(r)
        for r in _paged(
            api,
            f"/repos/{req.repo}/actions/runs?head_sha={sha}&event={event}",
            "workflow_runs",
            "too-many-runs",
        )
    ]
    evidence: list[RunEvidence] = []
    for workflow in req.workflows:
        run = _select_run(runs, workflow, req, sha, event)
        evidence.append(
            RunEvidence(
                workflow=workflow,
                run_id=_int(run.get("id")) or 0,
                run_url=_str(run.get("html_url")) or "",
                run_attempt=_int(run.get("run_attempt")) or 1,
                created_at=_str(run.get("created_at")) or "",
            )
        )
    return evidence


def _pr_head_evidence(api: _Api, req: ReuseRequest, pr: int, head_sha: str, tree: str) -> list[RunEvidence]:
    head_tree = _tree_of(api, req.repo, head_sha)
    if head_tree != tree:
        raise _Stop(
            "tree-mismatch",
            f"pushed tree {tree[:12]} != PR #{pr} head {head_sha[:12]}'s tree {head_tree[:12]}: the merged "
            "tree was never validated by the PR",
        )
    return _run_evidence(api, req, head_sha, "pull_request")


def _decide(req: ReuseRequest, api: _Api | None) -> ReuseDecision:
    if req.event_name != "push":
        return ReuseDecision(req, False, "event-not-push", f"event '{req.event_name or '<unset>'}' is not a push")
    if req.ref != f"refs/heads/{req.default_branch}":
        return ReuseDecision(
            req, False, "ref-not-default-branch", f"ref '{req.ref or '<unset>'}' is not refs/heads/{req.default_branch}"
        )
    if api is None:
        return ReuseDecision(req, False, "no-token", "no GITHUB_TOKEN/GH_TOKEN: reuse cannot be verified")

    start = api.calls
    pr: int | None = None
    head_sha: str | None = None
    tree: str | None = None
    evidence: list[RunEvidence] = []
    try:
        pr, head_sha = _associated_pr(api, req)
        tree = _tree_of(api, req.repo, req.sha)
        proof = f"tree {tree[:12]} == PR #{pr} head {head_sha[:12]}"
        try:
            evidence = _pr_head_evidence(api, req, pr, head_sha, tree)
        except _Stop as head_stop:
            if not req.queue_evidence or head_stop.reason not in QUEUE_FALLBACK_REASONS:
                raise
            # The queue tested this exact commit, so its tree is proven too.
            try:
                evidence = _run_evidence(api, req, req.sha, "merge_group")
            except _Stop as queue_stop:
                if queue_stop.reason == "no-successful-run":
                    raise head_stop from None  # no queue run: the PR-head reason stands
                raise
            proof = f"merge_group run on {req.sha[:12]} itself (PR #{pr}; {head_stop.reason} on the head)"
        jobs = _check_jobs(api, req, evidence)
    except _Stop as stop:
        return ReuseDecision(
            req, False, stop.reason, stop.detail, pr, head_sha, tree, tuple(evidence), (), api.calls - start
        )
    detail = (
        f"{proof}; "
        f"{len(jobs)} required job(s) green in run(s) {', '.join(str(r.run_id) for r in evidence)}"
    )
    return ReuseDecision(req, True, VERIFIED, detail, pr, head_sha, tree, tuple(evidence), jobs, api.calls - start)


# ── rendering ──────────────────────────────────────────────────────────────


def to_json_dict(d: ReuseDecision) -> dict[str, JsonValue]:
    """`--json`/`--out` document, schema 1 (docs/ci-toml.md "reuse-check")."""

    first = d.runs[0] if d.runs else None
    return {
        "schema": SCHEMA_VERSION,
        "command": "reuse-check",
        "repo": d.request.repo,
        "sha": d.request.sha,
        "default_branch": d.request.default_branch,
        "event_name": d.request.event_name,
        "ref": d.request.ref,
        "mode": d.request.mode,
        "reuse": d.reuse,
        "would_reuse": d.would_reuse,
        "reason": d.reason,
        "detail": d.detail,
        "pr": d.pr,
        "pr_head_sha": d.pr_head_sha,
        "tree": d.tree,
        "run_id": first.run_id if first else None,
        "run_url": first.run_url if first else None,
        "runs": [
            {
                "workflow": r.workflow,
                "run_id": r.run_id,
                "run_url": r.run_url,
                "run_attempt": r.run_attempt,
                "created_at": r.created_at,
            }
            for r in d.runs
        ],
        "workflows": list(d.request.workflows),
        "required_jobs": list(d.request.required_jobs),
        "jobs": [
            {
                "name": j.name,
                "job_id": j.job_id,
                "run_id": j.run_id,
                "conclusion": j.conclusion,
                "completed_at": j.completed_at,
                "html_url": j.html_url,
            }
            for j in d.jobs
        ],
        "max_age_hours": d.request.max_age_hours,
        "evaluated_at": _iso(d.request.now),
        "api_calls": d.api_calls,
    }


def github_output_lines(d: ReuseDecision) -> list[str]:
    first = d.runs[0] if d.runs else None
    return [
        f"reuse={'true' if d.reuse else 'false'}",
        f"would_reuse={'true' if d.would_reuse else 'false'}",
        f"reason={d.reason}",
        f"pr={d.pr if d.pr is not None else ''}",
        f"run_id={first.run_id if first else ''}",
        f"run_url={first.run_url if first else ''}",
        f"tree={d.tree or ''}",
        "evidence_json=" + json.dumps(to_json_dict(d), separators=(",", ":")),
    ]


def render_text(d: ReuseDecision) -> str:
    lines = [
        f"ci-lint reuse-check: reuse={'true' if d.reuse else 'false'} "
        f"would_reuse={'true' if d.would_reuse else 'false'} mode={d.request.mode} reason={d.reason}",
        f"  {d.detail}",
    ]
    if d.pr is not None:
        lines.append(f"  pr: #{d.pr} head {d.pr_head_sha or '?'} tree {d.tree or '?'}")
    for r in d.runs:
        lines.append(f"  run: {r.run_url or r.run_id} ({r.workflow}, attempt {r.run_attempt})")
    for j in d.jobs:
        lines.append(f"  proven: {j.name} ({j.conclusion}, completed {j.completed_at})")
    lines.append(f"  api calls: {d.api_calls}")
    return "\n".join(lines)


def render_step_summary(d: ReuseDecision) -> str:
    """The provenance block appended to `$GITHUB_STEP_SUMMARY` (design
    section "Aggregator semantics"): a green default-branch commit is never
    an unexplained skip."""

    first = d.runs[0] if d.runs else None
    if d.reuse:
        head = "### Verified reuse: validation jobs skipped"
    elif d.would_reuse:
        head = "### Verified reuse (shadow): would have skipped; everything ran"
    else:
        head = "### No reuse: every job runs"
    lines = [head, "", f"- reason: `{d.reason}` -- {d.detail}"]
    if d.pr is not None:
        lines.append(f"- source PR: #{d.pr} (head `{d.pr_head_sha}`)")
    if d.tree:
        lines.append(f"- tree: `{d.tree}`")
    if first is not None:
        lines.append(f"- proving run: {first.run_url or first.run_id} (attempt {first.run_attempt})")
    lines.append(f"- mode: `{d.request.mode}`, max age {d.request.max_age_hours:g} h, {d.api_calls} API call(s)")
    return "\n".join(lines) + "\n"


# ── reuse-report (shadow-mode promotion evidence) ─────────────────────────


@dataclass(frozen=True)
class ReportRow:
    run_id: int
    run_url: str
    sha: str
    created_at: str
    conclusion: str
    run_attempt: int
    outcome: str  # safe-skip | false-reuse-candidate | accepted-flaky | must-run | no-signal
    decision: ReuseDecision


@dataclass(frozen=True)
class ReuseReport:
    repo: str
    workflow: str
    since: str
    until: str | None
    min_runs: int
    rows: tuple[ReportRow, ...]
    api_calls: int
    error: str | None = None

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.rows if r.outcome == outcome)

    @property
    def decisive(self) -> int:
        return sum(1 for r in self.rows if r.outcome != "no-signal")

    @property
    def verdict(self) -> str:
        if self.error is not None:
            return "error"
        if self.count("false-reuse-candidate"):
            return "blocked"
        if self.decisive < self.min_runs:
            return "insufficient-sample"
        if not self.count("safe-skip"):
            # Decisive runs happened but reuse never actually fired on one
            # (every push was a must-run: a squash-merge batch, a drifted
            # tree, a failed proving job). That is not evidence the
            # mechanism works -- it is evidence it has never been used, so
            # there is nothing to promote.
            return "no-op"
        return "promotable"


_FAILED_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out", "startup_failure"})


def _failed_job_names(api: _Api, repo: str, run_id: int) -> frozenset[str]:
    """Display names of the jobs that did not succeed in a run.

    Used only to attribute a red push run to a job, so the report can tell
    "reuse would have lost coverage" from "an unrelated job failed anyway".
    """

    names: set[str] = set()
    for raw in _paged(api, f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest", "jobs", "too-many-jobs"):
        job = _as_dict(raw)
        name = _str(job.get("name"))
        if name is None:
            continue
        if _str(job.get("conclusion")) not in ("success", "skipped", "neutral", ""):
            names.add(name)
    return frozenset(names)


def _outcome(
    verdict: bool,
    conclusion: str,
    run_id: int,
    accepted: frozenset[int],
    reuse_would_skip: frozenset[str] = frozenset(),
    failed: frozenset[str] = frozenset(),
) -> str:
    if conclusion not in _FAILED_CONCLUSIONS and conclusion != "success":
        return "no-signal"
    if not verdict:
        return "must-run"
    if conclusion == "success":
        return "safe-skip"
    # A red push run is only evidence against reuse if reuse would have
    # skipped one of the jobs that actually failed. A job that keeps running
    # under reuse -- a cache-retirement sweep, a housekeeping step, an
    # unrelated matrix leg -- fails identically whether or not the decision
    # reused, so it says nothing about the decision. Counting those made a
    # repository with one flaky maintenance job look permanently unsafe.
    #
    # Only downgrade when the failing jobs are KNOWN and none of them is
    # skippable. An unreadable or empty job list means the run is
    # unattributable, and unattributable stays a candidate: a decision
    # nobody can place against the evidence is not a decision to trust.
    if reuse_would_skip and failed and not (reuse_would_skip & failed):
        return "must-run"
    return "accepted-flaky" if run_id in accepted else "false-reuse-candidate"


def run_report(
    fetch_status: FetchStatusFn,
    token: str,
    template: ReuseRequest,
    since: str,
    until: str | None = None,
    *,
    min_runs: int = DEFAULT_MIN_RUNS,
    limit: int = 300,
    accept_flaky: frozenset[int] = frozenset(),
) -> ReuseReport:
    """Re-evaluate every completed default-branch push run of the first
    workflow since `since` (retroactive shadow mode): for each, `decide` runs
    with the clock set to that run's creation time, and the verdict is
    compared with what the push run actually concluded. `template` supplies
    repo/workflows/required jobs/max age; its sha/now/mode are replaced per
    run. GET only; one shared URL cache across all runs."""

    workflow = template.workflows[0]
    basename = workflow.rsplit("/", 1)[-1]
    created = f">={since}" if until is None else f"{since}..{until}"
    base = (
        f"/repos/{template.repo}/actions/workflows/{urllib.parse.quote(basename)}/runs"
        f"?event=push&branch={urllib.parse.quote(template.default_branch)}&status=completed"
        f"&created={urllib.parse.quote(created)}"
    )
    api = _Api(fetch_status, token)
    runs: list[dict[str, JsonValue]] = []
    try:
        page = 1
        while len(runs) < limit:
            body = _as_dict(api.get(f"{base}&per_page={PER_PAGE}&page={page}"))
            batch = body.get("workflow_runs")
            if not isinstance(batch, list):
                raise _Stop("api-malformed", "workflow runs listing has no 'workflow_runs' list")
            runs.extend(_as_dict(r) for r in batch)
            if len(batch) < PER_PAGE:
                break
            page += 1
    except _Stop as stop:
        return ReuseReport(template.repo, workflow, since, until, min_runs, (), api.calls, f"{stop.reason}: {stop.detail}")

    rows: list[ReportRow] = []
    for run in runs[:limit]:
        run_id = _int(run.get("id"))
        sha = _str(run.get("head_sha"))
        created_at = parse_time(run.get("created_at"))
        if run_id is None or sha is None or created_at is None or _str(run.get("head_branch")) != template.default_branch:
            continue
        req = replace(template, sha=sha, now=created_at, mode="shadow")
        decision = _decide(req, api)
        conclusion = _str(run.get("conclusion")) or "unknown"
        failed: frozenset[str] = frozenset()
        reuse_would_skip: frozenset[str] = frozenset()
        if conclusion != "success":
            # Only red runs need attribution; a green one is already a
            # safe skip or a must-run on the verdict alone.
            try:
                failed = _failed_job_names(api, template.repo, run_id)
            except _Stop:
                failed = frozenset()
            reuse_would_skip = frozenset(j.name for j in decision.jobs)
        rows.append(
            ReportRow(
                run_id=run_id,
                run_url=_str(run.get("html_url")) or "",
                sha=sha,
                created_at=_iso(created_at),
                conclusion=conclusion,
                run_attempt=_int(run.get("run_attempt")) or 1,
                outcome=_outcome(decision.verdict, conclusion, run_id, accept_flaky, reuse_would_skip, failed),
                decision=decision,
            )
        )
    return ReuseReport(template.repo, workflow, since, until, min_runs, tuple(rows), api.calls)


def report_to_json_dict(report: ReuseReport) -> dict[str, JsonValue]:
    reasons: dict[str, JsonValue] = {}
    for row in report.rows:
        if row.outcome == "must-run":
            current = reasons.get(row.decision.reason)
            reasons[row.decision.reason] = (current if isinstance(current, int) else 0) + 1
    would = sum(1 for r in report.rows if r.outcome != "no-signal" and r.decision.verdict)
    return {
        "schema": SCHEMA_VERSION,
        "command": "reuse-report",
        "repo": report.repo,
        "workflow": report.workflow,
        "since": report.since,
        "until": report.until,
        "verdict": report.verdict,
        "error": report.error,
        "min_runs": report.min_runs,
        "decisive_runs": report.decisive,
        "would_reuse": would,
        "reuse_rate": round(would / report.decisive, 4) if report.decisive else 0.0,
        "safe_skip": report.count("safe-skip"),
        "false_reuse_candidates": [r.run_url or str(r.run_id) for r in report.rows if r.outcome == "false-reuse-candidate"],
        "accepted_flaky": [r.run_url or str(r.run_id) for r in report.rows if r.outcome == "accepted-flaky"],
        "no_signal": report.count("no-signal"),
        "must_run_reasons": reasons,
        "api_calls": report.api_calls,
        "rows": [
            {
                "run_id": r.run_id,
                "run_url": r.run_url,
                "sha": r.sha,
                "created_at": r.created_at,
                "conclusion": r.conclusion,
                "run_attempt": r.run_attempt,
                "outcome": r.outcome,
                "would_reuse": r.decision.verdict,
                "reason": r.decision.reason,
                "pr": r.decision.pr,
                "proving_run_id": r.decision.runs[0].run_id if r.decision.runs else None,
            }
            for r in report.rows
        ],
    }


def render_report_text(report: ReuseReport) -> str:
    doc = report_to_json_dict(report)
    lines = [
        f"ci-lint reuse-report: {report.repo} {report.workflow} push runs since {report.since}"
        + (f" until {report.until}" if report.until else ""),
        "",
        "| run | created | conclusion | attempt | would reuse | reason | PR | outcome |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report.rows:
        lines.append(
            f"| {r.run_id} | {r.created_at} | {r.conclusion} | {r.run_attempt} | "
            f"{'yes' if r.decision.verdict else 'no'} | {r.decision.reason} | "
            f"{'#' + str(r.decision.pr) if r.decision.pr is not None else '-'} | {r.outcome} |"
        )
    lines.extend(
        [
            "",
            f"decisive runs: {doc['decisive_runs']} (no-signal: {doc['no_signal']}), would reuse: "
            f"{doc['would_reuse']} (rate {doc['reuse_rate']}), safe skips: {doc['safe_skip']}",
            f"false-reuse candidates (analyse each; pass --accept-flaky <run id> only for a proven flake): "
            f"{len(_as_list(doc['false_reuse_candidates']))}",
        ]
    )
    for url in _as_list(doc["false_reuse_candidates"]):
        lines.append(f"  {url}")
    if report.error:
        lines.append(f"error: {report.error}")
    lines.append(f"api calls: {report.api_calls}")
    lines.append(
        f"verdict: {report.verdict} (promotion needs >= {report.min_runs} decisive run(s), at "
        "least one safe skip, and 0 false-reuse candidates)"
    )
    return "\n".join(lines)
