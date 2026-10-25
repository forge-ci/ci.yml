"""`ci-lint cache rework` -- CACHE-035 from real job-log evidence (#148 p1).

The TSV shapes below are copied verbatim from captured fleet logs (bosn run
40172648545 job `Rust workspace`, talos-hq/clud run 40188114016, 2026-10-25):
`gh run view --log` emits TWO tab fields and glues the ISO timestamp to the
content with a space; zccache's per-unit lines carry ANSI color; cargo prints
`Compiling <crate> v<version>`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.cache.rework import (
    LayerHit,
    ReworkError,
    check_rework,
    parse_log,
    render_rework_text,
    run_rework,
)

LAYER_HIT_LINE = (
    "Rust workspace (locked tests)\tUNKNOWN STEP\t2026-10-25T07:25:11.2662837Z "
    "build-cache           HIT       …x64-94b2bedf39536a13-61ad7a1acb6d536c         330.3MB"
)
EXACT_HIT_LINE = (
    "Rust workspace (locked tests)\tUNKNOWN STEP\t2026-10-25T07:25:11.2681411Z "
    "2/3 exact-hit  2/3 any-hit  total restore: 12.6s"
)
COMPILED_LINE = "Job\tBuild\t2026-10-25T07:26:00.0000000Z     Compiling serde v1.0.200"
ANSI_UNIT_LINE = (
    "Job\tBuild\t2026-10-25T07:25:36.8375019Z     3.50 "
    "soldr[cache] build_script_build \x1b[32m[HIT!]\x1b[0m"
)


class ParseLogTest(unittest.TestCase):
    def test_two_tab_tsv_with_glued_timestamp(self) -> None:
        parsed = parse_log(LAYER_HIT_LINE + "\n")
        self.assertEqual([], list(parsed.compiles))
        self.assertTrue(parsed.warm)
        self.assertEqual((LayerHit("build-cache", 1),), parsed.layers)

    def test_exact_hit_summary_counts_as_warm_without_layer_rows(self) -> None:
        parsed = parse_log(EXACT_HIT_LINE + "\n")
        self.assertTrue(parsed.warm)
        self.assertEqual((), parsed.layers)

    def test_compiling_line_captures_crate_version_and_step(self) -> None:
        parsed = parse_log(COMPILED_LINE)
        self.assertFalse(parsed.warm)
        self.assertEqual(1, len(parsed.compiles))
        event = parsed.compiles[0]
        self.assertEqual(("Build", "serde", "1.0.200"), (event.step, event.crate, event.version))

    def test_four_field_variant_and_ansi_are_handled(self) -> None:
        four_field = "Job\tBuild\t2026-10-25T07:26:00Z\tCompiling unicode_ident v1.1.12\n"
        parsed = parse_log(four_field)
        self.assertEqual(["unicode_ident"], [c.crate for c in parsed.compiles])
        self.assertEqual([], list(parse_log(ANSI_UNIT_LINE).compiles))  # HIT replay line, no compile

    def test_bare_stdout_is_accepted(self) -> None:
        parsed = parse_log("Compiling anyhow v1.0.86\n")
        self.assertEqual(["anyhow"], [c.crate for c in parsed.compiles])
        self.assertFalse(parsed.warm)


class ReworkCheckTest(unittest.TestCase):
    def test_signal_a_warm_restore_still_rebuilt(self) -> None:
        findings = check_rework([LAYER_HIT_LINE + "\n" + COMPILED_LINE + "\n"])
        self.assertEqual(1, len(findings))
        f = findings[0]
        self.assertEqual("CACHE-035", f.rule)
        self.assertEqual("needs_review", f.status.value)
        self.assertIn("warm restore still rebuilt", f.message)
        self.assertIn("build-cache HIT", f.message)
        self.assertIn("serde v1.0.200", f.message)

    def test_signal_a_absent_when_genuinely_warm_or_genuinely_cold(self) -> None:
        # bosn run 40172648545's real shape: warm table, zero compiles -> silent.
        self.assertEqual([], check_rework([LAYER_HIT_LINE + "\n" + ANSI_UNIT_LINE + "\n"]))
        # Cold rebuilds are legitimate work, not rework.
        self.assertEqual([], check_rework([COMPILED_LINE + "\n"]))

    def test_signal_b_same_crate_across_steps(self) -> None:
        log = COMPILED_LINE + "\n" + COMPILED_LINE.replace("\tBuild\t", "\tTest\t")
        findings = check_rework([log])
        self.assertEqual(1, len(findings))
        self.assertEqual("CACHE-035", findings[0].rule)
        self.assertIn("2 distinct steps", findings[0].message)
        self.assertIn("serde v1.0.200", findings[0].message)

    def test_same_crate_twice_in_one_step_is_not_signal_b(self) -> None:
        # Cargo prints one line per crate per invocation; a repeat in the SAME
        # step is not the cross-step rework signal.
        self.assertEqual([], check_rework([COMPILED_LINE + "\n" + COMPILED_LINE + "\n"]))

    def test_empty_input_is_silent(self) -> None:
        self.assertEqual([], check_rework([]))


class ReworkRunnerTest(unittest.TestCase):
    def test_run_read_render_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.log"
            path.write_text(LAYER_HIT_LINE + "\n" + COMPILED_LINE + "\n", encoding="utf-8")
            report = run_rework([str(path)])
            self.assertEqual(1, len(report.findings))
            self.assertTrue(report.warm)
            self.assertEqual(1, report.compile_events)
            self.assertEqual((LayerHit("build-cache", 1),), report.layers_hit)
            text = render_rework_text(report)
            self.assertIn("CACHE-035", text)
            self.assertIn("fix:", text)
            payload = report.to_json_dict()
            self.assertEqual("CACHE-035", payload["findings"][0]["rule"])
            self.assertTrue(payload["warm_restore"])
            with self.assertRaises(ReworkError):
                run_rework([str(Path(tmp) / "missing.log")])

    def test_clean_report_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.log"
            path.write_text(LAYER_HIT_LINE + "\n", encoding="utf-8")
            report = run_rework([str(path)])
            self.assertEqual(0, len(report.findings))
            self.assertIn("no rework signals", render_rework_text(report))