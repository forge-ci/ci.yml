"""TEST-003 -- ci_lint/rules/test_parallelism.py (zackees/ci.yml#152)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.test_parallelism import check_test_003, workers_flag

_WF = """name: ci
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: {cmd}
"""


def _repo(tmp: str, cmd: str, evidence: dict[str, float] | None = None, script: str | None = None) -> Path:
    root = Path(tmp)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(_WF.format(cmd=cmd), encoding="utf-8")
    if evidence is not None or script is not None:
        (root / "ci").mkdir()
    if evidence is not None:
        (root / "ci" / "serial-utilization.json").write_text(json.dumps(evidence), encoding="utf-8")
    if script is not None:
        (root / "ci" / "test.py").write_text(script, encoding="utf-8")
    return root


_GOOD = {"sum_test_seconds": 60.0, "wall_seconds": 200.0, "physical_cores": 2, "logical_cores": 4}


class WorkersFlagTest(unittest.TestCase):
    def test_shapes(self) -> None:
        self.assertEqual("-n auto", workers_flag(("uv", "run", "pytest", "-n", "auto")))
        self.assertEqual("-n4", workers_flag(("python", "-m", "pytest", "-n4")))
        self.assertEqual("--numprocesses=2", workers_flag(("pytest", "--numprocesses=2")))
        self.assertIsNone(workers_flag(("pytest", "-n", "0")))
        self.assertIsNone(workers_flag(("pytest", "tests")))
        self.assertIsNone(workers_flag(("sort", "-n", "x")))


class Test003(unittest.TestCase):
    def test_no_evidence_is_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            findings = check_test_003(_repo(tmp, "uv run pytest -n auto"))
        self.assertEqual(1, len(findings))
        self.assertEqual(Status.NEEDS_REVIEW, findings[0].status)
        self.assertIn("-n auto", findings[0].message)

    def test_serial_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], check_test_003(_repo(tmp, "uv run pytest tests")))

    def test_allow_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp, "pytest -n 4  # ci-lint: allow TEST-003 io-bound network suite")
            self.assertEqual([], check_test_003(repo))

    def test_valid_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], check_test_003(_repo(tmp, "pytest -n auto", _GOOD)))

    def test_cpu_bound_evidence_is_review(self) -> None:
        bad = dict(_GOOD, sum_test_seconds=135.0, wall_seconds=135.0)
        with tempfile.TemporaryDirectory() as tmp:
            findings = check_test_003(_repo(tmp, "pytest -n auto", bad))
        self.assertEqual(1, len(findings))
        self.assertIn("CPU-bound", findings[0].message)

    def test_malformed_evidence_is_review(self) -> None:
        bad = dict(_GOOD, physical_cores=0)
        with tempfile.TemporaryDirectory() as tmp:
            findings = check_test_003(_repo(tmp, "pytest -n auto", bad))
        self.assertIn("positive number", findings[0].message)

    def test_script_one_level(self) -> None:
        script = 'import subprocess\nsubprocess.run(["uv", "run", "pytest", "-n", "auto"], check=True)\n'
        with tempfile.TemporaryDirectory() as tmp:
            findings = check_test_003(_repo(tmp, "python ci/test.py", script=script))
        self.assertEqual(["ci/test.py"], [f.path for f in findings])


if __name__ == "__main__":
    unittest.main()
