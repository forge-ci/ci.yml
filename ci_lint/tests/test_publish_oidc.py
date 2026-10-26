"""`ci-lint publish oidc-check` -- ci_lint/publish_oidc.py (round-5 brief,
deliverable 2). Every token here is a throwaway fake built in the test
itself (header.payload.sig) -- never a real OIDC token. The load-bearing
assertion is `test_cli_never_prints_the_raw_token`: it runs the actual CLI
command end-to-end (with a fake fetch injected in place of network I/O)
and greps its captured stdout+stderr for the fake token string.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_lint.publish_oidc import (
    OidcCheckError,
    assert_claims,
    decode_payload,
    request_oidc_token,
    to_json_dict,
)
from ci_lint.publish_oidc import render as render_oidc
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

REPO = FIXTURES / "runtime" / "cache" / "repo"  # [publish.pypi] = { auth = "oidc", environment = "pypi", mode = "mock" }
REPOSITORY = "zackees/template-python-rust-cmd"


def _b64url(obj: object) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _fake_token(payload: dict[str, object], *, header: dict[str, object] | None = None, sig: str = "sssssig") -> str:
    header = header or {"alg": "RS256", "typ": "JWT"}
    return f"{_b64url(header)}.{_b64url(payload)}.{sig}"


GOOD_PAYLOAD = {
    "repository": REPOSITORY,
    "ref": "refs/heads/main",
    "environment": "pypi",
    "event_name": "workflow_dispatch",
    "workflow_ref": f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/main",
    "aud": "pypi",
    "jti": "super-secret-token-id-never-print-me",
}


class DecodePayloadTest(unittest.TestCase):
    def test_decodes_valid_payload(self) -> None:
        token = _fake_token(GOOD_PAYLOAD)
        payload = decode_payload(token)
        self.assertEqual(REPOSITORY, payload["repository"])

    def test_malformed_token_raises(self) -> None:
        with self.assertRaises(OidcCheckError):
            decode_payload("not-a-jwt")

    def test_non_base64_payload_raises(self) -> None:
        with self.assertRaises(OidcCheckError):
            decode_payload("aaaa.!!!not-base64!!!.bbbb")

    def test_payload_not_an_object_raises(self) -> None:
        token = f"{_b64url({})}.{_b64url([1, 2, 3])}.sig"
        with self.assertRaises(OidcCheckError):
            decode_payload(token)


class RequestOidcTokenTest(unittest.TestCase):
    def test_appends_audience_and_returns_value(self) -> None:
        seen_urls: list[str] = []

        def fetch(url: str, token: str) -> dict[str, object]:
            seen_urls.append(url)
            assert token == "runtime-request-token"
            return {"value": "the.fake.jwt", "count": 1}

        result = request_oidc_token(
            fetch, request_url="https://example/token?api-version=2.0", request_token="runtime-request-token",
            audience="pypi",
        )
        self.assertEqual("the.fake.jwt", result)
        self.assertEqual(["https://example/token?api-version=2.0&audience=pypi"], seen_urls)

    def test_missing_value_field_raises(self) -> None:
        with self.assertRaises(OidcCheckError):
            request_oidc_token(
                lambda url, token: {"count": 1}, request_url="https://example/token", request_token="t",
                audience="pypi",
            )


class AssertClaimsTest(unittest.TestCase):
    def _assert(self, payload: dict[str, object]):
        return assert_claims(
            payload,
            repository=REPOSITORY,
            expect_ref="refs/heads/main",
            expect_event="workflow_dispatch",
            environment="pypi",
            audience="pypi",
        )

    def test_all_claims_pass(self) -> None:
        result = self._assert(GOOD_PAYLOAD)
        self.assertTrue(result.ok)
        self.assertEqual(6, len(result.assertions))
        self.assertTrue(all(a.ok for a in result.assertions))

    def test_wrong_repository_fails(self) -> None:
        result = self._assert({**GOOD_PAYLOAD, "repository": "someone-else/evil"})
        self.assertFalse(result.ok)
        repo_assertion = next(a for a in result.assertions if a.claim == "repository")
        self.assertFalse(repo_assertion.ok)

    def test_wrong_ref_fails(self) -> None:
        result = self._assert({**GOOD_PAYLOAD, "ref": "refs/heads/some-pr-branch"})
        self.assertFalse(result.ok)

    def test_wrong_environment_fails(self) -> None:
        result = self._assert({**GOOD_PAYLOAD, "environment": "staging"})
        self.assertFalse(result.ok)

    def test_wrong_event_name_fails(self) -> None:
        result = self._assert({**GOOD_PAYLOAD, "event_name": "pull_request"})
        self.assertFalse(result.ok)

    def test_reusable_workflow_workflow_ref_fails(self) -> None:
        # warehouse#11096: PyPI trusted publishing requires a top-level
        # workflow -- a workflow_ref pointing at a reusable workflow (or a
        # different ref) must fail this assertion.
        result = self._assert(
            {**GOOD_PAYLOAD, "workflow_ref": f"{REPOSITORY}/.github/workflows/reusable.yml@refs/heads/main"}
        )
        self.assertFalse(result.ok)
        wf = next(a for a in result.assertions if a.claim == "workflow_ref")
        self.assertFalse(wf.ok)

    def test_wrong_audience_fails(self) -> None:
        result = self._assert({**GOOD_PAYLOAD, "aud": "testpypi"})
        self.assertFalse(result.ok)

    def test_missing_claim_fails_not_crashes(self) -> None:
        payload = dict(GOOD_PAYLOAD)
        del payload["environment"]
        result = self._assert(payload)
        self.assertFalse(result.ok)
        env = next(a for a in result.assertions if a.claim == "environment")
        self.assertIsNone(env.actual)
        self.assertFalse(env.ok)


class NeverPrintsTokenTest(unittest.TestCase):
    """The round-5 brief's load-bearing security requirement: 'include a
    test that asserts the token string never appears in stdout/stderr'."""

    def setUp(self) -> None:
        self.token = _fake_token(GOOD_PAYLOAD, sig="super-secret-signature-never-print-me")

    def test_render_and_json_never_contain_the_token(self) -> None:
        payload = decode_payload(self.token)
        result = assert_claims(
            payload, repository=REPOSITORY, expect_ref="refs/heads/main", expect_event="workflow_dispatch",
            environment="pypi", audience="pypi",
        )
        rendered = render_oidc(result)
        as_json = json.dumps(to_json_dict(result))
        self.assertNotIn(self.token, rendered)
        self.assertNotIn(self.token, as_json)
        # the payload's own jti claim (a token-identifying value) must not
        # leak through either -- it is never in PRINTABLE_CLAIMS.
        self.assertNotIn("super-secret-token-id-never-print-me", rendered)
        self.assertNotIn("super-secret-token-id-never-print-me", as_json)

    def test_error_messages_never_contain_the_token(self) -> None:
        # A transport-style failure's message is built from the URL only,
        # never the response body or the request token.
        def fetch(url: str, token: str) -> dict[str, object]:
            return {"unexpected": "shape"}

        try:
            request_oidc_token(fetch, request_url="https://example/token", request_token=self.token, audience="pypi")
        except OidcCheckError as exc:
            self.assertNotIn(self.token, str(exc))
        else:
            self.fail("expected OidcCheckError")

    def test_cli_never_prints_the_raw_token(self) -> None:
        from ci_lint.cli import _cmd_publish_oidc_check, build_parser

        def fake_fetch(url: str, token: str) -> dict[str, object]:
            assert token == "runtime-request-token"
            assert "audience=pypi" in url
            return {"value": self.token}

        parser = build_parser()
        args = parser.parse_args(
            ["publish", "oidc-check", "--repo", str(REPO), "--audience", "pypi", "--expect-ref", "refs/heads/main",
             "--expect-event", "workflow_dispatch"]
        )

        env = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example/token?api-version=2.0",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runtime-request-token",
            "GITHUB_REPOSITORY": REPOSITORY,
        }
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("ci_lint.cli.default_fetch", fake_fetch):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = _cmd_publish_oidc_check(args)

        self.assertEqual(0, exit_code)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(self.token, combined)
        self.assertNotIn("runtime-request-token", combined)
        self.assertIn("mock publish: stopping before upload", combined)

    def test_cli_refuses_when_mode_is_not_mock(self) -> None:
        from ci_lint.cli import _cmd_publish_oidc_check, build_parser

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            text = (REPO / "ci.toml").read_text(encoding="utf-8")
            rehearsal_text = text.replace('mode = "mock"', 'mode = "rehearsal"')
            self.assertNotEqual(text, rehearsal_text)  # sanity: the replace actually matched
            (repo_root / "ci.toml").write_text(rehearsal_text, encoding="utf-8")

            parser = build_parser()
            args = parser.parse_args(
                ["publish", "oidc-check", "--repo", str(repo_root), "--audience", "pypi"]
            )
            env = {
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example/token",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "should-never-be-used",
                "GITHUB_REPOSITORY": REPOSITORY,
            }
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = _cmd_publish_oidc_check(args)
            self.assertEqual(2, exit_code)

    def test_ci_toml_loads_and_is_mock_mode(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.assertIsNotNone(ci.publish.pypi)
        self.assertEqual("mock", ci.publish.pypi.mode)
