"""`ci-lint cache save-ok` -- issue #6 §6's "when we do NOT save" table,
rule ID CACHE-008, rows 1-10 in order (round-4A brief, deliverable 3).
"""

from __future__ import annotations

import unittest

from ci_lint.cache.save_ok import SaveOkInputError, SaveOkRequest, evaluate_save_ok
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

REPO = FIXTURES / "runtime" / "cache" / "repo"


class SaveOkTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def _base_req(self, **overrides: object) -> SaveOkRequest:
        defaults: dict[str, object] = dict(family="compile", flow="main", event="push")
        defaults.update(overrides)
        return SaveOkRequest(**defaults)  # type: ignore[arg-type]

    # -- C/C++ object-cache families (zackees/ci.yml#393) -----------------
    def test_ccache_family_follows_the_base_layer_writer_rule(self) -> None:
        _loaded = load_ci_toml(FIXTURES / "CACHE-004" / "green-cpp")
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        for fam in ("ccache", "zccache"):
            pr = evaluate_save_ok(ci, SaveOkRequest(family=fam, flow="pr", event="pull_request"))
            self.assertEqual((False, 2), (pr.save, pr.rule), fam)
            main = evaluate_save_ok(ci, SaveOkRequest(family=fam, flow="main", event="push",
                                                      paths=("~/.ccache", "build/.zccache")))
            self.assertTrue(main.save, main.reason)

    # -- rule 11 ---------------------------------------------------------
    def test_rule_11_cmake_build_tree(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(paths=("~/.ccache", "build/")))
        self.assertEqual((False, 11), (r.save, r.rule))
        self.assertIn("build/", r.reason)
        r = evaluate_save_ok(self.ci, self._base_req(paths=("out/build/linux-ci/CMakeCache.txt",)))
        self.assertEqual(11, r.rule)

    # -- rule 1 --------------------------------------------------------
    def test_rule_1_fork(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(fork=True))
        self.assertFalse(r.save)
        self.assertEqual(1, r.rule)

    def test_rule_1_act(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(act=True))
        self.assertFalse(r.save)
        self.assertEqual(1, r.rule)

    # -- rule 2 ----------------------------------------------------------
    def test_rule_2_base_layer_on_a_pr(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(flow="pr", event="pull_request"))
        self.assertFalse(r.save)
        self.assertEqual(2, r.rule)

    def test_rule_2_does_not_fire_for_a_pr_scoped_delta_attempt(self) -> None:
        # --pr given: this is explicitly PR-scoped (a delta, or an escalated
        # pr-base save) -- rule 2 ("a base-layer save on a non-writer flow")
        # must not fire; it gets past rule 2 into 3+.
        r = evaluate_save_ok(
            self.ci, self._base_req(flow="pr", event="pull_request", pr=42, payload_bytes=5_000_000)
        )
        self.assertNotEqual(2, r.rule)

    # -- rule 3 ------------------------------------------------------------
    def test_rule_3_retired_family(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(family="solo-toolchain", flow="main", event="push"))
        self.assertFalse(r.save)
        self.assertEqual(3, r.rule)

    def test_bad_input_family_neither_declared_nor_retired(self) -> None:
        with self.assertRaises(SaveOkInputError):
            evaluate_save_ok(self.ci, self._base_req(family="does-not-exist"))

    # -- rule 4 --------------------------------------------------------------
    def test_rule_4_build_failed(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(build_failed=True))
        self.assertFalse(r.save)
        self.assertEqual(4, r.rule)

    def test_test_failure_alone_never_blocks_a_save_only_build_failure_does(self) -> None:
        # save-ok has no notion of "tests failed" at all -- --build-ok (the
        # default) is sufficient regardless of test outcome, per issue #6
        # §6 row 4: "A test failure doesn't block the save."
        r = evaluate_save_ok(self.ci, self._base_req(build_failed=False, payload_bytes=5_000_000))
        self.assertTrue(r.save)

    # -- rule 5 ------------------------------------------------------------
    def test_rule_5_exact_hit(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(exact_hit=True))
        self.assertFalse(r.save)
        self.assertEqual(5, r.rule)

    def test_rule_5_too_few_new_units(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(new_units=0))
        self.assertFalse(r.save)
        self.assertEqual(5, r.rule)

    # -- rule 6 --------------------------------------------------------------
    def test_rule_6_payload_below_family_min(self) -> None:
        # 'compile' declares min = "1MB" in the fixture ci.toml.
        r = evaluate_save_ok(self.ci, self._base_req(payload_bytes=500))
        self.assertFalse(r.save)
        self.assertEqual(6, r.rule)

    def test_rule_6_payload_above_family_max(self) -> None:
        # 'compile' declares max = "200MB".
        r = evaluate_save_ok(self.ci, self._base_req(payload_bytes=250 * 1024 * 1024))
        self.assertFalse(r.save)
        self.assertEqual(6, r.rule)

    # -- rule 7 --------------------------------------------------------------
    def test_rule_7_delta_with_stale_base(self) -> None:
        r = evaluate_save_ok(
            self.ci,
            self._base_req(
                flow="pr", event="pull_request", pr=42, payload_bytes=5_000_000,
                base_key="restored-base-A", current_base_key="current-base-B",
            ),
        )
        self.assertFalse(r.save)
        self.assertEqual(7, r.rule)

    def test_rule_7_does_not_fire_when_bases_match(self) -> None:
        r = evaluate_save_ok(
            self.ci,
            self._base_req(
                flow="pr", event="pull_request", pr=42, payload_bytes=5_000_000,
                base_key="same-base", current_base_key="same-base",
            ),
        )
        self.assertTrue(r.save)

    # -- rule 8 --------------------------------------------------------------
    def test_rule_8_delta_exceeds_max_per_pr(self) -> None:
        # [cache.pr].max-per-pr = "128MB", 'compile'.max = "200MB": a
        # payload between the two exercises rule 8 without also tripping
        # rule 6 (below/above the FAMILY's own max).
        r = evaluate_save_ok(
            self.ci,
            self._base_req(flow="pr", event="pull_request", pr=42, payload_bytes=150 * 1024 * 1024),
        )
        self.assertFalse(r.save)
        self.assertEqual(8, r.rule)

    # -- rule 9 --------------------------------------------------------------
    def test_rule_9_lockfile_changed_without_ci_cache_save(self) -> None:
        r = evaluate_save_ok(
            self.ci,
            self._base_req(
                flow="pr", event="pull_request", pr=42, payload_bytes=5_000_000, lockfile_changed=True,
            ),
        )
        self.assertFalse(r.save)
        self.assertEqual(9, r.rule)

    def test_rule_9_lockfile_changed_with_ci_cache_save_tag_is_a_pr_scoped_base_save(self) -> None:
        # issue #6 §6: "This tag allows a PR-scoped save of deps/compile
        # anyway, still inside the PR budget and still trimmed on close."
        r = evaluate_save_ok(
            self.ci,
            self._base_req(
                flow="pr", event="pull_request", pr=42, payload_bytes=5_000_000, lockfile_changed=True,
                tags=("ci-cache-save",),
            ),
        )
        self.assertTrue(r.save)

    def test_rule_9_tags_parsed_from_a_pr_title_bracket_form_too(self) -> None:
        # The CLI's --tags accepts a raw PR-title-shaped string; the pure
        # evaluate_save_ok function itself takes already-parsed tokens
        # (bracket-stripped) -- exercised at the CLI layer, not here.
        r = evaluate_save_ok(
            self.ci,
            self._base_req(
                flow="pr", event="pull_request", pr=42, payload_bytes=5_000_000, lockfile_changed=True,
                tags=("ci-cache-save", "ci-full"),
            ),
        )
        self.assertTrue(r.save)

    # -- rule 10 -------------------------------------------------------------
    def test_rule_10_rerun_already_saved(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(rerun_saved=True))
        self.assertFalse(r.save)
        self.assertEqual(10, r.rule)

    # -- yes -------------------------------------------------------------
    def test_plain_writer_flow_save_is_yes(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(payload_bytes=5_000_000))
        self.assertTrue(r.save)
        self.assertIsNone(r.rule)
        self.assertEqual("save: yes", r.render())

    def test_render_no_mentions_the_rule_number(self) -> None:
        r = evaluate_save_ok(self.ci, self._base_req(fork=True))
        self.assertEqual("save: no (rule 1: the run is from a fork, or under act (local results never upload))", r.render())


if __name__ == "__main__":
    unittest.main()
