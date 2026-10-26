"""GEN-021 static half -- ci_lint/rules/default_branch_skip.py (zackees/ci.yml#156):
a default-branch push may skip a gate-required job only through verified
reuse (`ci-lint reuse-check`), never through a blanket job-level `if:`.

RED -> GREEN: fixtures/GEN-021/red (two blanket skips, no reuse decision) is
a violation; fixtures/GEN-021/green (the design's clud-shaped reference
wiring) is clean; every review-* fixture is `needs_review`, never a pass.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.precheck import run_precheck
from ci_lint.rules.default_branch_skip import check_gen_021, classify_condition
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class ClassifyConditionTest(unittest.TestCase):
    def test_blanket_default_branch_exclusions(self) -> None:
        for cond in (
            "github.event_name != 'push'",
            "${{ github.event_name == 'pull_request' }}",
            "github.ref != 'refs/heads/main' && needs.static.result == 'success'",
            'github.ref_name != "main"',
            "startsWith(github.ref, 'refs/pull/')",
            "!startsWith(github.ref, 'refs/heads/main')",
            "(github.event_name != 'push') && always()",
        ):
            self.assertEqual(classify_condition(cond), "skip", cond)

    def test_conditions_that_do_not_skip_default_branch_pushes_or_never_run_on_prs(self) -> None:
        for cond in (
            "needs.static.outputs.mode != 'windows'",
            "needs.static.outputs.reuse != 'true' && needs.static.outputs.mode != ''",
            "github.event_name != 'schedule'",
            "github.event_name == 'push'",
            "github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'",
            "github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'",
            "always() && github.event_name == 'push' && (needs.a.result == 'success' || needs.b.result == 'skipped')",
            "github.event_name == 'push' && github.ref != 'refs/heads/main'",
        ):
            self.assertEqual(classify_condition(cond), "clean", cond)

    def test_unresolvable_event_logic(self) -> None:
        for cond in (
            "github.event_name != 'push' || contains(github.event.head_commit.message, '[ci full]')",
            "!(github.event_name == 'push')",
            "contains(github.ref, 'main')",
            "github.event_name == 'pull_request' || github.event_name == 'schedule'",
        ):
            self.assertEqual(classify_condition(cond), "unknown", cond)

    def test_default_branch_name_is_respected(self) -> None:
        self.assertEqual(classify_condition("github.ref != 'refs/heads/master'", "master"), "skip")
        self.assertEqual(classify_condition("github.ref != 'refs/heads/master'", "main"), "clean")


@requires_yaml_tooling
class Gen021Test(unittest.TestCase):
    def _statuses(self, kind: str) -> list[Status]:
        findings = check_gen_021(fixture("GEN-021", kind))
        self.assertTrue(all(f.rule == "GEN-021" for f in findings))
        return [f.status for f in findings]

    def test_red_blanket_skips_are_violations(self) -> None:
        findings = check_gen_021(fixture("GEN-021", "red"))
        self.assertEqual([f.status for f in findings], [Status.VIOLATION, Status.VIOLATION])
        self.assertTrue(any("jobs.clippy" in f.message for f in findings))
        self.assertTrue(any("jobs.unit" in f.message for f in findings))
        self.assertTrue(all("no verified-reuse decision" in f.message for f in findings))

    def test_green_reference_wiring_is_clean(self) -> None:
        self.assertEqual(self._statuses("green"), [])

    def test_unresolvable_condition_is_needs_review(self) -> None:
        self.assertEqual(self._statuses("review-unresolvable"), [Status.NEEDS_REVIEW])

    def test_no_gate_job_is_needs_review_not_violation(self) -> None:
        self.assertEqual(self._statuses("review-no-gate"), [Status.NEEDS_REVIEW])

    def test_blanket_skip_beside_a_reuse_decision_is_needs_review(self) -> None:
        findings = check_gen_021(fixture("GEN-021", "review-partial"))
        self.assertEqual([f.status for f in findings], [Status.NEEDS_REVIEW])
        self.assertIn("jobs.clippy", findings[0].message)

    def test_hand_written_reuse_flag_is_needs_review(self) -> None:
        findings = check_gen_021(fixture("GEN-021", "review-unverified"))
        self.assertTrue(findings)
        self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in findings))
        self.assertTrue(any("does not run `ci-lint reuse-check`" in f.message for f in findings))

    def test_reuse_check_delegated_to_a_ci_script_counts(self) -> None:
        # forge-ci/ci.yml#52 / template-python-rust-cmd#50: the template's
        # decision job runs `python3 ci/reuse_decision.py`, whose argv list
        # spells `"-m", "ci_lint", "reuse-check"` one token per line. That is
        # a real decision, not a hand-written flag.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(fixture("GEN-021", "review-unverified"), repo)
            (repo / "ci").mkdir(exist_ok=True)
            (repo / "ci" / "always_reuse.py").write_text(
                "import subprocess, sys\n"
                "cmd = [\n    sys.executable,\n    \"-m\",\n    \"ci_lint\",\n    \"reuse-check\",\n"
                "    \"--required-job\",\n    f\"Test linux-x64 (unit) [{sys.argv[1]}]\",\n]\n"
                "raise SystemExit(subprocess.run(cmd, check=False).returncode)\n",
                encoding="utf-8",
            )
            findings = check_gen_021(repo)
        self.assertFalse(any("does not run `ci-lint reuse-check`" in f.message for f in findings), findings)
        self.assertEqual(findings, [])

    def test_script_without_reuse_check_stays_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(fixture("GEN-021", "review-unverified"), repo)
            (repo / "ci").mkdir(exist_ok=True)
            (repo / "ci" / "always_reuse.py").write_text("print('reuse=true')\n", encoding="utf-8")
            findings = check_gen_021(repo)
        self.assertTrue(any("does not run `ci-lint reuse-check`" in f.message for f in findings))

    def test_coverage_and_consumption_gaps_are_needs_review(self) -> None:
        findings = check_gen_021(fixture("GEN-021", "review-coverage"))
        messages = " | ".join(f.message for f in findings)
        self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in findings))
        self.assertIn("jobs.integration", messages)
        self.assertIn("'Old job name' matches no job", messages)
        self.assertIn("== 'false'", messages)
        self.assertNotIn("jobs.unit", messages)


@requires_yaml_tooling
class PrecheckWiringTest(unittest.TestCase):
    """GEN-021 runs inside `ci-lint precheck`, and a `[[exceptions]]` entry
    (a live-proven merge queue, per GEN-010) is the documented exception."""

    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp) / "repo"
        shutil.copytree(fixture("_e2e", "green"), repo)
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        shutil.copy(fixture("GEN-021", "red") / ".github" / "workflows" / "ci.yml", workflows / "gen021.yml")
        return repo

    def test_precheck_reports_gen_021(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_precheck(self._repo(tmp))
        gen = [f for f in result.findings if f.rule == "GEN-021"]
        self.assertEqual([f.status for f in gen], [Status.VIOLATION, Status.VIOLATION])

    def test_exception_entry_suppresses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            with (repo / "ci.toml").open("a", encoding="utf-8") as fh:
                fh.write(
                    "\n[[exceptions]]\n"
                    'rule = "GEN-021"\n'
                    'path = ".github/workflows/gen021.yml"\n'
                    'reason = "merge queue validates the exact merge commit (GEN-010 live audit green)"\n'
                    'issue = "https://github.com/zackees/ci.yml/issues/156"\n'
                    'expires = "2099-01-01"\n'
                )
            result = run_precheck(repo)
        gen = [f for f in result.findings if f.rule == "GEN-021"]
        self.assertEqual([f.status for f in gen], [Status.APPROVED_EXCEPTION, Status.APPROVED_EXCEPTION])


if __name__ == "__main__":
    unittest.main()
