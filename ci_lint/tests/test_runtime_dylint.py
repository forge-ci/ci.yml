"""`ci-lint dylint coverage` -- ci_lint/runtime/dylint.py (round-6E, RUST-003).

Uses `ci_lint/tests/fixtures/runtime/dylint/`: the canonical example's
ci.toml (6 declared platforms, `[lint.dylint].targets = "all-platforms"`),
a real trimmed GitHub Actions job log from a green
zackees/template-python-rust-cmd `dylint` run (`logs/
template-dylint-green.log`, `gh run view --job 109219473942 --log`) and its
`--results-out` JSON shape (`results/template-dylint-green.json`), plus
synthetic RED variants: one target's `--target` flag missing from the
invocation line, a sequential-shape results JSON with one target failed and
the next target never attempted, and an E0463 "target may not be
installed" raw-log failure.
"""

from __future__ import annotations

import unittest

from ci_lint.runtime.dylint import DylintCoverageError, compute_dylint_coverage, render_text
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

ALL_SIX_TARGETS = frozenset(
    {
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-pc-windows-msvc",
        "aarch64-pc-windows-msvc",
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
    }
)


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]


class DylintCoverageRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FIXTURES / "runtime" / "dylint"
        _loaded = load_ci_toml(self.repo)
        self.ci = _loaded.ci
        findings = _loaded.findings
        self.assertIsNotNone(self.ci, msg=str(findings))

    # -- GREEN: real evidence, both shapes --------------------------------

    def test_real_green_log_covers_every_declared_target(self) -> None:
        report = compute_dylint_coverage(self.ci, [self.repo / "logs" / "template-dylint-green.log"])
        self.assertEqual((), report.findings)
        self.assertEqual(ALL_SIX_TARGETS, report.covered_targets)
        self.assertEqual(6, len(report.required))

    def test_results_json_shape_covers_every_declared_target(self) -> None:
        report = compute_dylint_coverage(
            self.ci, [self.repo / "results" / "template-dylint-green.json"]
        )
        self.assertEqual((), report.findings)
        self.assertEqual(ALL_SIX_TARGETS, report.covered_targets)

    def test_render_text_does_not_crash(self) -> None:
        report = compute_dylint_coverage(self.ci, [self.repo / "logs" / "template-dylint-green.log"])
        text = render_text(report)
        self.assertIn("linux-x64", text)
        self.assertIn("0 violation", text)

    # -- RED: one target missing from the raw-log invocation line --------

    def test_raw_log_missing_target_is_rust_003(self) -> None:
        report = compute_dylint_coverage(self.ci, [self.repo / "logs" / "red-missing-target.log"])
        self.assertEqual(["RUST-003"], rule_ids(report.findings))
        self.assertIn("windows-x64", report.findings[0].message)
        self.assertIn("x86_64-pc-windows-msvc", report.findings[0].message)
        # every OTHER declared target is still covered -- only the omitted
        # one is flagged.
        self.assertNotIn("x86_64-pc-windows-msvc", report.covered_targets)
        self.assertEqual(ALL_SIX_TARGETS - {"x86_64-pc-windows-msvc"}, report.covered_targets)

    # -- RED: E0463 (rust-std never provisioned) --------------------------

    def test_e0463_raw_log_is_rust_003_with_prepare_fix(self) -> None:
        report = compute_dylint_coverage(self.ci, [self.repo / "logs" / "red-e0463.log"])
        rules = rule_ids(report.findings)
        self.assertIn("RUST-003", rules)
        e0463_findings = [f for f in report.findings if "E0463" in f.message]
        self.assertEqual(1, len(e0463_findings))
        self.assertIn("aarch64-pc-windows-msvc", e0463_findings[0].message)
        self.assertIn("soldr dylint prepare --target aarch64-pc-windows-msvc", e0463_findings[0].fix)
        # the build aborted with no 'Finished' marker after the toolchain
        # banner -- every OTHER target named on that same invocation line is
        # ALSO unconfirmed (no double-counted E0463 finding, but still not
        # "covered").
        self.assertNotIn("aarch64-unknown-linux-gnu", report.covered_targets)

    # -- RED: sequential results.json, one failed + one never attempted --

    def test_sequential_results_json_failed_and_unattempted_targets(self) -> None:
        report = compute_dylint_coverage(
            self.ci, [self.repo / "results" / "red-sequential-one-failed.json"]
        )
        messages = " ".join(f.message for f in report.findings)
        self.assertIn("aarch64-pc-windows-msvc", messages)  # explicitly failed (rc=101)
        self.assertIn("x86_64-pc-windows-msvc", messages)  # never attempted (sequential stopped)
        self.assertEqual(2, len(report.findings))
        self.assertTrue(all(f.rule == "RUST-003" for f in report.findings))
        self.assertEqual(ALL_SIX_TARGETS - {"aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc"}, report.covered_targets)

    # -- No evidence at all: every declared target is unconfirmed ---------

    def test_no_log_given_flags_every_target(self) -> None:
        report = compute_dylint_coverage(self.ci, [])
        self.assertEqual(6, len(report.findings))
        self.assertTrue(all(f.rule == "RUST-003" for f in report.findings))

    # -- Errors: exit-2 shaped, never silently "zero coverage" -------------

    def test_missing_log_file_raises(self) -> None:
        with self.assertRaises(DylintCoverageError):
            compute_dylint_coverage(self.ci, [self.repo / "does-not-exist.log"])

    def test_malformed_results_json_raises(self) -> None:
        with self.assertRaises(DylintCoverageError):
            compute_dylint_coverage(self.ci, [self.repo / "results" / "malformed.json"])


if __name__ == "__main__":
    unittest.main()
