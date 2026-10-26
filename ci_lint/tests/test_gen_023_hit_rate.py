"""GEN-023 log-evidence clauses -- ci_lint/cache/hit_rate.py (zackees/ci.yml#411)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.cache.hit_rate import check_hit_rate
from ci_lint.finding import Status

# forge-ci/zccache run 40260098536, verbatim (gh run view --log shape, ANSI kept).
_ZCCACHE = (
    "native\tUNKNOWN STEP\t2026-10-26T02:43:58.27Z 02:22 soldr: cache \x1b[32m0 HIT\x1b[0m, "
    "\x1b[33m392 MISS\x1b[0m (0% hit rate, saved 0.0s)\n"
)


class HitRateTest(unittest.TestCase):
    def _run(self, text: str, **kw: float) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "job.log"
            p.write_text(text, encoding="utf-8")
            report = check_hit_rate([p], **kw)  # type: ignore[arg-type]
        for f in report.findings:
            self.assertEqual("GEN-023", f.rule)
            self.assertEqual(Status.NEEDS_REVIEW, f.status)
        return [f.message for f in report.findings]

    def test_cold_zccache_run_is_flagged(self) -> None:
        msgs = self._run(_ZCCACHE)
        self.assertEqual(1, len(msgs))
        self.assertIn("0%", msgs[0])

    def test_clud_67_percent_is_flagged(self) -> None:
        self.assertEqual(1, len(self._run("soldr: cache 381 HIT, 188 MISS (67% hit rate)\n")))

    def test_warm_run_is_clean(self) -> None:
        self.assertEqual([], self._run("soldr: cache 900 HIT, 20 MISS (98% hit rate)\n"))

    def test_small_session_not_judged(self) -> None:
        self.assertEqual([], self._run("soldr: cache 1 HIT, 31 MISS (3% hit rate)\n"))

    def test_compiling_without_stats_is_flagged(self) -> None:
        msgs = self._run("build\tcargo\t2026-10-26T00:00:00Z    Compiling serde v1.0.200\n")
        self.assertEqual(1, len(msgs))
        self.assertIn("no 'soldr: cache", msgs[0])

    def test_unrelated_job_ignored(self) -> None:
        self.assertEqual([], self._run("lint\truff\tAll checks passed!\n"))


if __name__ == "__main__":
    unittest.main()
