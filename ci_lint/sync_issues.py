"""`ci-lint sync-issues`: the fingerprinted issue plan for a fleet scan
(zackees/ci.yml#38; docs/agent-guide.md "Findings and issue lifecycle"),
and, since round M2-42 (zackees/ci.yml#98), `--apply` to actually write it.

One issue per (repository, rule ID, subject), where the subject is the
finding's file (or `repo:<owner/name>` for a settings finding). The
fingerprint is the first 16 hex digits of sha256("<repo>|<rule>|<subject>")
and is embedded in the issue body as `<!-- ci-lint-fingerprint: <fp> -->`,
so a later run finds the same issue again regardless of title edits.

Only `violation` findings are planned; a `needs_review` finding is never
filed (agent-guide.md: "Do not file issues from a single ambiguous run").
Issues are planned in the scanned repository itself, labelled `ci-lint`.

`--dry-run` computes and prints the exact create/update/close actions,
reading existing issues with GET requests, and writes nothing.

`--apply` (round M2-42) actually performs those writes, gated by THREE
independent guards, every one of which must hold:

  1. Per-repository opt-in: `ci.toml`'s `[fleet].sync-issues = true`
     (`ci_lint.schema.FleetConfig`, wired into `RepoScan.sync_issues_opt_in`
     by `ci_lint.fleet.scan_repo`). A repository without this key gets
     zero writes, ever, no matter how many violations it has.
  2. `--max-writes N` (small default): a hard cap on the number of issue
     writes (create + update + close, each counted once) in one run, so a
     bug or a huge fleet scan cannot flood a repository's issue tracker.
  3. The fingerprint marker: `update`/`close` only ever act on an
     `ExistingIssue` that `fetch_existing` already matched by
     `<!-- ci-lint-fingerprint: ... -->`, and `_execute` re-checks that
     marker is present in the body being sent before every PATCH, as a
     second, structural guarantee that this module can never touch an
     issue that was not one of its own.

Idempotency: a rerun against unchanged findings produces `unchanged`
actions only (no writes); `plan_repo`'s body/title comparison against the
fetched `ExistingIssue` is the single source of truth for "nothing changed"
-- `run_apply` never re-creates or re-updates an issue that already matches.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status
from ci_lint.fleet import API_ROOT, FleetReport, RepoScan
from ci_lint.github_api import FetchStatusFn, GitHubApiError, WriteFn

ISSUE_LABEL = "ci-lint"
_FP_RE = re.compile(r"<!--\s*ci-lint-fingerprint:\s*([0-9a-f]{16})\s*-->")


@dataclass(frozen=True)
class ExistingIssue:
    number: int
    title: str
    body: str
    fingerprint: str


@dataclass(frozen=True)
class IssueAction:
    action: str  # "create" | "update" | "unchanged" | "close"
    repo: str
    fingerprint: str
    rule: str
    subject: str
    title: str
    body: str
    number: int | None = None


@dataclass(frozen=True)
class RenderedIssue:
    title: str
    body: str


@dataclass(frozen=True)
class ExistingListing:
    issues: list[ExistingIssue]
    error: str | None = None


@dataclass(frozen=True, order=True)
class _GroupKey:
    rule: str
    subject: str


@dataclass(frozen=True)
class SyncPlan:
    actions: list[IssueAction]
    errors: list[str]


def subject_of(finding: Finding, repo: str) -> str:
    return finding.path or f"repo:{repo}"


def fingerprint(repo: str, rule: str, subject: str) -> str:
    return hashlib.sha256(f"{repo}|{rule}|{subject}".encode("utf-8")).hexdigest()[:16]


def render_issue(repo: str, rule: str, subject: str, findings: list[Finding]) -> RenderedIssue:
    fp = fingerprint(repo, rule, subject)
    title = f"ci-lint {rule}: {subject}"
    lines = [
        f"<!-- ci-lint-fingerprint: {fp} -->",
        f"`ci-lint fleet scan` reports **{rule}** for `{subject}` in `{repo}`.",
        "",
        "Findings:",
    ]
    for f in sorted(findings, key=lambda x: (x.line or 0, x.message)):
        loc = f.location()
        lines.append(f"- `{loc}`: {f.message}")
    lines.extend(["", f"Fix: {findings[0].fix}", "", "Policy: https://github.com/zackees/ci.yml (docs/policy-*.md)."])
    return RenderedIssue(title, "\n".join(lines))


def fetch_existing(fetch_status: FetchStatusFn, token: str, repo: str) -> ExistingListing:
    url = f"{API_ROOT}/repos/{repo}/issues?state=open&labels={ISSUE_LABEL}&per_page=100"
    try:
        status, body = fetch_status(url, token)
    except GitHubApiError as exc:
        return ExistingListing([], f"{repo}: listing issues failed ({exc})")
    if status == 404:
        return ExistingListing([])
    if status != 200 or not isinstance(body, list):
        return ExistingListing([], f"{repo}: listing issues failed (HTTP {status})")
    issues: list[ExistingIssue] = []
    for item in body:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        text = item.get("body")
        text = text if isinstance(text, str) else ""
        match = _FP_RE.search(text)
        number = item.get("number")
        if match is None or not isinstance(number, int):
            continue
        title = item.get("title")
        issues.append(
            ExistingIssue(number=number, title=title if isinstance(title, str) else "", body=text, fingerprint=match.group(1))
        )
    return ExistingListing(issues)


def plan_repo(scan: RepoScan, existing: list[ExistingIssue]) -> list[IssueAction]:
    groups: dict[_GroupKey, list[Finding]] = {}
    for f in scan.findings:
        if f.status != Status.VIOLATION:
            continue
        groups.setdefault(_GroupKey(f.rule, subject_of(f, scan.repo)), []).append(f)
    by_fp = {e.fingerprint: e for e in existing}
    actions: list[IssueAction] = []
    seen: set[str] = set()
    for key, findings in sorted(groups.items()):
        rule, subject = key.rule, key.subject
        fp = fingerprint(scan.repo, rule, subject)
        seen.add(fp)
        rendered = render_issue(scan.repo, rule, subject, findings)
        title, body = rendered.title, rendered.body
        prior = by_fp.get(fp)
        if prior is None:
            kind, number = "create", None
        elif prior.body.strip() == body.strip() and prior.title == title:
            kind, number = "unchanged", prior.number
        else:
            kind, number = "update", prior.number
        actions.append(IssueAction(kind, scan.repo, fp, rule, subject, title, body, number))
    for prior in existing:
        if prior.fingerprint not in seen:
            actions.append(
                IssueAction(
                    "close",
                    scan.repo,
                    prior.fingerprint,
                    "",
                    "",
                    prior.title,
                    "`ci-lint fleet scan` no longer reports this finding.",
                    prior.number,
                )
            )
    return actions


def plan_sync(
    report: FleetReport, fetch_status: FetchStatusFn | None, token: str
) -> SyncPlan:
    """`fetch_status=None` plans offline (every issue is `create`)."""

    actions: list[IssueAction] = []
    errors: list[str] = []
    for scan in report.repos:
        existing: list[ExistingIssue] = []
        if fetch_status is not None:
            listing = fetch_existing(fetch_status, token, scan.repo)
            existing = listing.issues
            if listing.error:
                errors.append(listing.error)
        actions.extend(plan_repo(scan, existing))
    return SyncPlan(actions, errors)


def to_json_list(actions: list[IssueAction]) -> list[JsonValue]:
    return [
        {
            "action": a.action,
            "repo": a.repo,
            "number": a.number,
            "fingerprint": a.fingerprint,
            "rule": a.rule,
            "subject": a.subject,
            "labels": ISSUE_LABEL,
            "title": a.title,
            "body": a.body,
        }
        for a in actions
    ]


def render_text(actions: list[IssueAction], errors: list[str]) -> str:
    counts: dict[str, int] = {}
    for a in actions:
        counts[a.action] = counts.get(a.action, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"
    lines = [f"ci-lint sync-issues --dry-run: {summary} (no writes performed)"]
    for a in actions:
        target = f"{a.repo}#{a.number}" if a.number is not None else a.repo
        lines.extend(["", f"=== WOULD {a.action.upper()} {target} [{a.fingerprint}] label={ISSUE_LABEL}", f"title: {a.title}"])
        if a.action != "unchanged":
            lines.append(a.body)
    if errors:
        lines.extend(["", "errors:"])
        lines.extend(f"  {e}" for e in errors)
    return "\n".join(lines)


# ── --apply (round M2-42, zackees/ci.yml#98) ────────────────────────────────


@dataclass(frozen=True)
class ApplyResult:
    executed: tuple[IssueAction, ...]
    skipped_not_opted_in: tuple[str, ...]  # repo names with violations but no [fleet].sync-issues opt-in
    skipped_cap: tuple[IssueAction, ...]  # actions that would write but hit --max-writes
    errors: tuple[str, ...]


def _execute(write: WriteFn, token: str, action: IssueAction) -> str | None:  # noqa: C901
    """Perform ONE issue write for `action`. Returns None on success, else
    an error string. Never called for `action.action in ("unchanged",)`."""

    try:
        if action.action == "create":
            status, _ = write(
                f"{API_ROOT}/repos/{action.repo}/issues",
                token,
                "POST",
                {"title": action.title, "body": action.body, "labels": [ISSUE_LABEL]},
            )
            if status not in (200, 201):
                return f"{action.repo}: create issue failed (HTTP {status})"
            return None
        if action.number is None:
            return f"{action.repo}: {action.action} action has no issue number"
        if action.action == "update":
            # Structural guard: never PATCH a body that has lost its
            # fingerprint marker (belt-and-suspenders over fetch_existing's
            # own filter -- see module docstring point 3).
            if _FP_RE.search(action.body) is None:
                return f"{action.repo}#{action.number}: refusing update, body has no fingerprint marker"
            status, _ = write(
                f"{API_ROOT}/repos/{action.repo}/issues/{action.number}",
                token,
                "PATCH",
                {"title": action.title, "body": action.body},
            )
            if status != 200:
                return f"{action.repo}#{action.number}: update issue failed (HTTP {status})"
            return None
        if action.action == "close":
            status, _ = write(
                f"{API_ROOT}/repos/{action.repo}/issues/{action.number}/comments",
                token,
                "POST",
                {"body": action.body},
            )
            if status not in (200, 201):
                return f"{action.repo}#{action.number}: close comment failed (HTTP {status})"
            status2, _ = write(
                f"{API_ROOT}/repos/{action.repo}/issues/{action.number}",
                token,
                "PATCH",
                {"state": "closed"},
            )
            if status2 != 200:
                return f"{action.repo}#{action.number}: close failed (HTTP {status2})"
            return None
        return f"{action.repo}: unknown action {action.action!r}"
    except GitHubApiError as exc:
        return f"{action.repo}: {exc}"


def run_apply(
    report: FleetReport,
    fetch_status: FetchStatusFn,
    write: WriteFn,
    token: str,
    max_writes: int,
) -> ApplyResult:
    """Perform the writes `plan_sync` would only print. THREE guards (see
    module docstring): per-repo `[fleet].sync-issues` opt-in, `max_writes`,
    and the fingerprint marker re-check in `_execute`.

    A repository with `sync_issues_opt_in=False` is skipped entirely --
    `fetch_existing` is never even called for it, so an un-opted-in
    repository gets zero API calls from this function, not just zero
    writes."""

    executed: list[IssueAction] = []
    skipped_not_opted_in: list[str] = []
    skipped_cap: list[IssueAction] = []
    errors: list[str] = []
    remaining = max_writes
    for scan in report.repos:
        if not scan.sync_issues_opt_in:
            if any(f.status == Status.VIOLATION for f in scan.findings):
                skipped_not_opted_in.append(scan.repo)
            continue
        listing = fetch_existing(fetch_status, token, scan.repo)
        if listing.error:
            errors.append(listing.error)
            continue
        for action in plan_repo(scan, listing.issues):
            if action.action == "unchanged":
                continue
            if remaining <= 0:
                skipped_cap.append(action)
                continue
            error = _execute(write, token, action)
            if error is not None:
                errors.append(error)
                continue
            executed.append(action)
            remaining -= 1
    return ApplyResult(
        executed=tuple(executed),
        skipped_not_opted_in=tuple(skipped_not_opted_in),
        skipped_cap=tuple(skipped_cap),
        errors=tuple(errors),
    )


def apply_to_json(result: ApplyResult) -> dict[str, JsonValue]:
    return {
        "executed": to_json_list(list(result.executed)),
        "skipped_not_opted_in": list(result.skipped_not_opted_in),
        "skipped_cap": to_json_list(list(result.skipped_cap)),
        "errors": list(result.errors),
    }


def render_apply_text(result: ApplyResult) -> str:
    lines = [
        f"ci-lint sync-issues --apply: {len(result.executed)} written, "
        f"{len(result.skipped_cap)} skipped (cap), "
        f"{len(result.skipped_not_opted_in)} repo(s) skipped (no [fleet].sync-issues opt-in)"
    ]
    for a in result.executed:
        target = f"{a.repo}#{a.number}" if a.number is not None else a.repo
        lines.append(f"  DID {a.action.upper()} {target} [{a.fingerprint}]")
    if result.skipped_not_opted_in:
        lines.append("")
        lines.append("repos with findings but no opt-in (add `[fleet].sync-issues = true` to ci.toml):")
        lines.extend(f"  {r}" for r in result.skipped_not_opted_in)
    if result.skipped_cap:
        lines.append("")
        lines.append(f"skipped by --max-writes cap ({len(result.skipped_cap)} action(s) not applied):")
        for a in result.skipped_cap:
            target = f"{a.repo}#{a.number}" if a.number is not None else a.repo
            lines.append(f"  {a.action.upper()} {target} [{a.fingerprint}]")
    if result.errors:
        lines.append("")
        lines.append("errors:")
        lines.extend(f"  {e}" for e in result.errors)
    return "\n".join(lines)
