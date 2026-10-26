"""`ci-lint publish oidc-check`: issue #6 §7's mock publisher -- mint an
OIDC token via the GitHub Actions runtime's own token endpoint for a given
audience, assert its claims in-memory, and stop before any upload.

Security discipline (round-5 brief; this is the load-bearing part of this
module): the raw token string, its `jti`, and its signature segment are
NEVER printed, logged, returned in a JSON-serializable dict, or included in
any exception message this module raises. `request_oidc_token` returns the
token to its caller and nothing else touches it; `decode_payload` reads
only the middle (payload) JWT segment -- the header and signature segments
are discarded unread. Every network call goes through an injectable
`FetchFn` (`ci_lint.github_api.FetchFn`), so no test in this package ever
makes a real request; every unit test builds its own throwaway fake token.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import FetchFn, GitHubApiError

DEFAULT_WORKFLOW_PATH = ".github/workflows/ci.yml"

# The only claim values this module is allowed to print. Never "aud" (an
# audience isn't secret either, but the round-5 brief's allowlist names
# exactly these five, so printed VALUES are restricted to exactly this set
# -- every claim is still asserted and its PASS/FAIL is always printed).
PRINTABLE_CLAIMS = frozenset({"repository", "ref", "environment", "event_name", "workflow_ref"})


class OidcCheckRefused(Exception):
    """ci.toml [publish].pypi.mode != "mock" -- refuse to run at all (exit 2)."""


class OidcCheckError(Exception):
    """Token request or decode failure. Never includes the raw token."""


@dataclass(frozen=True)
class ClaimAssertion:
    claim: str
    expected: str
    actual: str | None  # None: the claim was absent from the payload entirely
    ok: bool


@dataclass(frozen=True)
class OidcCheckResult:
    assertions: tuple[ClaimAssertion, ...]
    ok: bool


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_payload(token: str) -> dict[str, JsonValue]:
    """Decodes ONLY the JWT payload (the middle `.`-separated segment).
    The header and signature segments are never touched -- this function
    does not (and cannot, without the issuer's public key) verify the
    signature; it trusts the token because it was fetched over the
    Actions runtime's own authenticated channel, exactly as `core.getIDToken`
    does in the official actions/toolkit."""

    parts = token.split(".")
    if len(parts) != 3:
        raise OidcCheckError("malformed OIDC token: expected 3 dot-separated segments")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OidcCheckError(f"cannot decode OIDC token payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise OidcCheckError("OIDC token payload is not a JSON object")
    return payload


def request_oidc_token(fetch: FetchFn, *, request_url: str, request_token: str, audience: str) -> str:
    """Requests the OIDC token for `audience` from
    `ACTIONS_ID_TOKEN_REQUEST_URL` (`&audience=<audience>` appended, the
    documented GitHub Actions convention), authenticating with
    `ACTIONS_ID_TOKEN_REQUEST_TOKEN`. Returns the raw JWT string. This
    function itself never prints or logs either the URL's token query
    value, `request_token`, or the returned JWT -- it only returns it."""

    url = f"{request_url}&audience={audience}"
    try:
        body = fetch(url, request_token)
    except GitHubApiError as exc:
        # exc's message is built from `url`/a transport error only --
        # never from the response body -- so it cannot leak the token.
        raise OidcCheckError(f"OIDC token request failed: {exc}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("value"), str):
        raise OidcCheckError("OIDC token endpoint response missing a string 'value' field")
    return body["value"]


def assert_claims(
    payload: dict[str, JsonValue],
    *,
    repository: str,
    expect_ref: str,
    expect_event: str,
    environment: str,
    audience: str,
    workflow_path: str = DEFAULT_WORKFLOW_PATH,
) -> OidcCheckResult:
    assertions: list[ClaimAssertion] = []

    def add(claim: str, expected: str) -> None:
        actual = payload.get(claim)
        actual_str = actual if isinstance(actual, str) else None
        assertions.append(ClaimAssertion(claim=claim, expected=expected, actual=actual_str, ok=actual_str == expected))

    add("repository", repository)
    add("ref", expect_ref)
    add("environment", environment)
    add("event_name", expect_event)
    # PyPI trusted publishing requires a top-level (non-reusable) workflow
    # file -- warehouse#11096 -- which is exactly what asserting the full
    # `workflow_ref` shape (not just its suffix) proves.
    add("workflow_ref", f"{repository}/{workflow_path}@{expect_ref}")
    add("aud", audience)

    return OidcCheckResult(assertions=tuple(assertions), ok=all(a.ok for a in assertions))


def to_json_dict(result: OidcCheckResult) -> dict[str, JsonValue]:
    """Never includes the raw token, `jti`, or signature -- only the
    (non-secret) claim names/expected/actual values this module already
    decided are safe to surface, per PRINTABLE_CLAIMS."""

    return {
        "ok": result.ok,
        "assertions": [
            {
                "claim": a.claim,
                "ok": a.ok,
                "expected": a.expected if a.claim in PRINTABLE_CLAIMS else None,
                "actual": a.actual if a.claim in PRINTABLE_CLAIMS else None,
            }
            for a in result.assertions
        ],
    }


def render(result: OidcCheckResult) -> str:
    lines = ["ci-lint publish oidc-check: asserted claims"]
    for a in result.assertions:
        status = "PASS" if a.ok else "FAIL"
        if a.claim in PRINTABLE_CLAIMS:
            lines.append(f"  [{status}] {a.claim} = {a.actual!r} (expected {a.expected!r})")
        else:
            lines.append(f"  [{status}] {a.claim}")
    lines.append(f"ci-lint publish oidc-check: {'PASS' if result.ok else 'FAIL'}")
    lines.append("mock publish: stopping before upload")
    return "\n".join(lines)
