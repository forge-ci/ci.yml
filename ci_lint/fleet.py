"""`ci-lint fleet scan`: the live, read-only central fleet scanner
(zackees/ci.yml#38, the first slice of docs/agent-guide.md's central-checker
design).

Unlike every other `ci_lint` command, this one never needs a local checkout
or a `ci.toml`: it inventories repositories straight from the GitHub REST
API (GET only -- never PATCH/POST/PUT/DELETE) and runs the checks that do
not require a repository to have adopted `ci.toml` schema 3 yet:

  - FLEET-001: the repository has no `ci.toml` at its root, or it does not
    declare `schema = 3` (adoption tracking for #38).
  - GEN-001 / GEN-008: no `.github/workflows/ci.yml` `pull_request` entry
    point / more than one workflow declaring a `pull_request` trigger.
  - RUN-001: a job's literal `runs-on` label is not a fleet runner label.
  - RUST-014: a SHA-pinned `zackees/setup-soldr` is missing a critical
    release or has drifted too far behind latest (reused verbatim from
    ci_lint.rules.setup_soldr_freshness; the floating `@v0` never fires).
  - SEC-007 / GEN-006: the live settings checks from `ci-lint audit` that
    do not depend on `ci.toml` (reused verbatim from ci_lint.settings_audit;
    a 403 is `needs_review`, never a pass).
  - CACHE-025: a `uses: Swatinem/rust-cache@...` step (reused verbatim from
    ci_lint.rules.swatinem_ban; Rust build caching goes through setup-soldr;
    no exceptions, maintainer decision 2026-10-02).
  - FLEET-002: the repository's Actions cache usage is at or above
    FLEET_CACHE_WARN_FRACTION of GitHub's 10 GiB per-repository limit.
  - FLEET-003 (#159, docs/designs/default-branch-verified-reuse.md Phase 3):
    default-branch push runs take >= FLEET_BURN_SHARE of the recent runs'
    wall time while no workflow makes a `ci-lint reuse-check` decision
    (GEN-021) and there is no live merge queue (a `merge_group` trigger
    plus an active merge-queue ruleset on the default branch). Unreadable
    run history or rulesets is `needs_review`, never a pass.

Every call goes through an injectable `FetchStatusFn`, so the unit tests
replay recorded API responses and never touch the network. Workflow files
are materialized into a temporary directory and parsed by the same
`ci_lint.workflow_scan` loader the precheck uses (PyYAML -> yq ->
needs_review).
"""

from __future__ import annotations

import base64
import json
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status
from ci_lint.github_api import FetchStatusFn, GitHubApiError
from ci_lint.rules.default_branch_skip import _REUSE_CHECK_RE
from ci_lint.rules.setup_soldr_freshness import check_rust_014
from ci_lint.rules.swatinem_ban import check_cache_025
from ci_lint.rules.workflows import FLEET_RUNNERS
from ci_lint.schema import load_ci_toml
from ci_lint.settings_audit import (
    SettingsReply,
    _fetch_branch_protection,
    _fetch_rulesets,
    _has_active_merge_queue,
    check_gen_006,
    check_sec_007,
)
from ci_lint.workflow_scan import as_dict, get_on_section, jobs_of, load_workflows
from ci_lint.yaml_io import LoadStatus

API_ROOT = "https://api.github.com"
DEFAULT_OWNERS: tuple[str, ...] = ("zackees", "FastLED", "TechWatchProject")
FLEET_CACHE_LIMIT_BYTES = 10 * 1024**3
FLEET_CACHE_WARN_FRACTION = 0.8
# FLEET-003: the design doc's Phase 3 bar (clud measured 22%).
FLEET_BURN_SHARE = 0.20
FLEET_BURN_RUNS_PER_PAGE = 100

# Ranking: breadth first -- each distinct violated rule ID weighs
# RULE_WEIGHT, then every individual violation and needs_review finding adds
# 1, so 30 `ubuntu-latest` jobs (one rule) do not outrank six different
# policy gaps. Ties break by repository name.
RULE_WEIGHT = 10



@dataclass(frozen=True)
class RepoRef:
    full_name: str
    default_branch: str
    archived: bool
    fork: bool


@dataclass(frozen=True)
class RepoScan:
    repo: str
    default_branch: str
    ci_toml_schema: int | None  # None = no ci.toml; 0 = present but no/invalid schema
    workflow_count: int
    pr_entrypoints: tuple[str, ...]
    cache_bytes: int | None
    findings: tuple[Finding, ...]
    sync_issues_opt_in: bool = False

    @property
    def violations(self) -> int:
        return sum(1 for f in self.findings if f.status == Status.VIOLATION)

    @property
    def needs_review(self) -> int:
        return sum(1 for f in self.findings if f.status == Status.NEEDS_REVIEW)

    @property
    def score(self) -> int:
        rules = {f.rule for f in self.findings if f.status == Status.VIOLATION}
        return len(rules) * RULE_WEIGHT + self.violations + self.needs_review


@dataclass(frozen=True)
class FleetReport:
    owners: tuple[str, ...]
    repos: tuple[RepoScan, ...]  # ranked: highest score first
    errors: tuple[str, ...]


# ── API helpers ────────────────────────────────────────────────────────────


def _get(fetch_status: FetchStatusFn, token: str, url: str) -> SettingsReply | None:
    try:
        status, body = fetch_status(url, token)
        return SettingsReply(status, body)
    except GitHubApiError:
        return None


@dataclass(frozen=True)
class OwnerListing:
    """`list_owner_repos`'s result: the repositories read so far, plus an
    error message when a page could not be listed."""

    repos: tuple[RepoRef, ...]
    error: str | None


@dataclass(frozen=True)
class FetchedFile:
    """A contents-API read: HTTP status (0 = network failure) and the
    decoded text, or None when unreadable."""

    status: int
    text: str | None


@dataclass(frozen=True)
class CiTomlCheck:
    schema: int | None
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class WorkflowCheck:
    workflow_count: int
    pr_entrypoints: tuple[str, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class CacheUsageCheck:
    cache_bytes: int | None
    findings: tuple[Finding, ...]


def list_owner_repos(fetch_status: FetchStatusFn, token: str, owner: str) -> OwnerListing:
    """Every repository of `owner` (user or org), paginated."""

    repos: list[RepoRef] = []
    page = 1
    while True:
        url = f"{API_ROOT}/users/{owner}/repos?per_page=100&type=owner&page={page}"
        result = _get(fetch_status, token, url)
        if result is None or result.status != 200 or not isinstance(result.body, list):
            status = "network error" if result is None else f"HTTP {result.status}"
            return OwnerListing(tuple(repos), f"{owner}: listing repositories failed ({status})")
        body = result.body
        for item in body:
            ref = _repo_ref(item)
            if ref is not None:
                repos.append(ref)
        if len(body) < 100:
            return OwnerListing(tuple(repos), None)
        page += 1


def _repo_ref(item: object) -> RepoRef | None:
    if not isinstance(item, dict):
        return None
    name = item.get("full_name")
    if not isinstance(name, str):
        return None
    branch = item.get("default_branch")
    return RepoRef(
        full_name=name,
        default_branch=branch if isinstance(branch, str) else "main",
        archived=item.get("archived") is True,
        fork=item.get("fork") is True,
    )


def fetch_repo(fetch_status: FetchStatusFn, token: str, full_name: str) -> RepoRef | None:
    result = _get(fetch_status, token, f"{API_ROOT}/repos/{full_name}")
    if result is None or result.status != 200:
        return None
    return _repo_ref(result.body)


def _fetch_file(fetch_status: FetchStatusFn, token: str, repo: str, path: str) -> FetchedFile:
    """The decoded text or None; status 0 = network failure."""

    result = _get(fetch_status, token, f"{API_ROOT}/repos/{repo}/contents/{path}")
    if result is None:
        return FetchedFile(0, None)
    status, body = result.status, result.body
    if status != 200 or not isinstance(body, dict):
        return FetchedFile(status, None)
    content = body.get("content")
    if not isinstance(content, str) or body.get("encoding") != "base64":
        return FetchedFile(status, None)
    try:
        return FetchedFile(status, base64.b64decode(content).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return FetchedFile(status, None)


# ── checks ─────────────────────────────────────────────────────────────────


def check_ci_toml(repo: str, status: int, text: str | None) -> CiTomlCheck:
    subject = f"repo:{repo}"
    if status == 0:
        return CiTomlCheck(
            None,
            (
            Finding(
                rule="FLEET-001",
                path=subject,
                status=Status.NEEDS_REVIEW,
                message="could not read ci.toml (network error)",
                fix="re-run `ci-lint fleet scan` once the GitHub API is reachable",
            ),
            ),
        )
    if status == 404 or text is None:
        return CiTomlCheck(
            None,
            (
            Finding(
                rule="FLEET-001",
                path=subject,
                message="no ci.toml at the repository root: ci_lint cannot enforce the fleet policy here",
                fix="adopt ci.toml schema 3: start from examples/rust-pypi-app/ci.toml (or the "
                "zackees/template-python-rust-cmd reference), keep only this repository's real "
                "platforms/suites, and run `python3 -m ci_lint precheck --local` until it is clean",
            ),
            ),
        )
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return CiTomlCheck(
            0,
            (
            Finding(
                rule="FLEET-001",
                path="ci.toml",
                message=f"ci.toml does not parse as TOML: {exc}",
                fix="fix the TOML syntax, then run `python3 -m ci_lint precheck --local`",
            ),
            ),
        )
    schema = doc.get("schema")
    if schema != 3:
        return CiTomlCheck(
            (schema if isinstance(schema, int) else 0),
            (
            Finding(
                rule="FLEET-001",
                path="ci.toml",
                message=f"ci.toml declares schema = {schema!r}, not 3",
                fix="migrate ci.toml to schema 3 (docs/ci-toml.md) and set `schema = 3`; the "
                "schema-1 units/groups layout from proposal.md is superseded",
            ),
            ),
        )
    return CiTomlCheck(3, ())


def check_workflows(repo_root: Path) -> WorkflowCheck:
    """GEN-001, GEN-008, RUN-001 over materialized workflow files."""

    findings: list[Finding] = []
    workflows = load_workflows(repo_root)
    pr_entrypoints: list[str] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            findings.append(
                Finding(
                    rule="RUN-001",
                    path=wf.path,
                    status=Status.NEEDS_REVIEW,
                    message=f"workflow could not be parsed: {wf.reason}",
                    fix="install PyYAML (`uv run --no-project --with pyyaml ...`) or yq and re-run",
                )
            )
            continue
        doc = as_dict(wf.document)
        if "pull_request" in get_on_section(doc):
            pr_entrypoints.append(wf.path)
        for job_id, job in jobs_of(doc).items():
            label = job.get("runs-on")
            if isinstance(label, str) and "${{" not in label and label not in FLEET_RUNNERS:
                findings.append(
                    Finding(
                        rule="RUN-001",
                        path=wf.path,
                        message=f"job '{job_id}' runs on '{label}', which is not a fleet runner label",
                        fix=f"use an explicit fleet label from {sorted(FLEET_RUNNERS)} "
                        "(never '-latest', never macos-13)",
                    )
                )
    if ".github/workflows/ci.yml" not in pr_entrypoints:
        unreadable_ci = any(
            workflow.path == ".github/workflows/ci.yml" and workflow.status != LoadStatus.OK
            for workflow in workflows
        )
        findings.append(
            Finding(
                rule="GEN-001",
                path=".github/workflows/ci.yml",
                status=Status.NEEDS_REVIEW if unreadable_ci else Status.VIOLATION,
                message=(
                    "could not determine whether .github/workflows/ci.yml declares on.pull_request "
                    "because the workflow could not be parsed"
                    if unreadable_ci else "no .github/workflows/ci.yml with an on.pull_request trigger"
                ),
                fix=(
                    "parse .github/workflows/ci.yml successfully and re-run `ci-lint fleet scan` "
                    "before assessing its PR trigger"
                    if unreadable_ci else "make .github/workflows/ci.yml the single ordinary-PR entry point "
                    "(on.pull_request), gated by the precheck plan"
                ),
            )
        )
    if len(pr_entrypoints) > 1:
        findings.append(
            Finding(
                rule="GEN-008",
                path=".github/workflows",
                message=f"{len(pr_entrypoints)} workflows declare pull_request: {', '.join(pr_entrypoints)}",
                fix="fold every pull_request workflow into .github/workflows/ci.yml as plan-gated jobs, "
                "then remove the pull_request trigger from the others",
            )
        )
    return WorkflowCheck(len(workflows), tuple(pr_entrypoints), tuple(findings))


def check_cache_usage(fetch_status: FetchStatusFn, token: str, repo: str) -> CacheUsageCheck:
    url = f"{API_ROOT}/repos/{repo}/actions/cache/usage"
    result = _get(fetch_status, token, url)
    if result is None or result.status != 200 or not isinstance(result.body, dict):
        what = "network error" if result is None else f"HTTP {result.status}"
        return CacheUsageCheck(
            None,
            (
            Finding(
                rule="FLEET-002",
                path=f"repo:{repo}",
                status=Status.NEEDS_REVIEW,
                message=f"could not read Actions cache usage ({what})",
                fix="re-run with a token that can read this repository's Actions caches",
            ),
            ),
        )
    size = result.body.get("active_caches_size_in_bytes")
    if not isinstance(size, int):
        return CacheUsageCheck(None, ())
    if size >= FLEET_CACHE_LIMIT_BYTES * FLEET_CACHE_WARN_FRACTION:
        return CacheUsageCheck(
            size,
            (
            Finding(
                rule="FLEET-002",
                path=f"repo:{repo}",
                message=f"Actions cache usage {size / 1024**3:.2f} GiB is at or above "
                f"{int(FLEET_CACHE_WARN_FRACTION * 100)}% of the 10 GiB repository limit (eviction churn)",
                fix="declare a [cache] budget in ci.toml and run `ci-lint cache audit`/`cache janitor` "
                "to remove CACHE-008 entries (closed-PR, stale-base, retired families)",
            ),
            ),
        )
    return CacheUsageCheck(size, ())


# ── FLEET-003: default-branch CI burn ─────────────────────────────────────


@dataclass(frozen=True)
class BurnShare:
    push_seconds: int
    total_seconds: int
    push_runs: int

    @property
    def share(self) -> float:
        return self.push_seconds / self.total_seconds if self.total_seconds else 0.0


def _iso_seconds(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def burn_share(runs: list[object], default_branch: str) -> BurnShare:
    """Wall time (run_started_at -> updated_at, completed runs only) of
    default-branch `push` runs versus every run in the page. Wall time, not
    summed job time: one GET, an approximation of runner share."""

    push = total = push_runs = 0
    for run in runs:
        if not isinstance(run, dict) or run.get("status") != "completed":
            continue
        start, end = _iso_seconds(run.get("run_started_at")), _iso_seconds(run.get("updated_at"))
        if start is None or end is None or end < start:
            continue
        total += end - start
        if run.get("event") == "push" and run.get("head_branch") == default_branch:
            push += end - start
            push_runs += 1
    return BurnShare(push, total, push_runs)


@dataclass(frozen=True)
class WorkflowRelief:
    has_reuse_check: bool  # some workflow runs `ci-lint reuse-check`
    has_merge_group: bool  # some workflow declares a `merge_group` trigger


def workflow_relief(repo_root: Path) -> WorkflowRelief:
    """Default-branch burn relief visible in materialized workflow files."""

    reuse = merge_group = False
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        if "merge_group" in get_on_section(doc):
            merge_group = True
        for job in jobs_of(doc).values():
            steps = job.get("steps")
            for step in steps if isinstance(steps, list) else []:
                run = step.get("run") if isinstance(step, dict) else None
                if isinstance(run, str) and _REUSE_CHECK_RE.search(run):
                    reuse = True
    return WorkflowRelief(reuse, merge_group)


def check_fleet_003(
    fetch_status: FetchStatusFn,
    token: str,
    repo: str,
    default_branch: str,
    has_reuse_check: bool,
    has_merge_group: bool,
    rulesets: SettingsReply | None,
) -> list[Finding]:
    path = f"branch:{default_branch}"
    if has_reuse_check:
        return []
    queue_unknown = rulesets is None or rulesets.status not in (200, 404)
    if has_merge_group and not queue_unknown and rulesets is not None:
        body = rulesets.body if isinstance(rulesets.body, list) else []
        if _has_active_merge_queue(body, default_branch):
            return []
    url = f"{API_ROOT}/repos/{repo}/actions/runs?per_page={FLEET_BURN_RUNS_PER_PAGE}"
    result = _get(fetch_status, token, url)
    if result is None or result.status != 200 or not isinstance(result.body, dict):
        what = "network error" if result is None else f"HTTP {result.status}"
        return [
            Finding(
                rule="FLEET-003",
                path=path,
                status=Status.NEEDS_REVIEW,
                message=f"no reuse-check decision and no proven merge queue, and the run history is unreadable ({what})",
                fix="re-run `ci-lint fleet scan` with a token that can read this repository's Actions runs",
            )
        ]
    runs = result.body.get("workflow_runs")
    burn = burn_share(runs if isinstance(runs, list) else [], default_branch)
    if burn.push_runs == 0 or burn.share < FLEET_BURN_SHARE:
        return []
    pct = f"{burn.share * 100:.0f}%"
    if queue_unknown and has_merge_group:
        return [
            Finding(
                rule="FLEET-003",
                path=path,
                status=Status.NEEDS_REVIEW,
                message=f"default-branch push runs are {pct} of recent CI wall time with no reuse-check "
                "decision; a merge_group trigger exists but the rulesets could not be read",
                fix="re-run with a token that can read rulesets to confirm the merge queue is live",
            )
        ]
    return [
        Finding(
            rule="FLEET-003",
            path=path,
            message=f"default-branch push runs re-run validation and are {pct} of recent CI wall time "
            f"({burn.push_runs} push runs) with no `ci-lint reuse-check` decision and no live merge queue",
            fix="adopt verified default-branch reuse (GEN-021, `[reuse.default-branch]` + `ci-lint reuse-check`, "
            "docs/designs/default-branch-verified-reuse.md) or a merge queue (merge_group trigger + an "
            "active merge-queue ruleset)",
        )
    ]


def scan_repo(fetch_status: FetchStatusFn, token: str, ref: RepoRef) -> RepoScan:
    repo = ref.full_name
    findings: list[Finding] = []
    ci_file = _fetch_file(fetch_status, token, repo, "ci.toml")
    text = ci_file.text
    ci_check = check_ci_toml(repo, ci_file.status, text)
    schema = ci_check.schema
    findings.extend(ci_check.findings)

    workflow_texts: dict[str, str] = {}
    listing = _get(fetch_status, token, f"{API_ROOT}/repos/{repo}/contents/.github/workflows")
    if listing is not None and listing.status == 200 and isinstance(listing.body, list):
        for item in listing.body:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name.endswith((".yml", ".yaml")) and item.get("type") == "file":
                wf_text = _fetch_file(fetch_status, token, repo, f".github/workflows/{name}").text
                if wf_text is not None:
                    workflow_texts[f".github/workflows/{name}"] = wf_text
    elif listing is None:
        findings.append(
            Finding(
                rule="GEN-001",
                path=".github/workflows",
                status=Status.NEEDS_REVIEW,
                message="could not list .github/workflows (network error)",
                fix="re-run `ci-lint fleet scan` once the GitHub API is reachable",
            )
        )

    sync_issues_opt_in = False
    with tempfile.TemporaryDirectory(prefix="ci-lint-fleet-") as tmp:
        root = Path(tmp)
        for rel, wf_text in workflow_texts.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(wf_text, encoding="utf-8")
        wf_check = check_workflows(root)
        findings.extend(wf_check.findings)
        findings.extend(check_rust_014(fetch_status, token, root))
        findings.extend(check_cache_025(root))
        relief = workflow_relief(root)
        if text is not None:
            (root / "ci.toml").write_text(text, encoding="utf-8")
            ci = load_ci_toml(root).ci
            if ci is not None:
                sync_issues_opt_in = ci.fleet.sync_issues

    findings.extend(check_sec_007(fetch_status, token, repo))
    bp = _fetch_branch_protection(fetch_status, token, repo, ref.default_branch)
    rulesets = _fetch_rulesets(fetch_status, token, repo)
    findings.extend(check_gen_006(bp, rulesets, ref.default_branch))
    findings.extend(
        check_fleet_003(
            fetch_status, token, repo, ref.default_branch, relief.has_reuse_check, relief.has_merge_group, rulesets
        )
    )

    cache_check = check_cache_usage(fetch_status, token, repo)
    findings.extend(cache_check.findings)
    return RepoScan(
        repo=repo,
        default_branch=ref.default_branch,
        ci_toml_schema=schema,
        workflow_count=wf_check.workflow_count,
        pr_entrypoints=wf_check.pr_entrypoints,
        cache_bytes=cache_check.cache_bytes,
        findings=tuple(findings),
        sync_issues_opt_in=sync_issues_opt_in,
    )


def run_fleet_scan(
    fetch_status: FetchStatusFn,
    token: str,
    owners: tuple[str, ...] = DEFAULT_OWNERS,
    repos: tuple[str, ...] = (),
    include_forks: bool = False,
) -> FleetReport:
    """`repos` narrows the scan: each entry is `owner/name` or a bare name
    resolved against every owner in `owners` (first match wins)."""

    errors: list[str] = []
    targets: list[RepoRef] = []
    if repos:
        for entry in repos:
            candidates = [entry] if "/" in entry else [f"{o}/{entry}" for o in owners]
            found = None
            for cand in candidates:
                found = fetch_repo(fetch_status, token, cand)
                if found is not None:
                    break
            if found is None:
                errors.append(f"{entry}: repository not found under {', '.join(owners)}")
            else:
                targets.append(found)
    else:
        for owner in owners:
            listing = list_owner_repos(fetch_status, token, owner)
            if listing.error:
                errors.append(listing.error)
            targets.extend(r for r in listing.repos if not r.archived and (include_forks or not r.fork))
    scans = [scan_repo(fetch_status, token, t) for t in targets]
    scans.sort(key=lambda s: (-s.score, s.repo))
    return FleetReport(owners=owners, repos=tuple(scans), errors=tuple(errors))


# ── rendering ──────────────────────────────────────────────────────────────


def finding_to_json(f: Finding) -> dict[str, JsonValue]:
    return {
        "rule": f.rule,
        "status": f.status.value,
        "path": f.path,
        "line": f.line,
        "message": f.message,
        "fix": f.fix,
    }


def to_json_dict(report: FleetReport) -> dict[str, JsonValue]:
    return {
        "owners": list(report.owners),
        "errors": list(report.errors),
        "repos": [
            {
                "rank": i + 1,
                "repo": s.repo,
                "default_branch": s.default_branch,
                "score": s.score,
                "violations": s.violations,
                "needs_review": s.needs_review,
                "ci_toml_schema": s.ci_toml_schema,
                "workflow_count": s.workflow_count,
                "pr_entrypoints": list(s.pr_entrypoints),
                "cache_bytes": s.cache_bytes,
                "sync_issues_opt_in": s.sync_issues_opt_in,
                "findings": [finding_to_json(f) for f in s.findings],
            }
            for i, s in enumerate(report.repos)
        ],
    }


def render_text(report: FleetReport) -> str:
    lines = [
        f"ci-lint fleet scan: {len(report.repos)} repositories ({', '.join(report.owners)}), ranked worst first",
        "",
        "| # | repo | score | viol | review | ci.toml | workflows | PR entrypoints | cache GiB | rules |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, s in enumerate(report.repos, start=1):
        rules = sorted({f.rule for f in s.findings if f.status == Status.VIOLATION})
        schema = "none" if s.ci_toml_schema is None else f"schema {s.ci_toml_schema}"
        cache = "?" if s.cache_bytes is None else f"{s.cache_bytes / 1024**3:.2f}"
        lines.append(
            f"| {i} | {s.repo} | {s.score} | {s.violations} | {s.needs_review} | {schema} | "
            f"{s.workflow_count} | {len(s.pr_entrypoints)} | {cache} | {', '.join(rules) or '-'} |"
        )
    for s in report.repos:
        if not s.findings:
            continue
        lines.extend(["", f"## {s.repo}"])
        lines.extend(f"  {f.render()}" for f in s.findings)
    if report.errors:
        lines.extend(["", "errors:"])
        lines.extend(f"  {e}" for e in report.errors)
    return "\n".join(lines)


def load_report_json(text: str) -> FleetReport:
    """Rebuild a FleetReport from `fleet scan --json` output (sync-issues
    --from-scan input). Raises ValueError on a malformed document."""

    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise ValueError("not a `ci-lint fleet scan --json` document (no 'repos' list)")
    scans: list[RepoScan] = []
    for item in data["repos"]:
        if not isinstance(item, dict) or not isinstance(item.get("repo"), str):
            raise ValueError("malformed repos[] entry")
        findings: list[Finding] = []
        for fd in item.get("findings") or []:
            if not isinstance(fd, dict):
                raise ValueError("malformed findings[] entry")
            line = fd.get("line")
            findings.append(
                Finding(
                    rule=str(fd.get("rule")),
                    message=str(fd.get("message")),
                    fix=str(fd.get("fix")),
                    path=fd.get("path") if isinstance(fd.get("path"), str) else None,
                    line=line if isinstance(line, int) else None,
                    status=Status(str(fd.get("status"))),
                )
            )
        schema = item.get("ci_toml_schema")
        cache = item.get("cache_bytes")
        prs = item.get("pr_entrypoints")
        scans.append(
            RepoScan(
                repo=item["repo"],
                default_branch=str(item.get("default_branch") or "main"),
                ci_toml_schema=schema if isinstance(schema, int) else None,
                workflow_count=int(item.get("workflow_count") or 0),
                pr_entrypoints=tuple(p for p in prs if isinstance(p, str)) if isinstance(prs, list) else (),
                cache_bytes=cache if isinstance(cache, int) else None,
                findings=tuple(findings),
                sync_issues_opt_in=item.get("sync_issues_opt_in") is True,
            )
        )
    owners = data.get("owners")
    errs = data.get("errors")
    return FleetReport(
        owners=tuple(o for o in owners if isinstance(o, str)) if isinstance(owners, list) else (),
        repos=tuple(scans),
        errors=tuple(e for e in errs if isinstance(e, str)) if isinstance(errs, list) else (),
    )
