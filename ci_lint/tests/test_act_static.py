"""ACT-002 static clauses -- ci_lint/act_static.py (zackees/ci.yml#213)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ci_lint.act_static import check_act_002, check_act_003
from ci_lint.finding import Status


class Act002Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, text: str) -> None:
        (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.tmp / rel).write_text(text, encoding="utf-8")

    def test_clean_bosn_ci_run(self) -> None:
        self._write("ci/local.sh", "#!/bin/sh\n# act is not called here\nbosn ci run --workflow ci.yml\necho act\n")
        self._write("ci/gate.py", 'ARGV = ("bosn", "ci", "run", "--workflow", "ci.yml")\nACT = 1\n')
        self.assertEqual(check_act_002(self.tmp, (("python3", "ci/gate.py"),)), [])

    def test_zccache_shape_bare_act_and_legacy_flags(self) -> None:
        self._write("ci/docker/act/run.sh", "act -j test --action-offline-mode --action-cache-path /c\n")
        msgs = [f.message for f in check_act_002(self.tmp)]
        self.assertEqual(len(msgs), 3)
        self.assertTrue(any("bare 'act'" in m for m in msgs))
        self.assertTrue(any("--action-cache-path" in m for m in msgs))

    def test_python_argv_and_allow_comment(self) -> None:
        self._write(
            "ci/run.py",
            'A = ["act2", "-W", "x"]\nB = ["act", "-l"]  # ci-lint: allow ACT-002 bosn bootstrap\n'
            'C = ["bosn", "ci", "run", "--use-new-action-cache"]\n',
        )
        found = sorted((f.line, f.status) for f in check_act_002(self.tmp))
        self.assertEqual(found, [(1, Status.VIOLATION), (3, Status.VIOLATION)])

    def test_repo_owned_pin(self) -> None:
        self._write("scripts/install-act.sh", "ACT_VERSION=0.2.80\ncurl -L https://github.com/nektos/act/releases/download/v0.2.80/act.tgz\n")
        self.assertEqual(len(check_act_002(self.tmp)), 2)

    def test_gate_and_lane_argv(self) -> None:
        found = check_act_002(self.tmp, (("bosn", "ci", "run"), ("act", "-j", "lint")))
        self.assertEqual([f.message for f in found], ["lane 1: argv is bare 'act'"])

    def test_bosn_task(self) -> None:
        self._write("bosn.toml", '[task.a]\ncmd = "bosn ci run"\n[task.b]\ncmd = "cd /repo && /usr/local/bin/act -j t"\n')
        found = check_act_002(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("task 'b'", found[0].message)

    def test_act_variable_needs_review(self) -> None:
        self._write("ci/run.sh", '"$ACT_BIN" -j test\nif [ "$ACT" = true ]; then echo local; fi\n')
        found = check_act_002(self.tmp)
        self.assertEqual([f.status for f in found], [Status.NEEDS_REVIEW])


if __name__ == "__main__":
    unittest.main()


class Act003Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wf(self, text: str) -> None:
        path = self.tmp / ".github" / "workflows" / "ci.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_clud_setup_uv_exception_is_clean(self) -> None:
        self._wf(
            "jobs:\n  t:\n    steps:\n      - name: Set up uv\n        uses: astral-sh/setup-uv@v5\n"
            "        with:\n          # exception zackees/ci.yml#226\n"
            "          prune-cache: ${{ !env.ACT }}\n"
            "          cache-suffix: ${{ env.ACT && 'act-unpruned' || '' }}\n"
            "      - run: echo $ACT_BIN\n"
        )
        self.assertEqual(check_act_003(self.tmp), [])

    def test_soldr_save_cache_override_and_run_condition(self) -> None:
        self._wf(
            "jobs:\n  t:\n    steps:\n      - uses: zackees/setup-soldr@v0\n        with:\n"
            "          save-cache: ${{ env.ACT && 'true' || 'auto' }}\n"
            "      - if: ${{ !env.ACT }}\n        run: echo hi\n"
            "      - run: '[ \"$ACT\" = true ] && exit 0'\n"
            "      - uses: astral-sh/setup-uv@v5\n        with:\n          enable-cache: ${{ env.ACT }}\n"
            "      - run: echo ${{ env.ACT }}  # ci-lint: allow ACT-003 diagnostic only\n"
        )
        lines = sorted(f.line or 0 for f in check_act_003(self.tmp))
        self.assertEqual(lines, [6, 7, 9, 12])
