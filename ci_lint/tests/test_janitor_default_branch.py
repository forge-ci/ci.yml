"""zackees/ci.yml#463: `ci-lint cache janitor --dry-run` without
`--default-branch` crashed because `infer_default_branch` handed the real
`default_fetch` a relative `/repos/{repo}` path, which urllib rejects.

The stub sits at the HTTP layer (`urllib.request.urlopen`), so any
relative or non-GitHub URL reaching the transport fails this test."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ci_lint import cli
from ci_lint.tests.helpers import FIXTURES

REPO = FIXTURES / "runtime" / "cache" / "repo"
SLUG = "zackees/example"


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class JanitorInfersDefaultBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.urls: list[str] = []
        env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": SLUG}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GITHUB_EVENT_PATH", None)

    def _urlopen(self, request: object, timeout: float = 0) -> _Response:
        url = request.full_url  # type: ignore[attr-defined]
        self.urls.append(url)
        if not url.startswith("https://api.github.com/"):
            raise AssertionError(f"non-absolute GitHub API URL reached the transport: {url!r}")
        if url == f"https://api.github.com/repos/{SLUG}":
            body: object = {"default_branch": "master"}
        elif url.endswith("/graphql"):
            body = {"data": {}}
        elif "/actions/caches" in url:
            body = {"total_count": 0, "actions_caches": []}
        elif "/actions/runs" in url:
            body = {"total_count": 0, "workflow_runs": []}
        else:
            body = {}
        return _Response(json.dumps(body).encode())

    def test_dry_run_without_default_branch(self) -> None:
        out = io.StringIO()
        with mock.patch("ci_lint.github_api.urllib.request.urlopen", side_effect=self._urlopen), redirect_stdout(out):
            rc = cli.main(["cache", "janitor", "--repo", str(REPO), "--dry-run"])
        self.assertIn(f"https://api.github.com/repos/{SLUG}", self.urls)
        self.assertEqual(0, rc, out.getvalue())


if __name__ == "__main__":
    unittest.main()
