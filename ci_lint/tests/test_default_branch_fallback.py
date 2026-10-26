"""#491: an unspecified --default-branch is inferred, then falls back to
`main` if it exists, else `master` (with a warning), instead of assuming
`main`; an explicit `main` that does not exist becomes `master`."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_lint import cli
from ci_lint.github_api import GitHubApiError


def _offline(url: str, token: str) -> object:
    raise GitHubApiError(url)


class DefaultBranchFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GITHUB_EVENT_PATH", None)
        fetch = mock.patch.object(cli, "default_fetch", _offline)
        fetch.start()
        self.addCleanup(fetch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _repo(self, branch: str) -> Path:
        root = self.tmp / branch
        subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "x"],
            check=True,
        )
        return root

    def _resolve(self, explicit: str | None, root: Path) -> tuple[str | None, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            branch = cli._require_default_branch(explicit, "", "o/r", "audit", root)
        return branch, err.getvalue()

    def test_explicit_wins(self) -> None:
        self.assertEqual(self._resolve("dev", self._repo("master"))[0], "dev")

    def test_falls_back_to_master_with_warning(self) -> None:
        branch, err = self._resolve(None, self._repo("master"))
        self.assertEqual(branch, "master")
        self.assertIn("warning", err)

    def test_prefers_main_when_present(self) -> None:
        self.assertEqual(self._resolve(None, self._repo("main"))[0], "main")

    def test_explicit_main_missing_uses_master(self) -> None:
        branch, err = self._resolve("main", self._repo("master"))
        self.assertEqual(branch, "master")
        self.assertIn("warning", err)

    def test_inference_beats_fallback(self) -> None:
        event = self.tmp / "event.json"
        event.write_text('{"repository": {"default_branch": "trunk"}}', encoding="utf-8")
        os.environ["GITHUB_EVENT_PATH"] = str(event)
        self.assertEqual(self._resolve(None, self._repo("main"))[0], "trunk")

    def test_neither_branch_is_an_error(self) -> None:
        self.assertIsNone(self._resolve(None, self._repo("trunk"))[0])


if __name__ == "__main__":
    unittest.main()
