"""`ci-lint tests size` -- ci_lint/runtime/tests_size.py.

Uses a small recorded `--message-format=json` fixture plus three tiny,
fixed-size dummy "executable" files checked into
`ci_lint/tests/fixtures/runtime/tests-size/bin/` (round-2A brief, part 2b).
The fixture's `<FIXTURE_DIR>` placeholder is substituted with the fixture
directory's actual absolute path at test time, since a real `executable`
path is always absolute and this fixture must stay portable across
machines/checkouts.
"""

from __future__ import annotations

import unittest

from ci_lint.cargo_messages import parse_compiler_artifacts
from ci_lint.runtime.tests_size import compute_tests_size, render_text
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES


class TestsSizeRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FIXTURES / "runtime" / "tests-size"
        _loaded = load_ci_toml(self.repo)
        self.ci = _loaded.ci
        findings = _loaded.findings
        self.assertIsNotNone(self.ci, msg=str(findings))
        raw = (self.repo / "artifacts.jsonl").read_text(encoding="utf-8")
        raw = raw.replace("<FIXTURE_DIR>", str(self.repo))
        self.artifacts = parse_compiler_artifacts(raw.splitlines())

    def test_only_test_profile_artifacts_with_an_executable_are_measured(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        names = {b.name for b in report.binaries}
        # The 4th fixture record (profile.test=false, executable=null) must
        # not appear.
        self.assertEqual({"demo-core:lib", "demo-core:test:extra", "demo-cli:bin:cli"}, names)

    def test_sizes_match_the_dummy_files_on_disk(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        sizes = {b.name: b.bytes for b in report.binaries}
        self.assertEqual(150, sizes["demo-core:lib"])
        self.assertEqual(100, sizes["demo-core:test:extra"])
        self.assertEqual(50, sizes["demo-cli:bin:cli"])

    def test_undeclared_binary_is_rust_012(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        messages = [f.message for f in report.findings if f.rule == "RUST-012"]
        self.assertTrue(any("undeclared test binary 'demo-cli:bin:cli'" in m for m in messages), msg=messages)

    def test_declared_but_missing_binary_is_rust_012(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        messages = [f.message for f in report.findings if f.rule == "RUST-012"]
        self.assertTrue(any("'demo:lib' was not produced" in m for m in messages), msg=messages)

    def test_over_max_binary_is_rust_012(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        messages = [f.message for f in report.findings if f.rule == "RUST-012"]
        self.assertTrue(any("'demo-core:lib' is 150 B" in m and "max-binary" in m for m in messages), msg=messages)

    def test_over_max_total_is_rust_012(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        self.assertEqual(300, report.total_bytes)
        messages = [f.message for f in report.findings if f.rule == "RUST-012"]
        self.assertTrue(any("max-total" in m for m in messages), msg=messages)

    def test_render_text_does_not_crash(self) -> None:
        report = compute_tests_size(self.ci, self.artifacts)
        text = render_text(report)
        self.assertIn("demo-core:lib", text)
        self.assertIn("TOTAL", text)


if __name__ == "__main__":
    unittest.main()
