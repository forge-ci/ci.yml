"""CACHE-025 -- ci_lint/rules/swatinem_ban.py (zackees/ci.yml#209): a
`uses: Swatinem/rust-cache@...` step is banned; Rust build caching goes
through setup-soldr / soldr. Maintainer decision 2026-10-02: no exceptions --
neither a same-line `# ci-lint: allow CACHE-025 <reason>` nor a ci.toml
`[[exceptions]]` entry excuses one (not a soldr bootstrap job, not a
benchmark baseline)."""

from __future__ import annotations

import datetime
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ci_lint.exceptions import apply_exceptions
from ci_lint.finding import Status
from ci_lint.precheck import run_precheck
from ci_lint.schema import ExceptionEntry, load_ci_toml
from ci_lint.rules.swatinem_ban import check_cache_025, scan_text
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class Cache025Test(unittest.TestCase):
    def test_green_setup_soldr_and_comments_are_clean(self) -> None:
        self.assertEqual([], check_cache_025(fixture("CACHE-025", "green")))

    def test_red_same_line_allow_with_reason_is_still_a_violation(self) -> None:
        # The former benchmark-baseline carve-out: a reasoned same-line
        # allow no longer excuses the step (maintainer decision 2026-10-02).
        findings = check_cache_025(fixture("CACHE-025", "red-allow"))
        self.assertEqual([(".github/workflows/bench.yml", 8)], [(f.path, f.line) for f in findings])
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))
        self.assertNotIn("allow CACHE-025", findings[0].fix)

    def test_ci_toml_exception_cannot_waive_cache_025(self) -> None:
        ci = load_ci_toml(fixture("_e2e", "green")).ci
        assert ci is not None
        entry = ExceptionEntry(
            rule="CACHE-025",
            path=".github/workflows/bench.yml",
            reason="benchmark baseline",
            issue="https://github.com/zackees/ci.yml/issues/209",
            expires="2999-01-01",
        )
        ci = replace(ci, exceptions=(entry,))
        findings = check_cache_025(fixture("CACHE-025", "red-allow"))
        outcome = apply_exceptions(ci, findings, today=datetime.date(2026, 10, 2))
        statuses = {(f.rule, f.path): f.status for f in outcome.findings}
        self.assertEqual(Status.VIOLATION, statuses[("CACHE-025", ".github/workflows/bench.yml")])
        # The entry itself is reported, so it cannot sit in ci.toml unnoticed.
        self.assertEqual(Status.VIOLATION, statuses[("CACHE-025", "ci.toml")])
        self.assertEqual([], outcome.matched)

    def test_red_workflow_steps(self) -> None:
        findings = check_cache_025(fixture("CACHE-025", "red"))
        # The quoted, SHA-pinned, lower-case form still matches, allow
        # comment or not.
        self.assertEqual(
            [("CACHE-025", ".github/workflows/ci.yml", 8), ("CACHE-025", ".github/workflows/ci.yml", 17)],
            [(f.rule, f.path, f.line) for f in findings],
        )
        self.assertTrue(all(f.status == Status.VIOLATION for f in findings))
        self.assertIn("setup-soldr", findings[0].fix)

    def test_red_composite_action(self) -> None:
        findings = check_cache_025(fixture("CACHE-025", "red-action"))
        self.assertEqual([(".github/actions/rust/action.yml", 5)], [(f.path, f.line) for f in findings])

    def test_lookalike_actions_do_not_match(self) -> None:
        text = "steps:\n  - uses: Swatinem/rust-cache-fork@v1\n  - uses: actions/cache@v4\n"
        self.assertEqual([], scan_text(text, "w.yml"))

    @requires_yaml_tooling
    def test_precheck_reports_cache_025(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            shutil.copytree(fixture("_e2e", "green"), root)
            action_dir = root / ".github" / "actions" / "swatinem"
            action_dir.mkdir(parents=True)
            shutil.copy(fixture("CACHE-025", "red-action") / ".github" / "actions" / "rust" / "action.yml", action_dir)
            result = run_precheck(root, title="[ci-windows] x")
        self.assertIn("CACHE-025", {f.rule for f in result.findings})


if __name__ == "__main__":
    unittest.main()
