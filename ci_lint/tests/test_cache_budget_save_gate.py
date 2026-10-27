"""`ci-lint cache budget` also reports ungated setup-uv saves when
[cache.pr].mode = "none" -- the cause of a PR's own entries, found from the
workflow files rather than only the live symptom (FastLED #4921)."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_lint import cli
from ci_lint.cache.budget import BudgetVerdict
from ci_lint.tests.helpers import fixture, requires_yaml_tooling

_PR_DELTA = 'pr       = { mode = "delta", families = ["compile"], max-per-pr = "128MB", budget = "1GB", trim = "on-close" }'
_PR_NONE = 'pr       = { mode = "none", families = [], trim = "on-close" }'


class BudgetSaveGateTest(unittest.TestCase):
    def _repo(self, mode_line: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        shutil.copytree(fixture("CACHE-003", "red"), repo)
        toml = repo / "ci.toml"
        text = toml.read_text(encoding="utf-8")
        self.assertIn(_PR_DELTA, text)
        text = text.replace(_PR_DELTA, mode_line)
        # Drop the explicit opt-in so only the mode implies the gate.
        text = text.replace('setup-uv          = { require = { save-cache = "plan" } }\n', "")
        toml.write_text(text, encoding="utf-8")
        return repo

    def _run(self, repo: Path) -> tuple[int, str]:
        ok = BudgetVerdict("ok", 0, None, 0, 0, "within [cache].budget")
        out = io.StringIO()
        env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "pull_request"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "cache_run_budget", return_value=ok), \
                contextlib.redirect_stdout(out):
            code = cli.main(["cache", "budget", "--repo", str(repo), "--default-branch", "main"])
        return code, out.getvalue()

    @requires_yaml_tooling
    def test_mode_none_fails_on_ungated_setup_uv(self) -> None:
        code, out = self._run(self._repo(_PR_NONE))
        self.assertEqual(1, code)
        self.assertIn("CACHE-003", out)
        self.assertIn(".github/workflows/ci.yml", out)

    @requires_yaml_tooling
    def test_pr_caching_mode_is_unaffected(self) -> None:
        code, out = self._run(self._repo(_PR_DELTA))
        self.assertEqual(0, code)
        self.assertNotIn("CACHE-003", out)


if __name__ == "__main__":
    unittest.main()
