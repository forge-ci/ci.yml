"""`ci_lint.reuse.compute_reuse` -- title-edit reuse lookup (round-3A brief,
Part 2). No network: every test injects a fake `fetch` that serves the
recorded fixtures under ci_lint/tests/fixtures/runtime/reuse/.
"""

from __future__ import annotations

import json
import unittest

from ci_lint.github_api import FetchFn, GitHubApiError, JsonValue
from ci_lint.reuse import compute_reuse, empty_result
from ci_lint.tests.helpers import FIXTURES

REUSE_FIXTURES = FIXTURES / "runtime" / "reuse"

HEAD_SHA = "deadbeef00000000000000000000000000000000"
REPO = "zackees/template-python-rust-cmd"


def _load(name: str) -> JsonValue:
    return json.loads((REUSE_FIXTURES / name).read_text(encoding="utf-8"))


def _fake_fetch(mapping: dict[str, JsonValue], calls: list[str] | None = None) -> FetchFn:
    """A fetch that matches by substring against `mapping`'s keys, and
    raises GitHubApiError (matching `default_fetch`'s contract) for
    anything not explicitly stubbed -- so a bug that fetches a URL this
    test didn't expect fails loudly instead of silently."""

    def fetch(url: str, token: str) -> JsonValue:
        assert token  # every real call always carries a token
        if calls is not None:
            calls.append(url)
        for needle, payload in mapping.items():
            if needle in url:
                return payload
        raise GitHubApiError(f"unexpected fetch (no fixture stubbed): {url}")

    return fetch


class ComputeReuseTest(unittest.TestCase):
    def test_empty_lane_digests_short_circuits_without_any_fetch(self) -> None:
        def fetch(url: str, token: str) -> JsonValue:
            raise AssertionError("must not be called when lane_digests is empty")

        result = compute_reuse(
            repo=REPO, head_sha=HEAD_SHA, current_run_id="3003", lane_digests={}, fetch=fetch, token="t"
        )
        self.assertEqual({}, dict(result.reuse))
        self.assertIsNone(result.warning)

    def test_matching_digest_on_a_successful_job_is_reused(self) -> None:
        calls: list[str] = []
        fetch = _fake_fetch(
            {"actions/runs?": _load("runs.json"), "runs/3002/jobs": _load("jobs-3002.json")}, calls
        )
        result = compute_reuse(
            repo=REPO,
            head_sha=HEAD_SHA,
            current_run_id="3003",
            lane_digests={"fast": "aaaaaaaaaaaa", "dylint": "bbbbbbbbbbbb"},
            fetch=fetch,
            token="t",
        )
        self.assertIsNone(result.warning)
        fast = result.reuse["fast"]
        assert fast is not None
        self.assertEqual(3002, fast.run_id)
        self.assertEqual(9002, fast.job_id)
        self.assertTrue(fast.html_url.endswith("/job/9002"))
        # dylint's job matched the digest but its conclusion was "failure",
        # not "success" -- never reused.
        self.assertIsNone(result.reuse["dylint"])
        # run 3001's path is a different workflow file -- never even
        # queried for jobs (the fake would raise if it were, since only
        # runs/3002/jobs is stubbed).
        self.assertTrue(all("3001" not in c for c in calls))
        self.assertTrue(any("3002/jobs" in c for c in calls))

    def test_current_run_is_excluded(self) -> None:
        fetch = _fake_fetch({"actions/runs?": _load("runs.json")})
        result = compute_reuse(
            repo=REPO,
            head_sha=HEAD_SHA,
            current_run_id="3002",  # the only run with a matching-digest job
            lane_digests={"fast": "aaaaaaaaaaaa"},
            fetch=fetch,
            token="t",
        )
        # 3002 excluded (it's "current"); 3001's path doesn't match the
        # workflow file; nothing left to find a reuse in.
        self.assertIsNone(result.reuse["fast"])

    def test_no_matching_digest_is_not_reused(self) -> None:
        fetch = _fake_fetch({"actions/runs?": _load("runs.json"), "runs/3002/jobs": _load("jobs-3002.json")})
        result = compute_reuse(
            repo=REPO,
            head_sha=HEAD_SHA,
            current_run_id="3003",
            lane_digests={"platform:windows-x64": "dddddddddddd"},  # jobs-3002 has [cccccccccccc]
            fetch=fetch,
            token="t",
        )
        self.assertIsNone(result.reuse["platform:windows-x64"])
        self.assertIsNone(result.warning)

    def test_api_error_on_the_runs_lookup_reuses_nothing_and_warns(self) -> None:
        def fetch(url: str, token: str) -> JsonValue:
            raise GitHubApiError("boom")

        result = compute_reuse(
            repo=REPO,
            head_sha=HEAD_SHA,
            current_run_id="3003",
            lane_digests={"fast": "aaaaaaaaaaaa"},
            fetch=fetch,
            token="t",
        )
        self.assertIsNone(result.reuse["fast"])
        assert result.warning is not None
        self.assertIn("boom", result.warning)

    def test_api_error_on_a_jobs_lookup_still_returns_other_lanes_and_warns(self) -> None:
        def fetch(url: str, token: str) -> JsonValue:
            if "actions/runs?" in url:
                return _load("runs.json")
            raise GitHubApiError("jobs endpoint down")

        result = compute_reuse(
            repo=REPO,
            head_sha=HEAD_SHA,
            current_run_id="3003",
            lane_digests={"fast": "aaaaaaaaaaaa"},
            fetch=fetch,
            token="t",
        )
        self.assertIsNone(result.reuse["fast"])
        assert result.warning is not None
        self.assertIn("jobs endpoint down", result.warning)

    def test_empty_result_helper(self) -> None:
        result = empty_result({"fast": "x", "dylint": "y"}, warning="reuse: unreachable")
        self.assertEqual({"fast": None, "dylint": None}, dict(result.reuse))
        self.assertEqual("reuse: unreachable", result.warning)
        self.assertEqual({"fast": None, "dylint": None}, result.to_json_dict())


if __name__ == "__main__":
    unittest.main()
