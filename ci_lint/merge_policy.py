"""GEN-034 rollout: auto-merge is a *policed* capability, not a free switch.

`ci-lint audit` reports GEN-034 per repository (see
`ci_lint.settings_audit.check_gen_034`). This module is the fleet-side half:
it enumerates repositories by owner, decides the same policy for each one, and
-- only with `--apply` -- enables `allow_auto_merge` on the repositories where
the decision is safe because required status checks already police the branch.

Two verdicts matter:

  - `unpoliced`  : `allow_auto_merge` is on with no required status check.
                   A pull request can merge itself with no CI verdict. `--apply`
                   turns auto-merge **off** here: the setting must never be a
                   licence to merge unverified code, and requiring a gate is a
                   policy decision this tool does not make for the repository.
  - `enable`     : required status checks gate the default branch, but
                   `allow_auto_merge` is off and no merge queue covers the
                   branch. Every verified PR still needs a human merge click,
                   which stalls an unattended fleet. With policed checks
                   already in place, enabling auto-merge cannot merge
                   unverified code, so `--apply` enables it (admin PATCH).

Repositories with no required checks at all and auto-merge already off are
reported `no-checks` and left alone -- enabling auto-merge there would create
the `unpoliced` state.

Reads and the single write go through injectable functions, so tests never
touch the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, cast

from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import FetchStatusFn, HttpResult, WriteFn
from ci_lint.settings_audit import (
    API_ROOT,
    RepoFacts,
    SettingsReply,
    _fetch_rulesets,
    _has_active_merge_queue,
    _required_contexts,
    _ruleset_payloads,
    fetch_repo_facts,
)

RULE = "GEN-034"

# The rollout reads and writes through the status-carrying client shape
# (`HttpResult`), which is what `ci_lint.github_api.default_fetch_status`/
# `default_write` return. `settings_audit`'s helpers take the unpackable
# `FetchStatusFn` protocol; `HttpResult` is iterable, so the casts below are
# only about the annotation, never about behaviour.
HttpFetchFn = Callable[[str, str], HttpResult]
HttpWriteFn = Callable[[str, str, str, "dict[str, JsonValue]"], HttpResult]

# Verdicts whose remedy is a settings change this tool may make.
APPLICABLE = ("enable", "unpoliced")


@dataclass(frozen=True)
class RepoPolicy:
    """One repository's merge-automation verdict."""

    full_name: str
    default_branch: str
    archived: bool
    fork: bool
    allow_auto_merge: bool | None
    required_contexts: tuple[str, ...]
    queue: bool
    verdict: str
    detail: str

    @property
    def actionable(self) -> bool:
        return self.verdict in APPLICABLE


@dataclass(frozen=True)
class OwnerListing:
    owner: str
    full_names: tuple[str, ...]
    error: str | None


@dataclass(frozen=True)
class VerdictCount:
    verdict: str
    count: int


@dataclass(frozen=True)
class Failure:
    repo: str
    error: str


@dataclass(frozen=True)
class PageResult:
    names: tuple[str, ...]
    error: str | None


@dataclass(frozen=True)
class RolloutReport:
    policies: tuple[RepoPolicy, ...]
    applied: tuple[str, ...]
    failures: tuple[Failure, ...]
    dry_run: bool

    def verdict_counts(self) -> tuple[VerdictCount, ...]:
        counts: dict[str, int] = {}
        for policy in self.policies:
            counts[policy.verdict] = counts.get(policy.verdict, 0) + 1
        return tuple(VerdictCount(verdict, counts[verdict]) for verdict in sorted(counts))


def _get(fetch_status: HttpFetchFn, token: str, url: str) -> SettingsReply | None:
    from ci_lint.settings_audit import _get as settings_get

    return settings_get(cast(FetchStatusFn, fetch_status), token, url)


def _page_repos(fetch_status: HttpFetchFn, token: str, base: str) -> PageResult:
    """Every non-fork, non-archived `full_name` under one listing endpoint.

    `PageResult.error` is `"404"` when the endpoint does not apply to this
    owner (the caller then tries the other endpoint); any other value is a
    real failure that must be reported.
    """

    names: list[str] = []
    page = 1
    while True:
        reply = _get(fetch_status, token, f"{base}&per_page=100&page={page}")
        if reply is None:
            return PageResult(tuple(names), "listing repositories failed (network error)")
        if reply.status == 404:
            return PageResult(tuple(names), "404")
        if reply.status != 200 or not isinstance(reply.body, list):
            return PageResult(tuple(names), f"listing repositories failed (HTTP {reply.status})")
        body = reply.body
        for item in body:
            if not isinstance(item, dict):
                continue
            name = item.get("full_name")
            if isinstance(name, str) and not item.get("fork") and not item.get("archived"):
                names.append(name)
        if len(body) < 100:
            return PageResult(tuple(names), None)
        page += 1


def list_owner_repos(fetch_status: HttpFetchFn, token: str, owner: str) -> OwnerListing:
    """Every non-fork, non-archived repository of a user or an organization.

    An organization is read through `/orgs/{owner}/repos` (which includes
    private repositories the token can see); a user through
    `/users/{owner}/repos`. A 404 on the organization endpoint falls back to
    the user endpoint so the command works for both without a type lookup.
    """

    for base in (f"{API_ROOT}/orgs/{owner}/repos?type=all", f"{API_ROOT}/users/{owner}/repos?type=owner"):
        page = _page_repos(fetch_status, token, base)
        if page.error != "404":
            message = None if page.error is None else f"{owner}: {page.error}"
            return OwnerListing(owner=owner, full_names=page.names, error=message)
    return OwnerListing(owner=owner, full_names=(), error=f"could not list repositories for {owner}")


def _protection(fetch_status: HttpFetchFn, token: str, full_name: str, branch: str) -> SettingsReply | None:
    return _get(fetch_status, token, f"{API_ROOT}/repos/{full_name}/branches/{branch}/protection")


def _rulesets(fetch_status: HttpFetchFn, token: str, full_name: str) -> SettingsReply | None:
    """Rulesets with their full detail.

    The list endpoint's summary objects carry no `rules`/`conditions`, so
    reusing `settings_audit._fetch_rulesets` (which expands each ruleset by
    id) is what makes the queue/required-check verdicts real. Reading the
    bare list here once reported every gated repository as queue-less.
    """

    return _fetch_rulesets(fetch_status, token, full_name)


def evaluate(
    full_name: str,
    default_branch: str,
    facts: RepoFacts | None,
    protection: SettingsReply | None,
    rulesets: SettingsReply | None,
    *,
    archived: bool = False,
    fork: bool = False,
) -> RepoPolicy:
    """The GEN-034 verdict for one repository (pure; no network)."""

    contexts = tuple(sorted(_required_contexts(protection, _ruleset_payloads(rulesets), default_branch)))
    queue = _has_active_merge_queue(_ruleset_payloads(rulesets), default_branch)
    allow = facts.allow_auto_merge if facts is not None else None
    if facts is None or allow is None:
        return RepoPolicy(full_name, default_branch, archived, fork, allow, contexts, queue, "unreadable",
                          "the repository payload carries no allow_auto_merge (admin-only on private "
                          "repositories); never treated as policed")
    if allow and not contexts:
        return RepoPolicy(full_name, default_branch, archived, fork, allow, contexts, queue, "unpoliced",
                          "allow_auto_merge is on but the default branch requires no status check")
    if queue:
        return RepoPolicy(full_name, default_branch, archived, fork, allow, contexts, queue, "policed-by-queue",
                          "an active merge queue with required checks is the merge path")
    if not contexts:
        return RepoPolicy(full_name, default_branch, archived, fork, allow, contexts, queue, "no-checks",
                          "no required status check on the default branch; auto-merge must stay off")
    if allow:
        return RepoPolicy(full_name, default_branch, archived, fork, allow, contexts, queue, "policed",
                          "auto-merge is on and policed by required checks")
    return RepoPolicy(full_name, default_branch, archived, fork, allow, contexts, queue, "enable",
                      "required checks exist but auto-merge is off: a green PR cannot land unattended")


def _set_auto_merge(write: HttpWriteFn, token: str, full_name: str, value: bool) -> HttpResult:
    """PATCH one repository's `allow_auto_merge`; the caller checks the status."""

    return write(f"{API_ROOT}/repos/{full_name}", token, "PATCH", {"allow_auto_merge": value})


def rollout(
    fetch_status: HttpFetchFn,
    *,
    token: str,
    owners: tuple[str, ...],
    repos: tuple[str, ...],
    apply: bool,
    write: HttpWriteFn | None = None,
) -> RolloutReport:
    """Evaluate every repository of `owners` (plus explicit `repos`).

    `apply` polices both directions: `enable` verdicts get
    `allow_auto_merge=true` (required checks already gate them, so this cannot
    merge unverified code) and `unpoliced` verdicts get
    `allow_auto_merge=false` (auto-merge is on with nothing checking it --
    that is the hazard this rule exists to remove). The function re-reads
    every patched repository, so the report states what GitHub now says,
    never what the PATCH was asked to do.
    """

    targets: list[str] = list(repos)
    for owner in owners:
        listing = list_owner_repos(fetch_status, token, owner)
        if listing.error is not None:
            raise RolloutError(listing.error)
        targets.extend(name for name in listing.full_names if name not in targets)

    policies: list[RepoPolicy] = []
    applied: list[str] = []
    failures: list[Failure] = []
    for full_name in targets:
        payload = fetch_repo_facts(cast(FetchStatusFn, fetch_status), token, full_name)
        if payload.reply is not None and payload.reply.status in (403, 429) and _rate_limited(payload.reply.body):
            raise RolloutError(
                f"GitHub API rate limit exhausted while reading {full_name}; re-run after the reset window"
            )
        facts = payload.facts
        # One GET of /repos/{repo} answers both questions: the settings and the
        # default branch the rulesets are matched against.
        branch = (facts.default_branch if facts is not None else None) or "main"
        protection = _protection(fetch_status, token, full_name, branch)
        rulesets = _rulesets(fetch_status, token, full_name)
        policy = evaluate(full_name, branch, facts, protection, rulesets)
        if apply and policy.actionable and write is not None:
            value = policy.verdict == "enable"
            try:
                patched = _set_auto_merge(write, token, full_name, value)
            except Exception as exc:  # noqa: BLE001 - reported per repository, never swallowed
                failures.append(Failure(full_name, str(exc)))
                policies.append(policy)
                continue
            if patched.status not in (200, 201):
                body = patched.body
                detail = body.get("message") if isinstance(body, dict) else body
                failures.append(Failure(full_name, f"PATCH refused with HTTP {patched.status}: {detail}"))
                policies.append(policy)
                continue
            after = fetch_repo_facts(cast(FetchStatusFn, fetch_status), token, full_name).facts
            if after is not None and after.allow_auto_merge is value:
                applied.append(f"{full_name}:{'enable' if value else 'disable'}")
                policy = evaluate(full_name, branch, after, protection, rulesets)
            else:
                failures.append(
                    Failure(
                        full_name,
                        f"PATCH accepted but allow_auto_merge still reads {None if after is None else after.allow_auto_merge}",
                    )
                )
        policies.append(policy)
    return RolloutReport(
        policies=tuple(policies),
        applied=tuple(applied),
        failures=tuple(failures),
        # `apply=True` with no write function cannot change anything, so it is a
        # dry run and must say so rather than reporting an applied run.
        dry_run=not (apply and write is not None),
    )


class RolloutError(RuntimeError):
    """A listing or a settings read failed; the rollout must not pretend it covered a fleet.

    Raised for a failed owner listing and for an exhausted GitHub API rate
    limit: a 403 that means "slow down" must stop the run, not become 35
    `unreadable` verdicts that read like per-repository permission problems.
    """


def _rate_limited(body: JsonValue) -> bool:
    if not isinstance(body, dict):
        return False
    message = body.get("message")
    return isinstance(message, str) and "rate limit" in message.lower()


def to_json_dict(report: RolloutReport) -> dict[str, JsonValue]:
    return {
        "dry_run": report.dry_run,
        "counts": [{"verdict": vc.verdict, "count": vc.count} for vc in report.verdict_counts()],
        "applied": list(report.applied),
        "failures": [{"repo": failure.repo, "error": failure.error} for failure in report.failures],
        "repositories": [
            {
                "repo": p.full_name,
                "default_branch": p.default_branch,
                "allow_auto_merge": p.allow_auto_merge,
                "required_checks": list(p.required_contexts),
                "merge_queue": p.queue,
                "verdict": p.verdict,
                "detail": p.detail,
            }
            for p in report.policies
        ],
    }


def render_text(report: RolloutReport) -> str:
    mode = "DRY RUN (no writes)" if report.dry_run else "APPLIED"
    lines = [f"ci-lint merge-policy ({mode})"]
    counts = report.verdict_counts()
    if counts:
        lines.append("verdicts: " + ", ".join(f"{vc.verdict}={vc.count}" for vc in counts))
    lines.append("")
    for p in report.policies:
        checks = len(p.required_contexts)
        lines.append(
            f"{p.verdict:<17} {p.full_name:<45} auto_merge={str(p.allow_auto_merge):<5} "
            f"checks={checks:<2} queue={str(p.queue):<5} {p.detail}"
        )
    if report.applied:
        lines.append("")
        lines.append("allow_auto_merge changes: " + ", ".join(report.applied))
    if report.failures:
        lines.append("")
        for failure in report.failures:
            lines.append(f"FAILED {failure.repo}: {failure.error}")
    return "\n".join(lines)
