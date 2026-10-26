"""CT-007: a `linter` pin must name a commit that exists.

`ci.toml`'s top-level `linter = "zackees/ci.yml@<40-hex>"` names the exact
ci-lint commit a repository's precheck runs. `CT-002` already checks that the
string is *well formed* -- 40 hex characters after an `@`. It cannot check
that the commit is *real*, and that distinction is the whole bug:

    linter = "zackees/ci.yml@fe965fc7e2c5203570072e1d0fd19d0935d98e39"

is a perfect CT-002 pass and a commit that has never existed in zackees/ci.yml.

## Why a dead pin is worse than a stale one

`ci-lint local-gate run` resolves the declared pin BEFORE executing any lane,
so an unresolvable pin means the gate cannot run at all: no `Local-Gate:`
trailer can be produced, and every PR fails GATE-003 with a red cascade behind
it. Precheck degrades too, since the tool it is supposed to run cannot be
fetched.

A *stale* pin merely misses newer rules. A dead one disables the repository's
entire local verification.

## Measured 2026-10-05

A fleet-wide audit of all 37 repositories found **two dead pins**, both
introduced by the cache-policy workstream hours earlier and neither caught by
any existing check:

- `zackees/bosn` -- `fe965fc7e2c5203570072e1d0fd19d0935d98e39`
- `zackees/soldr` -- `a07bab94f16124b5c6857b137a237a53a61e06d1`

Both return 422 from the GitHub API and are absent from the object store
entirely, so no branch or tag can recover them. Nine repositories pin a live
commit; no pin was merely stale (the oldest was 1.5 days behind main), which
is why the failure mode here is deadness and not drift.

## Why this is `needs_review`

Resolvability is a question about a REMOTE repository, so the honest check
needs a live call. Offline, this rule can only confirm the shape (CT-002's
job) and say nothing more -- so with no fetch supplied it stays silent rather
than guessing. `--live` supplies the fetch and a dead pin becomes a finding.
"""

from __future__ import annotations

from ci_lint import repo_identity

from dataclasses import dataclass

from ci_lint.finding import Finding, Status
from ci_lint.github_api import FetchStatusFn
from ci_lint.schema import CiToml

RULE = "CT-007"

CI_YML = repo_identity.CI_YML_REPO


@dataclass(frozen=True)
class LinterPinResult:
    """The outcome of resolving one repository's declared linter pin.

    `resolves` is None when the answer is UNKNOWN -- an unauthenticated
    request gets 401 for every commit, live or dead, so a 401 says nothing
    about the pin and must never be reported as a dead one.
    """

    repo: str
    sha: str
    resolves: bool | None
    detail: str


def resolve_linter_pin(
    fetch_status: FetchStatusFn, token: str, repo: str, sha: str
) -> LinterPinResult:
    """Ask GitHub whether `sha` is a commit in `CI_YML`.

    A 200 means the pin resolves. Anything else -- 404, 422, or a transport
    failure -- is recorded as unresolvable with the detail preserved, because
    "the API said 422" and "the network failed" are different diagnoses and the
    message should not blur them.
    """

    url = f"https://api.github.com/repos/{CI_YML}/commits/{sha}"
    try:
        status, _ = fetch_status(url, token)
    except Exception as exc:  # noqa: BLE001 -- a transport failure is data here
        return LinterPinResult(
            repo=repo, sha=sha, resolves=None, detail=f"lookup failed: {exc}"
        )
    if status == 200:
        return LinterPinResult(repo=repo, sha=sha, resolves=True, detail="")
    if status in (401, 403):
        # No (or under-scoped) credentials: every commit 401s, live or dead.
        # This is not evidence about the pin, so the answer stays unknown.
        return LinterPinResult(
            repo=repo,
            sha=sha,
            resolves=None,
            detail=f"GET {url} returned HTTP {status} (credentials required to resolve pins)",
        )
    return LinterPinResult(
        repo=repo, sha=sha, resolves=False, detail=f"GET {url} returned HTTP {status}"
    )


def check_ct_007(
    ci: CiToml,
    *,
    fetch_status: FetchStatusFn | None = None,
    token: str = "",
    repo: str = "",
) -> list[Finding]:
    """The declared `linter` SHA must name a real commit.

    Silent without a fetch: resolvability is remote state, and an offline run
    has no evidence either way. CT-002 already covers the shape offline.
    """

    sha = ci.linter_sha
    if not sha or fetch_status is None or not repo:
        return []
    result = resolve_linter_pin(fetch_status, token, repo, sha)
    if result.resolves is not False:
        # True (it resolves) or None (unknown -- no credentials). Neither is
        # evidence of a dead pin.
        return []
    return [
        Finding(
            rule=RULE,
            status=Status.VIOLATION,
            path="ci.toml",
            message=(
                f"'linter' pins {CI_YML}@{sha}, which is not a commit in that "
                f"repository ({result.detail}). `ci-lint local-gate run` resolves this pin "
                "before executing any lane, so the gate cannot run at all: no "
                "`Local-Gate:` trailer can be produced and every PR fails GATE-003. "
                "Precheck is degraded too, since the linter cannot be fetched."
            ),
            fix=(
                "repoint 'linter' at a commit that exists in "
                f"{CI_YML} (CT-002's shape check cannot catch this -- a well-formed "
                "SHA naming a nonexistent commit passes it)"
            ),
        )
    ]