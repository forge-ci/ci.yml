"""`ci-lint gate`: the "CI OK" aggregator (round-2A brief, part 2d; round-3A
brief Part 3 adds reuse verification).

Reads `plan.json` (`ci_lint plan`'s own `--out`/`--plan-out` JSON, which
carries `required_jobs` -- see `ci_lint.plan._required_job_ids` for the
job-id convention -- and, from round-3A, `lane_digests`) and `needs.json`
(GitHub Actions' `toJSON(needs)`, verbatim) and decides whether the run is
green:

  - every job in `plan["required_jobs"]` must be present in `needs` with
    `result == "success"`. A `skipped` required job is a failure (TEST-001
    spirit: a skip inside required coverage is not a pass) UNLESS it is a
    verified title-edit reuse -- see "Reuse verification" below.
  - a required job that is simply absent from `needs` (the workflow never
    defined a job with that id, or the gate job's own `needs:` list does
    not include it) is `needs_review`, never silently treated as a pass or
    a hard failure -- the naming convention may not match yet.
  - if `plan["mergeable"]` is false, the run is never green regardless of
    job results.

## Reuse verification (round-3A brief, Part 3)

A required job whose `needs` result is `skipped` is SUCCESS iff:

  1. the plan's own reuse map (`--reuse <reuse.json>`, `ci_lint.reuse`'s
     `ReuseResult.to_json_dict()`) marks the job's lane(s) as reused, AND
  2. when a GitHub token is available (`fetch`/`token`/`repo` all set),
     the referenced job is re-fetched live and confirmed `conclusion ==
     "success"`, its `name` contains the exact bracketed digest
     (`plan["lane_digests"][lane]`) for that lane, and (when `head_sha` is
     known) the referenced job's own `head_sha` matches.

`fast` and `dylint` map to the reuse map's own `"fast"`/`"dylint"` keys
1:1. `platform-build`/`platform-run` are matrix aggregates over every
selected platform lane (GitHub folds every matrix leg into one
`needs.<id>.result`), so they verify as reused only when **every**
currently-selected `"platform:<id>"` lane (read from `plan["lane_digests"]`)
is itself reused -- i.e. exactly when the plan's `platform_lanes_todo_json`
would be empty.

Without a token, a job the plan marks reused but that cannot be live-
verified is `needs_review` (exit non-zero) -- never a silent pass (round-3A
brief, Part 3: "without a token (local), a reused-but-unverified job is
needs_review"). A job the plan does *not* mark reused, or that fails live
verification (wrong digest, wrong head SHA, not `success`), is the
pre-existing TEST-001 failure -- a skip is not a pass.

## Default-branch reuse verification (`--default-branch-reuse`, GEN-021 phase 2)

A SECOND, unrelated reuse mechanism, added by zackees/ci.yml#158. It must not
be confused with the title-edit reuse above: title-edit reuse re-runs a *lane
of the same PR* from a prior attempt of that PR's own head, whereas
default-branch reuse skips a job on a *main push* because a pull_request run
already proved a tree-identical tree. Different trigger, different evidence,
different proof -- hence the separate flag, the separate parse, and the
separate verifier rather than an extra branch inside `_verify_reuse`.

The document is `ci-lint reuse-check --out`'s schema-1 JSON
(`ci_lint.default_branch_reuse.to_json_dict`). It is accepted only when it
says `reuse == true` for `sha == $GITHUB_SHA` on a `push` event -- a document
that says anything else is not evidence of anything, and every skipped job
falls back to the pre-existing "a skip is not a pass" failure.

A required job whose `needs` result is `skipped` is SUCCESS iff:

  1. the document lists a job whose display name is one of this job's
     candidate names -- the job id itself, or `<job id> [<lane digest>]`
     for each digest in `plan["lane_digests"]` (the plan-derived naming
     `required-jobs = "plan"` produces), AND
  2. a live `GET /repos/{r}/actions/jobs/{job_id}` still returns
     `conclusion == "success"` and `head_sha == proving_sha` (the PR head, or the pushed commit for
     merge_group evidence; `pr_head_sha` when the document predates the
     field).

Without a token, an otherwise-matching job is `needs_review` -- never a
silent pass, exactly as for title-edit reuse.
pre-existing TEST-001 failure -- a skip is not a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ci_lint.cargo_messages import JsonValue
from ci_lint.attestations import JobDecision
from ci_lint.default_branch_reuse import SCHEMA_VERSION as REUSE_DOC_SCHEMA
from ci_lint.finding import Finding, Status
from ci_lint.github_api import FetchFn, GitHubApiError
from ci_lint.workflow_job_names import WorkflowJobNames

_TAG_REMOVES_RE = re.compile(r"tag '\[(?P<tag>[^\]]+)\]'.*removes")

# round-3A brief, Part 3: platform-build/platform-run are matrix
# aggregates, so their reuse coverage is every currently-selected
# "platform:<id>" lane, not a single reuse-map key of their own.
_MATRIX_JOB_IDS: frozenset[str] = frozenset({"platform-build", "platform-run"})


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ReuseVerdict:
    """The outcome of checking one required job's skip against the plan's
    reuse map (and, when possible, live GitHub state).

      - "ok": a verified reuse -- the job counts as success.
      - "needs_review": the plan marks it reused but it could not be (or
        was not) live-verified -- never silently a pass.
      - "fail": not marked reused, or verification failed outright -- the
        pre-existing "a skip is a failure" behavior.
    """

    status: str
    run_id: int | None


def _lane_keys_for_job(job_id: str, lane_digests: dict[str, JsonValue]) -> list[str]:
    if job_id in ("fast", "dylint"):
        return [job_id]
    if job_id in _MATRIX_JOB_IDS:
        return sorted(k for k in lane_digests if k.startswith("platform:"))
    return []


def _verify_reuse(  # noqa: C901
    job_id: str,
    plan: dict[str, JsonValue],
    reuse: dict[str, JsonValue] | None,
    head_sha: str | None,
    fetch: FetchFn | None,
    token: str | None,
    repo: str | None,
) -> ReuseVerdict:
    if reuse is None:
        return ReuseVerdict(status="fail", run_id=None)

    lane_digests_raw = plan.get("lane_digests")
    lane_digests = lane_digests_raw if isinstance(lane_digests_raw, dict) else {}
    lane_keys = _lane_keys_for_job(job_id, lane_digests)
    if not lane_keys:
        # A job with no lane mapping (unknown job id, or a matrix job
        # whose plan selected zero platform lanes -- which should never
        # itself be "required" in that case) is never treated as reused.
        return ReuseVerdict(status="fail", run_id=None)

    entries: list[tuple[str, dict[str, JsonValue], str]] = []
    for lane in lane_keys:
        raw_entry = reuse.get(lane)
        digest = lane_digests.get(lane)
        if not isinstance(raw_entry, dict) or not isinstance(digest, str):
            return ReuseVerdict(status="fail", run_id=None)
        entries.append((lane, raw_entry, digest))

    first_run_id_raw = entries[0][1].get("run_id")
    first_run_id = first_run_id_raw if isinstance(first_run_id_raw, int) else None

    if fetch is None or token is None or repo is None:
        return ReuseVerdict(status="needs_review", run_id=first_run_id)

    for _lane, entry, digest in entries:
        run_id = entry.get("run_id")
        job_id_ref = entry.get("job_id")
        if not isinstance(run_id, int) or not isinstance(job_id_ref, int):
            return ReuseVerdict(status="fail", run_id=first_run_id)
        try:
            job_payload = fetch(f"https://api.github.com/repos/{repo}/actions/jobs/{job_id_ref}", token)
        except GitHubApiError:
            # The API being unreachable to VERIFY a claimed reuse is not
            # the same as it being down for the plan step -- treat it as
            # unverifiable, not as proof the reuse is bogus.
            return ReuseVerdict(status="needs_review", run_id=first_run_id)
        job = job_payload if isinstance(job_payload, dict) else {}
        name = job.get("name")
        conclusion = job.get("conclusion")
        job_head_sha = job.get("head_sha")
        if conclusion != "success":
            return ReuseVerdict(status="fail", run_id=first_run_id)
        if not isinstance(name, str) or f"[{digest}]" not in name:
            return ReuseVerdict(status="fail", run_id=first_run_id)
        if head_sha is not None and isinstance(job_head_sha, str) and job_head_sha != head_sha:
            return ReuseVerdict(status="fail", run_id=first_run_id)

    return ReuseVerdict(status="ok", run_id=first_run_id)


@dataclass(frozen=True)
class DefaultBranchReuseJob:
    """One proving job the `reuse-check` document carries: its display name
    (what the gate matches against a plan-derived job id) and the numeric
    GitHub job id the gate re-fetches live to re-verify the proof."""

    name: str
    job_id: int | None


@dataclass(frozen=True)
class DefaultBranchReuse:
    """A validated `reuse-check.json` (schema 1). Only reachable once
    `parse_default_branch_reuse` has confirmed `reuse == true`, the right
    schema, the right `sha`, and a `push` event -- an unvalidated document
    never becomes one of these."""

    pr: int | None
    pr_head_sha: str | None
    # Where the proving jobs ran: `proving_sha` when the document carries it
    # (merge_group evidence runs on the pushed commit), else the PR head.
    proving_sha: str | None
    run_id: int | None
    run_url: str | None
    tree: str | None
    jobs: tuple[DefaultBranchReuseJob, ...]

    def job_for(self, job_id: str, digests: tuple[str, ...]) -> DefaultBranchReuseJob | None:
        """The proving job that belongs to `job_id`, if any.

        Plan-derived display names are not one fixed shape. They come as
        the bare id (`fast`), the digest-qualified form (`fast [abc123]`),
        and the matrix form carrying the platform, its host, and its tool
        (`platform-build (linux-x64) (on ubuntu-24.04, soldr) [def456]`),
        whose middle this gate cannot reconstruct from the plan.

        So match on the two halves that ARE decidable here: the name must
        belong to `job_id`, and it must carry a lane digest this plan
        actually selected. The digest is the mechanical "same tier" proof,
        so a differently-tiered or stale lane cannot match even when the
        job id is right. Nothing else is accepted."""

        for job in self.jobs:
            name = job.name
            if name == job_id:
                return job
            if not name.endswith("]"):
                continue
            if not (name.startswith(f"{job_id} [") or name.startswith(f"{job_id} (")):
                continue
            if any(name.endswith(f"[{digest}]") for digest in digests):
                return job
        return None


@dataclass(frozen=True)
class ParsedDefaultBranchReuse:
    """The outcome of validating a `reuse-check.json`: the usable document,
    or `None` plus the reason it is not one. A record, not a tuple (PY-002):
    the reason is only meaningful alongside the verdict."""

    reuse: DefaultBranchReuse | None
    reason: str

    @property
    def usable(self) -> bool:
        return self.reuse is not None


def _reuse_doc_jobs(raw_jobs: JsonValue) -> tuple[DefaultBranchReuseJob, ...] | None:
    """The document's `jobs[]`, or None when it is not an array at all.

    Entries that are not objects, or carry no usable display name, are
    dropped rather than trusted: a proving job the gate cannot name is a
    proving job it cannot match, and must not become a catch-all."""

    if not isinstance(raw_jobs, list):
        return None
    jobs: list[DefaultBranchReuseJob] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        job_id = raw.get("job_id")
        jobs.append(
            DefaultBranchReuseJob(name=name, job_id=job_id if isinstance(job_id, int) else None)
        )
    return tuple(jobs)


def parse_default_branch_reuse(doc: JsonValue, *, sha: str | None) -> ParsedDefaultBranchReuse:
    """Validate a `reuse-check.json` document for gate consumption.

    Returns an unusable result when the document is not evidence -- a wrong
    schema, `reuse != true`, a `sha` that is not this push, or an event that
    is not a push. `sha` is the gate's own `$GITHUB_SHA`; when it is unknown
    (a local run) the `sha` equality is not checked, because a document that
    proved *some* tree is still not evidence about *this* run -- the live
    re-verification below is what actually carries the proof, and it is keyed
    on the document's own `pr_head_sha`.
    """

    def bad(reason: str) -> ParsedDefaultBranchReuse:
        return ParsedDefaultBranchReuse(reuse=None, reason=reason)

    if not isinstance(doc, dict):
        return bad("document is not a JSON object")
    if doc.get("schema") != REUSE_DOC_SCHEMA:
        return bad(f"schema is {doc.get('schema')!r}, expected {REUSE_DOC_SCHEMA}")
    if doc.get("reuse") is not True:
        # `would_reuse` without `reuse` is exactly the shadow-mode document:
        # it describes what *could* be skipped, and must never skip anything.
        return bad(f"document says reuse={doc.get('reuse')!r} (shadow mode never reuses)")
    if doc.get("event_name") != "push":
        return bad(f"event_name is {doc.get('event_name')!r}, expected 'push'")
    doc_sha = doc.get("sha")
    if sha is not None and doc_sha != sha:
        return bad(f"document proves sha {doc_sha!r}, this run is {sha!r}")

    jobs = _reuse_doc_jobs(doc.get("jobs"))
    if jobs is None:
        return bad("document has no 'jobs' array")
    if not jobs:
        return bad("document lists no proving job")

    pr = doc.get("pr")
    run_id = doc.get("run_id")
    pr_head_sha = doc.get("pr_head_sha") if isinstance(doc.get("pr_head_sha"), str) else None
    proving_sha = doc.get("proving_sha") if isinstance(doc.get("proving_sha"), str) else pr_head_sha
    return ParsedDefaultBranchReuse(
        reuse=DefaultBranchReuse(
            pr=pr if isinstance(pr, int) else None,
            pr_head_sha=pr_head_sha,
            proving_sha=proving_sha,
            run_id=run_id if isinstance(run_id, int) else None,
            run_url=doc.get("run_url") if isinstance(doc.get("run_url"), str) else None,
            tree=doc.get("tree") if isinstance(doc.get("tree"), str) else None,
            jobs=jobs,
        ),
        reason="verified",
    )


def _default_branch_lane_digests(plan: dict[str, JsonValue]) -> tuple[str, ...]:
    """Every lane digest this plan selected.

    Used only to decide whether a proving job's display name carries a
    digest the current run actually selected -- the "same tier" proof. A
    repository whose display names are unrelated to the plan's lanes simply
    matches nothing, which is a `fail` (a skip is not a pass), the safe
    direction."""

    digests_raw = plan.get("lane_digests")
    if not isinstance(digests_raw, dict):
        return ()
    return tuple(d for d in digests_raw.values() if isinstance(d, str) and d)


def _default_branch_proving_jobs(
    job_id: str, plan: dict[str, JsonValue], reuse: DefaultBranchReuse,
    workflow_jobs: WorkflowJobNames | None,
) -> tuple[DefaultBranchReuseJob, ...]:
    if workflow_jobs is None:
        job = reuse.job_for(job_id, _default_branch_lane_digests(plan))
        return (job,) if job is not None else ()
    if not reuse.proving_sha or reuse.run_id is None:
        return ()
    try:
        names = workflow_jobs.for_job(job_id)
    except ValueError:
        return ()
    selected: list[DefaultBranchReuseJob] = []
    for name in names:
        matches = [job for job in reuse.jobs if job.name == name]
        if len(matches) != 1:
            return ()
        selected.append(matches[0])
    if len({job.job_id for job in selected}) != len(selected):
        return ()
    return tuple(selected)


def _verify_default_branch_job(
    job: DefaultBranchReuseJob, reuse: DefaultBranchReuse, fetch: FetchFn,
    token: str, repo: str, *, native: bool,
) -> ReuseVerdict:
    if job.job_id is None:
        return ReuseVerdict(status="needs_review", run_id=reuse.run_id)
    try:
        job_payload = fetch(f"https://api.github.com/repos/{repo}/actions/jobs/{job.job_id}", token)
    except GitHubApiError:
        return ReuseVerdict(status="needs_review", run_id=reuse.run_id)
    payload = job_payload if isinstance(job_payload, dict) else {}
    if payload.get("conclusion") != "success" or payload.get("name") != job.name:
        return ReuseVerdict(status="fail", run_id=reuse.run_id)
    if reuse.proving_sha is not None and payload.get("head_sha") != reuse.proving_sha:
        return ReuseVerdict(status="fail", run_id=reuse.run_id)
    if native and payload.get("run_id") != reuse.run_id:
        return ReuseVerdict(status="fail", run_id=reuse.run_id)
    return ReuseVerdict(status="ok", run_id=reuse.run_id)


def _verify_default_branch_reuse(
    job_id: str,
    plan: dict[str, JsonValue],
    reuse: DefaultBranchReuse | None,
    fetch: FetchFn | None,
    token: str | None,
    repo: str | None,
    workflow_jobs: WorkflowJobNames | None = None,
) -> ReuseVerdict:
    if reuse is None:
        return ReuseVerdict(status="fail", run_id=None)
    jobs = _default_branch_proving_jobs(job_id, plan, reuse, workflow_jobs)
    if not jobs:
        return ReuseVerdict(status="fail", run_id=None)
    if fetch is None or token is None or repo is None:
        return ReuseVerdict(status="needs_review", run_id=reuse.run_id)
    for job in jobs:
        verdict = _verify_default_branch_job(job, reuse, fetch, token, repo, native=workflow_jobs is not None)
        if verdict.status != "ok":
            return verdict
    return ReuseVerdict(status="ok", run_id=reuse.run_id)


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    result: str | None  # None: absent from needs.json entirely
    ok: bool
    reused_from_run: int | None = None
    reused_from_pr: int | None = None
    locally_attested: bool = False
    queue_reused_from_pr: int | None = None  # #419 clause 5: merge_group skip proven by a PR-head attestation
    queue_tree: str | None = None


@dataclass(frozen=True)
class QueueReuse:
    """#419 clause 5: the aggregator's own re-derivation of the merge-queue
    trust decision (`ci_lint.merge_queue_trust`) for a `merge_group` run.

    `jobs` are the skip decisions it re-derived (only `skip=True` entries
    credit a skip); `unverifiable` is set when the GET-only re-derivation
    could not read GitHub, which makes every skipped required job
    `needs_review` instead of a pass. A re-derived decision that does not
    trust the entry leaves `jobs` empty, so a skip is the TEST-001 failure."""

    jobs: tuple[JobDecision, ...]
    pr: int | None = None
    tree: str | None = None
    unverifiable: str | None = None


def _queue_skip(job_id: str, result: str, queue: QueueReuse) -> _SkipOutcome | None:
    if any(item.job == job_id and item.skip for item in queue.jobs):
        return _SkipOutcome(JobStatus(job_id, result, True, queue_reused_from_pr=queue.pr,
                                      queue_tree=queue.tree), ())
    if queue.unverifiable is None:
        return None
    return _SkipOutcome(
        JobStatus(job_id, result, False),
        (Finding(
            rule="TEST-001",
            status=Status.NEEDS_REVIEW,
            path=job_id,
            message=f"required job '{job_id}' is skipped on a merge_group run, but the queue trust "
            f"decision could not be re-derived ({queue.unverifiable}) -- treated as needs_review, "
            "never a silent pass",
            fix="run 'ci-lint gate' where GITHUB_TOKEN can read the repository and the PR head commit "
            "is fetched, or re-run the queue entry so the job is not skipped",
        ),),
    )


@dataclass(frozen=True)
class GateReport:
    required_jobs: tuple[str, ...]
    statuses: tuple[JobStatus, ...]
    findings: tuple[Finding, ...]
    mergeable: bool
    not_mergeable_message: str | None
    ok: bool


def _removing_tags(plan: dict[str, JsonValue]) -> list[str]:
    reasons = plan.get("reasons")
    tags: list[str] = []
    if isinstance(reasons, list):
        for r in reasons:
            if not isinstance(r, str):
                continue
            m = _TAG_REMOVES_RE.search(r)
            if m and m.group("tag") not in tags:
                tags.append(m.group("tag"))
    return sorted(tags)


@dataclass(frozen=True)
class _SkipOutcome:
    """What a skipped required job resolves to: its status, and any finding
    that explains a non-pass. A record, not a tuple (PY-002)."""

    status: JobStatus
    findings: tuple[Finding, ...]


def _unverified_reuse_finding(job_id: str, run_id: int | None, source: str) -> Finding:
    return Finding(
        rule="TEST-001",
        status=Status.NEEDS_REVIEW,
        path=job_id,
        message=f"required job '{job_id}' is skipped and {source} proves it from run {run_id}, "
        "but it could not be verified live (no GITHUB_TOKEN, or the GitHub API was unreachable) -- "
        "treated as needs_review, never a silent pass",
        fix="run 'ci-lint gate' where GITHUB_TOKEN is set (in CI) so the reused job can be "
        "verified live, or re-run the job so it is not skipped",
    )


def _handle_skipped(
    job_id: str,
    plan: dict[str, JsonValue],
    parsed_db_reuse: ParsedDefaultBranchReuse,
    reuse: dict[str, JsonValue] | None,
    head_sha: str | None,
    fetch: FetchFn | None,
    token: str | None,
    repo: str | None,
    workflow_jobs: WorkflowJobNames | None,
) -> _SkipOutcome:
    """Decide what a required job whose `needs` result is `skipped` means.

    Default-branch reuse is tried FIRST: on a main push it is the mechanism
    in play, and the title-edit map is either absent or describes a
    different run entirely. Neither mechanism ever credits a skip the other
    rejected -- they are proofs of different things.
    """

    db_reuse = parsed_db_reuse.reuse
    db_verdict = _verify_default_branch_reuse(job_id, plan, db_reuse, fetch, token, repo, workflow_jobs)
    if db_verdict.status == "ok":
        return _SkipOutcome(
            status=JobStatus(
                job_id=job_id,
                result="skipped",
                ok=True,
                reused_from_run=db_verdict.run_id,
                reused_from_pr=db_reuse.pr if db_reuse else None,
            ),
            findings=(),
        )

    verdict = _verify_reuse(job_id, plan, reuse, head_sha, fetch, token, repo)
    if verdict.status == "ok":
        return _SkipOutcome(
            status=JobStatus(
                job_id=job_id, result="skipped", ok=True, reused_from_run=verdict.run_id
            ),
            findings=(),
        )

    if db_verdict.status == "needs_review":
        return _SkipOutcome(
            status=JobStatus(
                job_id=job_id, result="skipped", ok=False, reused_from_run=db_verdict.run_id
            ),
            findings=(
                _unverified_reuse_finding(job_id, db_verdict.run_id, "the default-branch reuse decision"),
            ),
        )
    if verdict.status == "needs_review":
        return _SkipOutcome(
            status=JobStatus(
                job_id=job_id, result="skipped", ok=False, reused_from_run=verdict.run_id
            ),
            findings=(_unverified_reuse_finding(job_id, verdict.run_id, "the plan's reuse map"),),
        )

    return _SkipOutcome(
        status=JobStatus(job_id=job_id, result="skipped", ok=False),
        findings=(
            Finding(
                rule="TEST-001",
                path=job_id,
                message=f"required job '{job_id}' result is 'skipped' and is not a verified reuse "
                "(a skipped required job counts as a failure unless a default-branch reuse "
                "decision or the plan's reuse map proves it, with a matching digest and head "
                "SHA, live)",
                fix=f"fix job '{job_id}' so it completes with result 'success'; if it was meant to "
                "be reused, check that the reuse document's --required-job names, the plan's "
                "lane_digests, and the head SHA all match the referenced run",
            ),
        ),
    )


def compute_gate(
    plan: dict[str, JsonValue],
    needs: dict[str, JsonValue],
    *,
    reuse: dict[str, JsonValue] | None = None,
    head_sha: str | None = None,
    fetch: FetchFn | None = None,
    token: str | None = None,
    repo: str | None = None,
    default_branch_reuse: JsonValue | None = None,
    push_sha: str | None = None,
    attested_jobs: tuple[JobDecision, ...] = (),
    workflow_jobs: WorkflowJobNames | None = None,
    queue_reuse: QueueReuse | None = None,
) -> GateReport:
    """`reuse`/`head_sha`/`fetch`/`token`/`repo` are all optional and all
    default to "no reuse information available" -- a call with none of
    them behaves exactly as before round-3A (every pre-existing call site
    and test is unaffected). See the module docstring's "Reuse
    verification" section.

    `default_branch_reuse` is the raw `reuse-check.json`; `push_sha` is the
    pushed commit (`$GITHUB_SHA`), which is what that document must have
    proved -- deliberately NOT `head_sha`, which is the pull_request head and
    is a different commit on a different event."""

    parsed_db_reuse = ParsedDefaultBranchReuse(reuse=None, reason="not supplied")
    if default_branch_reuse is not None:
        parsed_db_reuse = parse_default_branch_reuse(default_branch_reuse, sha=push_sha)

    required_raw = plan.get("required_jobs")
    required = tuple(j for j in required_raw if isinstance(j, str)) if isinstance(required_raw, list) else ()
    mergeable = plan.get("mergeable") is not False

    findings: list[Finding] = []
    statuses: list[JobStatus] = []
    for job_id in required:
        entry = _as_dict(needs.get(job_id))
        if not entry:
            findings.append(
                Finding(
                    rule="TEST-001",
                    status=Status.NEEDS_REVIEW,
                    path=job_id,
                    message=f"job '{job_id}' is required by the plan but absent from needs.json",
                    fix=f"ensure ci.yml defines a job with id '{job_id}' and that the gate job's "
                    f"'needs:' list includes '{job_id}'",
                )
            )
            statuses.append(JobStatus(job_id=job_id, result=None, ok=False))
            continue
        result = entry.get("result")
        result_str = result if isinstance(result, str) else "unknown"

        if result_str == "skipped":
            if any(item.job == job_id and item.skip for item in attested_jobs):
                statuses.append(JobStatus(job_id, result_str, True, locally_attested=True))
                continue
            queued = _queue_skip(job_id, result_str, queue_reuse) if queue_reuse is not None else None
            if queued is not None:
                statuses.append(queued.status)
                findings.extend(queued.findings)
                continue
            outcome = _handle_skipped(
                job_id,
                plan,
                parsed_db_reuse,
                reuse,
                head_sha,
                fetch,
                token,
                repo,
                workflow_jobs,
            )
            statuses.append(outcome.status)
            findings.extend(outcome.findings)
            continue

        ok = result_str == "success"
        if not ok:
            findings.append(
                Finding(
                    rule="TEST-001",
                    path=job_id,
                    message=f"required job '{job_id}' result is '{result_str}' "
                    "(a skipped required job counts as a failure)",
                    fix=f"fix job '{job_id}' so it completes with result 'success'; if it is genuinely "
                    "optional, it should not be in the plan's required_jobs",
                )
            )
        statuses.append(JobStatus(job_id=job_id, result=result_str, ok=ok))

    not_mergeable_message: str | None = None
    if not mergeable:
        tags = _removing_tags(plan)
        tag_list = ", ".join(tags) if tags else "unknown"
        not_mergeable_message = f"not mergeable: tag(s) {tag_list} remove required coverage"

    ok = mergeable and all(s.ok for s in statuses)
    return GateReport(
        required_jobs=required,
        statuses=tuple(statuses),
        findings=tuple(findings),
        mergeable=mergeable,
        not_mergeable_message=not_mergeable_message,
        ok=ok,
    )


def to_json_dict(report: GateReport) -> dict[str, object]:
    return {
        "required_jobs": list(report.required_jobs),
        "statuses": [
            {"job_id": s.job_id, "result": s.result, "ok": s.ok, "reused_from_run": s.reused_from_run, "reused_from_pr": s.reused_from_pr,
             "locally_attested": s.locally_attested,
             "queue_reused_from_pr": s.queue_reused_from_pr, "queue_tree": s.queue_tree}
            for s in report.statuses
        ],
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "line": f.line, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
        "mergeable": report.mergeable,
        "not_mergeable_message": report.not_mergeable_message,
        "ok": report.ok,
    }


def render_text(report: GateReport) -> str:
    lines = ["ci-lint gate:", f"{'job':<30} {'result':<12} {'ok':<5} note"]
    for s in report.statuses:
        note = "verified local attestation" if s.locally_attested else ""
        if s.queue_reused_from_pr is not None:
            note = f"reused from PR #{s.queue_reused_from_pr} (attestation) tree {s.queue_tree}"
        if s.reused_from_run is not None:
            note = (
                f"reused from PR #{s.reused_from_pr} run {s.reused_from_run}"
                if s.reused_from_pr is not None
                else f"reused from run {s.reused_from_run}"
            )
        lines.append(f"{s.job_id:<30} {s.result or '(absent)':<12} {'yes' if s.ok else 'no':<5} {note}")
    lines.append("")
    for f in report.findings:
        lines.append(f.render())
    if report.not_mergeable_message:
        lines.append(report.not_mergeable_message)
    lines.append(f"ci-lint gate: {'OK' if report.ok else 'NOT OK'}")
    return "\n".join(lines)
