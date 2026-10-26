"""CACHE-011, CACHE-012 -- ci_lint/cache/save_evidence.py (issue #7)."""

from __future__ import annotations

import unittest

from ci_lint.cache.save_evidence import check_cache_011, check_cache_012
from ci_lint.tests.helpers import fixture


class Cache011Test(unittest.TestCase):
    def test_real_github_job_fields_and_action_elapsed_time(self) -> None:
        """Clud main 37189270080: two successful Dylint layers saved nothing."""
        log = fixture("CACHE-011", "github") / "red.log"
        events, findings = check_cache_011([log], conclusion="success")
        self.assertEqual([e.layer for e in events], ["dylint-cache", "dylint-output-cache"])
        self.assertEqual(len(findings), 2)

    def test_prefixed_exact_hits_remain_healthy(self) -> None:
        log = fixture("CACHE-011", "github") / "green.log"
        events, findings = check_cache_011([log], conclusion="success")
        self.assertEqual(events, ())
        self.assertEqual(findings, ())

    def test_prefixed_failed_or_cancelled_runs_do_not_fire(self) -> None:
        log = fixture("CACHE-011", "github") / "red.log"
        for conclusion in ("failure", "cancelled"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(tuple(check_cache_011([log], conclusion=conclusion)), ((), ()))

    def test_successful_run_that_saved_nothing_is_flagged(self) -> None:
        """RED: zackees/clud main run 36463271709, job 109067163056 --
        conclusion 'success', but both Dylint layers skip their save and
        the summary reports 'saved id=-1'."""

        log = fixture("CACHE-011", "red") / "job.log"
        _events, findings = check_cache_011([log], conclusion="success")
        rules = [f.rule for f in findings]
        self.assertIn("CACHE-011", rules)
        # dylint-cache, dylint-output-cache, and the bare summary line.
        self.assertGreaterEqual(len(findings), 2)

    def test_exact_hit_skip_is_not_flagged(self) -> None:
        log = fixture("CACHE-011", "green") / "job.log"
        _events, findings = check_cache_011([log], conclusion="success")
        self.assertEqual([], list(findings))

    def test_non_success_conclusion_never_fires(self) -> None:
        """A failed build legitimately skips saving -- CACHE-011 only cares
        about a run GitHub itself reports as 'success'."""

        log = fixture("CACHE-011", "red") / "job.log"
        _events, findings = check_cache_011([log], conclusion="failure")
        self.assertEqual([], list(findings))


class Cache012Test(unittest.TestCase):
    def test_prefixed_unusable_restore_evidence_is_detected(self) -> None:
        log = fixture("CACHE-012", "github") / "red.log"
        events, findings = check_cache_012([log, log])
        self.assertEqual(len(events), 2)
        self.assertEqual([f.rule for f in findings], ["CACHE-012"])

    def test_two_consecutive_unusable_restores_is_flagged(self) -> None:
        """RED: the same layer restores an unusable 22B archive on two
        consecutive runs with no intervening save (issue #7's PR-restore
        evidence, run 36467947289)."""

        logs = [fixture("CACHE-012", "red") / "run1.log", fixture("CACHE-012", "red") / "run2.log"]
        _events, findings = check_cache_012(logs)
        self.assertIn("CACHE-012", [f.rule for f in findings])

    def test_streak_broken_by_a_real_save_is_not_flagged(self) -> None:
        logs = [fixture("CACHE-012", "green") / "run1.log", fixture("CACHE-012", "green") / "run2.log"]
        _events, findings = check_cache_012(logs)
        self.assertEqual([], list(findings))

    def test_single_unusable_restore_below_threshold_is_not_flagged(self) -> None:
        logs = [fixture("CACHE-012", "red") / "run1.log"]
        _events, findings = check_cache_012(logs)
        self.assertEqual([], list(findings))


if __name__ == "__main__":
    unittest.main()


class Cache026Test(unittest.TestCase):
    """CACHE-026: forge-ci/zccache's pre-prune guard disabled cache writes on
    every main push from 2026-10-22 (runs 39761957389..40260098569)."""

    FROZEN = (
        "Wait\tWait for the exact-SHA cache pre-prune run\t2026-10-24T03:11:02.1Z "
        "##[warning]cache writes disabled for this job: cache pre-prune run 40001 concluded failure\n"
    )
    HEALTHY = "Build\tsave\t2026-10-24T03:11:02.1Z final cache summary: build=miss/saved id=9327196934\n"

    def _logs(self, *bodies: str) -> list:
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d)
        out = []
        for i, body in enumerate(bodies):
            p = d / f"run{i}.log"
            p.write_text(body, encoding="utf-8")
            out.append(p)
        return out

    def test_three_consecutive_frozen_runs_is_a_violation(self) -> None:
        from ci_lint.cache.save_evidence import check_cache_026

        result = check_cache_026(self._logs(self.FROZEN, self.FROZEN, self.FROZEN))
        self.assertEqual(len(result.events), 3)
        self.assertEqual([f.rule for f in result.findings], ["CACHE-026"])
        self.assertIn("pre-prune run 40001", result.findings[0].message)

    def test_a_healthy_run_breaks_the_streak(self) -> None:
        from ci_lint.cache.save_evidence import check_cache_026

        result = check_cache_026(self._logs(self.FROZEN, self.FROZEN, self.HEALTHY, self.FROZEN))
        self.assertEqual(len(result.events), 3)
        self.assertEqual(result.findings, ())

    def test_exported_flag_counts_and_threshold_is_configurable(self) -> None:
        from ci_lint.cache.save_evidence import check_cache_026

        flag = "env\tset\tZCCACHE_CACHE_WRITES=false\n"
        self.assertEqual(check_cache_026(self._logs(flag), threshold=2).findings, ())
        self.assertEqual(len(check_cache_026(self._logs(flag, flag), threshold=2).findings), 1)

    def test_save_check_reports_cache_026(self) -> None:
        from ci_lint.cache.save_evidence import run_save_check, to_json_dict

        report = run_save_check(self._logs(self.FROZEN, self.FROZEN, self.FROZEN), conclusion="success")
        self.assertIn("CACHE-026", [f.rule for f in report.findings])
        self.assertEqual(len(to_json_dict(report)["guard_disabled_events"]), 3)
