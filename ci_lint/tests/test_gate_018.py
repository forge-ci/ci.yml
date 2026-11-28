"""GATE-018: script-style tests must be run by the gate (ci_lint/rules/script_tests.py)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.rules.script_tests import check_gate_018, is_script_test

SCRIPT = 'def main():\n    return 0\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
COLLECTED = "def test_ok():\n    assert True\n"


class Gate015Test(unittest.TestCase):
    def repo(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return root

    def rules(self, root: Path, runs=()) -> list[tuple[str | None, str]]:
        return [(f.path, f.rule) for f in check_gate_018(root, runs)]

    def test_classifies_script_tests(self):
        self.assertTrue(is_script_test(SCRIPT))
        self.assertFalse(is_script_test(COLLECTED + SCRIPT))
        self.assertFalse(is_script_test("import unittest\nclass T(unittest.TestCase): pass\n" + SCRIPT))

    def test_no_workflow_no_gate_is_a_violation(self):
        # The gh-runner#249 shape: tests, no workflows, no gate.
        root = self.repo({"tests/lanes_test.py": SCRIPT, "tests/test_x.py": COLLECTED})
        self.assertIn((".", "GATE-018"), self.rules(root))
        self.assertIn(("tests/lanes_test.py", "GATE-018"), self.rules(root))

    def test_gate_naming_the_script_passes(self):
        root = self.repo({"tests/lanes_test.py": SCRIPT})
        self.assertEqual(self.rules(root, (("uv", "run", "tests/lanes_test.py"),)), [])

    def test_gate_script_naming_it_passes(self):
        root = self.repo({"tests/lanes_test.py": SCRIPT, "ci/gate.sh": "uv run tests/lanes_test.py\n"})
        self.assertEqual(self.rules(root, (("bash", "ci/gate.sh"),)), [])

    def test_workflow_naming_it_passes(self):
        root = self.repo({"tests/lanes_test.py": SCRIPT,
                          ".github/workflows/ci.yml": "jobs: {t: {steps: [{run: python tests/lanes_test.py}]}}\n"})
        self.assertEqual(self.rules(root), [])

    def test_gate_without_it_is_a_violation(self):
        root = self.repo({"tests/lanes_test.py": SCRIPT})
        self.assertEqual(self.rules(root, (("pytest", "-q", "tests"),)), [("tests/lanes_test.py", "GATE-018")])

    def test_collected_tests_and_allow_comment_pass(self):
        root = self.repo({"tests/test_x.py": COLLECTED,
                          "tests/slow_test.py": "# ci-lint: allow GATE-018 manual benchmark\n" + SCRIPT})
        self.assertEqual(self.rules(root, (("pytest",),)), [])


if __name__ == "__main__":
    unittest.main()
