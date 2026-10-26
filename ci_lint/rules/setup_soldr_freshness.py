"""RUST-014: a SHA-pinned `zackees/setup-soldr` must not be missing a
critical fix, or drift more than N releases behind latest (zackees/ci.yml
#33, reconciling SEC-004's SHA-pin requirement with #18's float-by-default
direction).

Context (issue #33): `RUST-013` (issue #18) already flags an exact `soldr==`
pin or a literal `setup-soldr` `version:` input with no recorded exception,
and pushes the fleet toward `zackees/setup-soldr@v0` (floating). But
`SEC-004` requires every third-party `uses:` be pinned to a 40-hex commit
SHA, and some repositories additionally have GitHub's own
`sha_pinning_required` setting on, which rejects a floating `@v0` ref before
any job even runs (zackees/soldr had to be re-pinned by SHA in soldr#3445).
For those repositories a SHA pin is not itself a defect -- RUST-013 already
carves that out via `[[exceptions]]` -- but a SHA pin that has gone stale
silently costs the whole fleet CI time: setup-soldr#539/#541 (v0.9.81/
v0.9.82) fixed Dylint caches never saving (`RUST-004`), and every repo below
that pin kept paying for a fully cold Dylint run on every commit (issue #31:
kernal-api's Dylint job at ~9.5 min, 0 HIT / 2078 MISS).

Policy (recorded in docs/policy-rust.md): a SHA pin is allowed (it is in
fact REQUIRED wherever `sha_pinning_required` is on, or wherever RUST-013's
`[[exceptions]]` records a deliberate exact pin), but the pinned commit must
not be missing a release setup-soldr's own maintainers marked critical, and
must not drift more than `MAX_RELEASES_BEHIND` releases behind the latest
release. This is the SHA-pin analogue of RUST-013's floating-pin freshness
requirement -- RUST-013 says "don't freeze silently", RUST-014 says "if you
must freeze, the freeze must not silently rot". Unlike RUST-013 (a static
precheck rule, so `ci.toml`'s `[[exceptions]]` table can mark a deliberate
exact pin), RUST-014 is a LIVE check run only by `ci-lint audit` --
`ci_lint.exceptions.apply_exceptions` is never applied to `ci-lint audit`
findings (same as SEC-005/006/007 and GEN-006/010/011 above), so it cannot
be silenced through `[[exceptions]]`; a repository with a documented reason
to stay behind records that reason in the PR, not in ci.toml.

Mechanics: this is a LIVE check (needs the GitHub releases API for
`zackees/setup-soldr` itself, a repository unrelated to whichever repo is
being linted), wired into `ci-lint audit` exactly like SEC-005/006/007 and
GEN-006/010/011 -- same injectable `FetchStatusFn`, same "403 is
needs_review, never a silent pass" discipline as `ci_lint.settings_audit`.

  1. Statically find every `zackees/setup-soldr[...]@<40-hex-sha>` `uses:`
     in the repository's workflows and composite actions (the base action
     and its `/cook`, `/cleanup` sub-actions all share the pin's freshness).
  2. For each distinct pinned SHA, `GET /repos/zackees/setup-soldr/commits/
     {sha}` for its commit date. A 404 means the ref doesn't exist on
     setup-soldr's default branch history as a commit GitHub can resolve
     (e.g. a squash-merged/rewritten SHA) -- reported `needs_review`, never
     silently skipped.
  3. `GET /repos/zackees/setup-soldr/releases` (non-draft, non-prerelease,
     newest first, as GitHub already orders them) and keep every release
     published strictly after the pinned commit's date -- those are the
     releases this pin is missing.
  4. A missing release is "critical" when its name or body contains the
     word "critical" (case-insensitive) -- setup-soldr's own maintainers'
     documented way of flagging a release in its notes (issue #33's #539/
     #541 precedent: this repo's own case study). ANY missing critical
     release is a violation naming that release. Otherwise, more than
     `MAX_RELEASES_BEHIND` missing releases (regardless of criticality) is
     also a violation -- unbounded drift is itself the failure mode #33
     describes ("10 of 13 repos" stale, spread across nine different refs).
"""

from __future__ import annotations

from ci_lint import repo_identity

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue

from ci_lint.finding import Finding, Status
from ci_lint.github_api import FetchStatusFn, GitHubApiError
from ci_lint.rules.tools import _iter_uses_with
from ci_lint.workflow_scan import as_dict, load_composite_actions, load_workflows
from ci_lint.yaml_io import YamlValue
from ci_lint.yaml_io import LoadStatus

SETUP_SOLDR_REPO = repo_identity.SETUP_SOLDR_REPO
API_ROOT = "https://api.github.com"

# `zackees/setup-soldr@<sha>`, `zackees/setup-soldr/cook@<sha>`, etc.
_SHA_PIN_RE = re.compile(rf"^{repo_identity.SETUP_SOLDR_RE}(?:/[\w-]+)?@([0-9a-f]{{40}})$", re.IGNORECASE)

_CRITICAL_RE = re.compile(r"\bcritical\b", re.IGNORECASE)

# Beyond this many missing (non-critical) releases, drift alone is a
# violation -- issue #33's evidence: 9 different stale refs across 10 of 13
# repos with nobody watching. Chosen generously (a healthy repo bumps well
# inside this window); tune here, not per-repo, if the fleet's cadence
# changes.
MAX_RELEASES_BEHIND = 5


@dataclass(frozen=True)
class PinSite:
    """One `uses:` site of a SHA-pinned setup-soldr action."""

    path: str
    loc: str


@dataclass(frozen=True)
class ShaPin:
    """A distinct pinned SHA and every site that pins it."""

    sha: str
    sites: tuple[PinSite, ...]


@dataclass(frozen=True)
class _ScannedFile:
    path: str
    is_composite: bool
    doc: dict[str, YamlValue]


@dataclass(frozen=True)
class ApiReply:
    status: int
    body: JsonValue


@dataclass(frozen=True)
class _Release:
    tag_name: str
    name: str
    body: str


def _find_sha_pins(repo_root: Path) -> tuple[ShaPin, ...]:
    """Returns one ShaPin per distinct pinned SHA, in first-seen order."""

    pins: dict[str, list[PinSite]] = {}
    files: list[_ScannedFile] = []
    for wf in load_workflows(repo_root):
        if wf.status == LoadStatus.OK:
            files.append(_ScannedFile(wf.path, False, as_dict(wf.document)))
    for act in load_composite_actions(repo_root):
        if act.status == LoadStatus.OK:
            files.append(_ScannedFile(act.path, True, as_dict(act.document)))

    for scanned in files:
        for use_step in _iter_uses_with(scanned.doc, is_composite=scanned.is_composite):
            m = _SHA_PIN_RE.match(use_step.uses)
            if m is None:
                continue
            pins.setdefault(m.group(1), []).append(PinSite(scanned.path, use_step.loc))
    return tuple(ShaPin(sha, tuple(sites)) for sha, sites in pins.items())


def _get(fetch_status: FetchStatusFn, token: str, url: str) -> ApiReply | None:
    try:
        status, body = fetch_status(url, token)
        return ApiReply(status, body)
    except GitHubApiError:
        return None


def _sites(locations: tuple[PinSite, ...]) -> str:
    return ", ".join(f"{site.path}:{site.loc}" for site in locations)


def _network_finding(locations: tuple[PinSite, ...], sha: str, url: str) -> Finding:
    return Finding(
        rule="RUST-014",
        status=Status.NEEDS_REVIEW,
        path=_sites(locations),
        message=f"could not reach the GitHub API for {url} to check setup-soldr@{sha[:12]}'s freshness "
        "(network/transport failure)",
        fix="re-run once the GitHub API is reachable; this is never treated as a pass on failure",
    )


def _forbidden_finding(locations: tuple[PinSite, ...], sha: str) -> Finding:
    return Finding(
        rule="RUST-014",
        status=Status.NEEDS_REVIEW,
        path=_sites(locations),
        message=f"cannot check setup-soldr@{sha[:12]}'s freshness: 403 Forbidden on the "
        f"{SETUP_SOLDR_REPO} API",
        fix="re-run with a token that has at least public read access to zackees/setup-soldr",
    )


def _unresolvable_finding(locations: tuple[PinSite, ...], sha: str) -> Finding:
    return Finding(
        rule="RUST-014",
        status=Status.NEEDS_REVIEW,
        path=_sites(locations),
        message=f"setup-soldr@{sha} does not resolve to a commit on {SETUP_SOLDR_REPO} (404) -- the "
        "pin may be stale, on a deleted branch, or rewritten",
        fix=f"re-pin to a current commit SHA from {SETUP_SOLDR_REPO}'s releases page and re-run "
        "this check",
    )


def _check_one_sha(  # noqa: C901
    fetch_status: FetchStatusFn, token: str, sha: str, locations: tuple[PinSite, ...]
) -> list[Finding]:
    commit_url = f"{API_ROOT}/repos/{SETUP_SOLDR_REPO}/commits/{sha}"
    commit_result = _get(fetch_status, token, commit_url)
    if commit_result is None:
        return [_network_finding(locations, sha, commit_url)]
    c_status, c_body = commit_result.status, commit_result.body
    if c_status == 403:
        return [_forbidden_finding(locations, sha)]
    if c_status == 404:
        return [_unresolvable_finding(locations, sha)]
    if c_status != 200 or not isinstance(c_body, dict):
        return [
            Finding(
                rule="RUST-014",
                status=Status.NEEDS_REVIEW,
                path=_sites(locations),
                message=f"unexpected HTTP {c_status} resolving setup-soldr@{sha} commit date",
                fix="re-run and inspect the response",
            )
        ]
    commit_info = c_body.get("commit")
    committer = commit_info.get("committer") if isinstance(commit_info, dict) else None
    pinned_date = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(pinned_date, str):
        return [
            Finding(
                rule="RUST-014",
                status=Status.NEEDS_REVIEW,
                path=_sites(locations),
                message=f"setup-soldr@{sha}'s commit date could not be read from the API response",
                fix="re-run and inspect the response",
            )
        ]

    releases_url = f"{API_ROOT}/repos/{SETUP_SOLDR_REPO}/releases"
    releases_result = _get(fetch_status, token, releases_url)
    if releases_result is None:
        return [_network_finding(locations, sha, releases_url)]
    r_status, r_body = releases_result.status, releases_result.body
    if r_status == 403:
        return [_forbidden_finding(locations, sha)]
    if r_status != 200 or not isinstance(r_body, list):
        return [
            Finding(
                rule="RUST-014",
                status=Status.NEEDS_REVIEW,
                path=_sites(locations),
                message=f"unexpected HTTP {r_status} listing {SETUP_SOLDR_REPO} releases",
                fix="re-run and inspect the response",
            )
        ]

    missing: list[_Release] = []
    for rel in r_body:
        if not isinstance(rel, dict):
            continue
        if rel.get("draft") is True or rel.get("prerelease") is True:
            continue
        published_at = rel.get("published_at")
        if not isinstance(published_at, str):
            continue
        if published_at > pinned_date:
            missing.append(
                _Release(
                    tag_name=str(rel.get("tag_name") or ""),
                    name=str(rel.get("name") or ""),
                    body=str(rel.get("body") or ""),
                )
            )

    if not missing:
        return []

    critical = [
        rel
        for rel in missing
        if _CRITICAL_RE.search(rel.name) or _CRITICAL_RE.search(rel.body)
    ]
    latest_tag = str(r_body[0].get("tag_name")) if r_body and isinstance(r_body[0], dict) else "latest"

    if critical:
        names = ", ".join(rel.tag_name or rel.name or "None" for rel in critical)
        return [
            Finding(
                rule="RUST-014",
                path=_sites(locations),
                message=f"setup-soldr@{sha[:12]} is missing critical fix release(s): {names} "
                f"({len(missing)} release(s) behind {latest_tag} total)",
                fix=f"bump the {SETUP_SOLDR_REPO} pin at {_sites(locations)} to {latest_tag} (or at "
                "least a SHA published on/after the critical release's date) -- this is a live "
                "check (`ci-lint audit`), not a precheck rule, so it is never silenced by "
                "ci.toml's [[exceptions]] table; if this repository has a documented reason to "
                "stay behind, record that reason in the PR bumping the pin instead",
            )
        ]

    if len(missing) > MAX_RELEASES_BEHIND:
        return [
            Finding(
                rule="RUST-014",
                path=_sites(locations),
                message=f"setup-soldr@{sha[:12]} is {len(missing)} release(s) behind {latest_tag} "
                f"(more than the {MAX_RELEASES_BEHIND}-release drift budget)",
                fix=f"bump the {SETUP_SOLDR_REPO} pin at {_sites(locations)} to {latest_tag} -- this "
                "is a live check (`ci-lint audit`), not a precheck rule, so it is never silenced "
                "by ci.toml's [[exceptions]] table",
            )
        ]

    return []


def check_rust_014(fetch_status: FetchStatusFn, token: str, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for pin in _find_sha_pins(repo_root):
        findings.extend(_check_one_sha(fetch_status, token, pin.sha, pin.sites))
    return findings
