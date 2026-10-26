"""Minimal stdlib GitHub REST (+ GraphQL) client, injectable so tests never
touch the network (round-3A brief, Part 2: "No network in unit tests:
inject a fetch function"; round-4A brief: "GitHub REST via urllib with an
injectable fetch/delete function").

Everything that needs the GitHub API (`ci_lint.reuse`'s title-edit reuse
lookup, `ci_lint.runtime.gate`'s live reuse re-verification, and round-4A's
`ci_lint.cache.audit`/`ci_lint.cache.ops`, which also list, delete and
GraphQL-query caches/PRs) takes a `FetchFn`/`DeleteFn`/`GraphQLFn`, not a
URL and a `urllib` call directly, so a test can pass a fake that reads a
small recorded JSON fixture instead. `default_fetch`/`default_delete`/
`default_graphql` are the real implementations, used only by the CLI layer
(`ci_lint/cli.py`).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator

from ci_lint.cargo_messages import JsonValue

# (url, token) -> parsed JSON body. `token` is always sent as a bearer
# token; every caller here is an authenticated GitHub API read.
FetchFn = Callable[[str, str], JsonValue]

# (url, token) -> None. Raises GitHubApiError on any failure (including a
# non-2xx status). Used by `ci_lint.cache.ops` (heal/trim/janitor); every
# caller MUST have already decided this delete should happen -- this
# function does no policy of its own.
DeleteFn = Callable[[str, str], None]

# (query, token) -> parsed JSON body of the GraphQL response (the whole
# envelope, including a top-level "errors" array if GitHub returned one --
# callers check for it themselves, matching this module's "raise
# GitHubApiError on transport/HTTP failure, let the caller interpret a
# well-formed-but-unhappy body" split for FetchFn/DeleteFn).
GraphQLFn = Callable[[str, str], JsonValue]

# (url, token) -> (status_code, parsed JSON body or None). Round-5: some
# live checks (ci_lint.settings_audit's SEC-005/006/007, GEN-006/011) MUST
# tell a 403 ("needs an admin token" -- report needs_review, never pass)
# apart from a 404 ("treat as none" -- e.g. no branch protection configured
# at all) apart from a real transport failure. FetchFn/default_fetch can't
# express that distinction (a non-2xx status raises GitHubApiError with no
# structured code), so this is a second, explicitly status-aware fetch
# shape rather than a behavior change to the existing one -- every existing
# caller of FetchFn/default_fetch is untouched.
FetchStatusFn = Callable[[str, str], "tuple[int, JsonValue]"]

# (url, token, method, json-body) -> (status_code, parsed JSON body or None).
# Round M2-42 (`ci_lint.sync_issues --apply`, zackees/ci.yml#98): the ONLY
# write surface in this package that mutates repository content (issues),
# as opposed to `DeleteFn` (Actions cache entries). `method` is "POST" or
# "PATCH". Every caller MUST have already decided this write should happen
# (opt-in checked, cap not exceeded, fingerprint verified) -- like
# `DeleteFn`, this function does no policy of its own.
WriteFn = Callable[[str, str, str, "dict[str, JsonValue]"], "tuple[int, JsonValue]"]

_API_ROOT = "https://api.github.com"


class GitHubApiError(Exception):
    """Any network, HTTP, or JSON-decode failure talking to the GitHub API.

    Every caller in this package catches this and degrades to "reuse
    nothing" (or, in round-4A's live cache checks, "needs_review, not a
    silent pass") plus a printed warning (round-3A brief, Part 2: "On any
    API error: reuse nothing and print a warning; never fail the precheck
    because the API is down") -- it must never propagate out of a
    precheck/plan/gate/cache-audit run.
    """


@dataclass(frozen=True)
class HttpResult:
    """`(status_code, parsed JSON body or None)` from a status-returning call.

    Iterable so the `status, body = fetch(...)` unpacking every
    `FetchStatusFn` caller uses keeps working (PY-002, #193)."""

    status: int
    body: JsonValue

    def __iter__(self) -> Iterator[int | JsonValue]:
        return iter((self.status, self.body))


def _request(
    url: str,
    token: str,
    *,
    method: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> urllib.request.Request:
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "ci-lint")
    if content_type:
        request.add_header("Content-Type", content_type)
    return request


def default_fetch(url: str, token: str) -> JsonValue:
    """The real implementation: stdlib `urllib`, no third-party HTTP
    client (AGENTS.md / worker contract: ci_lint is standard-library-only).
    """

    request = _request(url, token)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed https GitHub API host
            body = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubApiError(f"GET {url} failed: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GitHubApiError(f"GET {url}: invalid JSON response: {exc}") from exc


def default_fetch_status(url: str, token: str) -> HttpResult:
    """Like `default_fetch`, but returns `(status_code, body)` instead of
    raising on a non-2xx HTTP status -- a 403 (often "this token lacks
    admin scope") and a 404 (often "this thing legitimately doesn't
    exist") must be told apart by the caller, not collapsed into one
    generic error. `body` is `None` when the response has no content or
    is not valid JSON (some 403/404 error bodies still parse as JSON with
    a `message` field, which is returned as-is when it does). Still raises
    GitHubApiError on an actual transport failure (DNS, timeout, ...),
    exactly like `default_fetch`."""

    request = _request(url, token)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed https GitHub API host
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubApiError(f"GET {url} failed: {exc}") from exc
    if not body:
        return HttpResult(status, None)
    try:
        return HttpResult(status, json.loads(body))
    except json.JSONDecodeError:
        return HttpResult(status, None)


def default_delete(url: str, token: str) -> None:
    """`DELETE` via stdlib `urllib`. Round-4A: `ci_lint.cache.ops` (heal/
    trim/janitor) is the only caller; it requires `actions: write` on the
    token, and every deletion it performs is logged before this is called."""

    request = _request(url, token, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed https GitHub API host
            response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubApiError(f"DELETE {url} failed: {exc}") from exc


def default_write(url: str, token: str, method: str, payload: "dict[str, JsonValue]") -> HttpResult:
    """POST/PATCH via stdlib `urllib`. Round M2-42: `ci_lint.sync_issues`'s
    `--apply` path is the only caller (issue create/update/close). Like
    `default_fetch_status`, a non-2xx HTTP status is returned rather than
    raised, so the caller can log exactly what GitHub said instead of
    losing the response body to a generic exception."""

    if method not in ("POST", "PATCH"):
        raise ValueError(f"default_write: unsupported method {method!r}")
    body = json.dumps(payload).encode("utf-8")
    request = _request(url, token, method=method, data=body, content_type="application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed https GitHub API host
            resp_body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        resp_body = exc.read()
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubApiError(f"{method} {url} failed: {exc}") from exc
    if not resp_body:
        return HttpResult(status, None)
    try:
        return HttpResult(status, json.loads(resp_body))
    except json.JSONDecodeError:
        return HttpResult(status, None)


def default_graphql(query: str, token: str) -> JsonValue:
    """POST a GraphQL query via stdlib `urllib`. Round-4A: `ci_lint.cache.
    github_cache.fetch_pr_states` is the only caller -- ONE query batching
    every referenced PR number's state, per the round-4A brief ("closed/
    merged PRs via ONE GraphQL query for all referenced PR numbers")."""

    body = json.dumps({"query": query}).encode("utf-8")
    request = _request(f"{_API_ROOT}/graphql", token, method="POST", data=body, content_type="application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed https GitHub API host
            resp_body = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GitHubApiError(f"POST /graphql failed: {exc}") from exc
    try:
        return json.loads(resp_body)
    except json.JSONDecodeError as exc:
        raise GitHubApiError(f"POST /graphql: invalid JSON response: {exc}") from exc
