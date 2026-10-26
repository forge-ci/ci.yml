"""CACHE-001, CACHE-002, CACHE-003, CACHE-004, CACHE-010, CACHE-014, CACHE-029 -- ci_lint/rules/cache_static.py."""

from __future__ import annotations

import pathlib

import unittest
import unittest.mock
from dataclasses import replace

from ci_lint.rules import cache_static
from ci_lint.workflow_scan import ParsedYamlFile
from ci_lint.yaml_io import LoadStatus
from ci_lint.rules.cache_static import (
    check_cache_001,
    check_cache_002,
    check_cache_003_setup_uv,
    check_cache_004,
    check_cache_010,
    check_cache_014,
    check_cache_029,
    check_cache_030,
    check_cache_031,
    check_cache_032,
    check_cache_034,
    check_cache_024,
    check_cache_038,
    parse_size,
)
from ci_lint.schema import CachePromote, load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class CacheStaticFixtureTest(unittest.TestCase):
    @requires_yaml_tooling
    def test_cache_001_raw_actions_cache(self) -> None:
        repo = fixture("CACHE-001", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("CACHE-001", [f.rule for f in check_cache_001(ci, repo)])

        repo = fixture("CACHE-001", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CACHE-001", [f.rule for f in check_cache_001(ci, repo)])

    @requires_yaml_tooling
    def test_cache_001_wrapper_directory_is_sanctioned(self) -> None:
        """Round-4B: actions/cache is allowed ONLY inside
        [allow].cache-actions.only-in (default .github/actions/cache) --
        the SAME raw action used from a DIFFERENT composite-action
        directory is still CACHE-001."""

        repo = fixture("CACHE-001", "green-wrapper")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CACHE-001", [f.rule for f in check_cache_001(ci, repo)])

        repo = fixture("CACHE-001", "red-outside-wrapper")
        ci = load_ci_toml(repo).ci
        self.assertIn("CACHE-001", [f.rule for f in check_cache_001(ci, repo)])

    @requires_yaml_tooling
    def test_cache_002_volatile_key_component(self) -> None:
        repo = fixture("CACHE-002", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("CACHE-002", [f.rule for f in check_cache_002(ci, repo)])

        repo = fixture("CACHE-002", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CACHE-002", [f.rule for f in check_cache_002(ci, repo)])

    @requires_yaml_tooling
    def test_cache_002_wrapper_key_must_be_dynamic(self) -> None:
        """Round-4B: inside the sanctioned actions/cache wrapper, a literal
        'key:' (even with no volatile token) is CACHE-002 -- keys must come
        from 'ci_lint cache key' via a step output or an input passthrough."""

        repo = fixture("CACHE-002", "red-wrapper-literal-key")
        ci = load_ci_toml(repo).ci
        self.assertIn("CACHE-002", [f.rule for f in check_cache_002(ci, repo)])

        repo = fixture("CACHE-002", "green-wrapper-dynamic-key")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CACHE-002", [f.rule for f in check_cache_002(ci, repo)])

    @requires_yaml_tooling
    def test_cache_003_setup_uv_save_cache_must_be_plan_driven(self) -> None:
        """Round-4B: astral-sh/setup-uv's 'save-cache' input, when
        [allow].setup-uv.require says it must be 'plan'-derived, is
        CACHE-003 if missing or a literal 'true'."""

        repo = fixture("CACHE-003", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("CACHE-003", [f.rule for f in check_cache_003_setup_uv(ci, repo)])

        repo = fixture("CACHE-003", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CACHE-003", [f.rule for f in check_cache_003_setup_uv(ci, repo)])

    @requires_yaml_tooling
    def test_cache_010_kill_switch_env_while_family_declared_active(self) -> None:
        """Issue #7: ZCCACHE_DISABLE=1 in a job env, while ci.toml declares
        a [cache.family] via setup-soldr, is CACHE-010 (zackees/clud's
        `_dylint.yml` -- clud#487)."""

        repo = fixture("CACHE-010", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("CACHE-010", [f.rule for f in check_cache_010(ci, repo)])

        repo = fixture("CACHE-010", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CACHE-010", [f.rule for f in check_cache_010(ci, repo)])

    @requires_yaml_tooling
    def test_cache_010_ccache_disable_only_contradicts_a_ccache_family(self) -> None:
        """zackees/ci.yml#393: CCACHE_DISABLE=1 beside a declared `via =
        "ccache"` family is CACHE-010; beside setup-soldr families only, it
        contradicts nothing ci.toml declares."""

        repo = fixture("CACHE-010", "red-ccache")
        _loaded = load_ci_toml(repo)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.assertEqual(["CACHE-010"], [f.rule for f in check_cache_010(ci, repo)])

        repo = fixture("CACHE-010", "green-ccache")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], check_cache_010(ci, repo))

    def test_cache_004_budgets_ccache_and_zccache_families(self) -> None:
        # zackees/ci.yml#393: C/C++ object-cache families count toward the
        # declared budget like any other family.
        _loaded = load_ci_toml(fixture("CACHE-004", "green-cpp"))
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.assertEqual([], [f for f in findings if f.rule == "CT-002"])
        _r = check_cache_004(ci)
        found, arithmetic = _r.findings, _r.arithmetic
        self.assertNotIn("CACHE-004", [f.rule for f in found], msg=arithmetic)
        self.assertIn("ccache", arithmetic)

        ci = load_ci_toml(fixture("CACHE-004", "red-cpp")).ci
        assert ci is not None
        _r = check_cache_004(ci)
        found, arithmetic = _r.findings, _r.arithmetic
        self.assertIn("CACHE-004", [f.rule for f in found], msg=arithmetic)

    def test_cache_004_budget_exceeded_without_pre_prune(self) -> None:
        ci = load_ci_toml(fixture("CACHE-004", "red")).ci
        _r = check_cache_004(ci)
        findings, arithmetic = _r.findings, _r.arithmetic
        self.assertIn("CACHE-004", [f.rule for f in findings])
        self.assertIn("worst case", arithmetic)

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        _r = check_cache_004(ci)
        findings, arithmetic = _r.findings, _r.arithmetic
        self.assertNotIn("CACHE-004", [f.rule for f in findings])

    def test_cache_004_budget_over_10gb_is_always_a_violation(self) -> None:
        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        from dataclasses import replace

        big = replace(ci.cache, budget="11GB")
        ci_big = replace(ci, cache=big)
        _r = check_cache_004(ci_big)
        findings, _ = _r.findings, _r.arithmetic
        self.assertIn("CACHE-004", [f.rule for f in findings])

    def test_cache_004_full_pre_prune_removes_only_the_lockfile_peak_term(self) -> None:
        """Regression for round-2A defect 4: every writer flow setting
        `pre-prune = true` must remove only the lockfile-change-peak term
        from the worst-case sum, not waive the whole CACHE-004 proof. The
        CACHE-004/green fixture already has `pre-prune = true` on both
        writer flows (`flow.main`, `flow.nightly`); its steady total is
        50MB and `[cache.pr].budget` is 1GB, so `worst = steady +
        pr.budget` = ~1.074GB. Before the fix, the old code treated full
        pre-prune as "skip the check entirely" and never compared that sum
        against the budget at all."""

        from dataclasses import replace

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        self.assertTrue(ci.flows["main"].pre_prune)
        self.assertTrue(ci.flows["nightly"].pre_prune)

        # worst_pruned (steady 50MB + pr.budget 1GB) = ~1.074GB > 1GB budget:
        # the fixed formula must still fail, even though every writer flow
        # pre-prunes.
        tight = replace(ci, cache=replace(ci.cache, budget="1GB"))
        _r = check_cache_004(tight)
        findings, arithmetic = _r.findings, _r.arithmetic
        self.assertIn("CACHE-004", [f.rule for f in findings], msg=arithmetic)
        self.assertIn("lockfile peak waived", arithmetic)

        # Raise the budget above worst_pruned (but still below worst_unpruned,
        # i.e. below steady + lockfile-peak + pr.budget = ~1.104GB) to prove
        # the lockfile-peak term really was dropped, not just shrunk.
        just_enough = replace(ci, cache=replace(ci.cache, budget="1100MB"))
        _r = check_cache_004(just_enough)
        findings2, arithmetic2 = _r.findings, _r.arithmetic
        self.assertNotIn("CACHE-004", [f.rule for f in findings2], msg=arithmetic2)

    def test_cache_004_partial_pre_prune_still_counts_the_lockfile_peak(self) -> None:
        from dataclasses import replace

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        one_flow_only = replace(ci, flows={**ci.flows, "nightly": replace(ci.flows["nightly"], pre_prune=False)})
        _r = check_cache_004(replace(one_flow_only, cache=replace(one_flow_only.cache, budget="1100MB")))
        findings, arithmetic = _r.findings, _r.arithmetic
        self.assertIn("CACHE-004", [f.rule for f in findings], msg=arithmetic)
        self.assertIn("not every writer flow pre-prunes", arithmetic)

    def test_cache_029_large_family_without_eviction_is_a_finding(self) -> None:
        """A large family with nothing bounding its entry count fills the
        repository's whole cache budget. Measured 2026-10-04 across the
        three largest Rust repositories: 83-87% of every one was superseded
        entries, ~25GB total, and clud had already crossed GitHub's 10GB cap
        so new saves were being refused."""

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        fam = dict(ci.cache.family)
        fam["huge"] = replace(fam["uv"], max="700MB", evict=None, per="none")
        findings = check_cache_029(replace(ci, cache=replace(ci.cache, family=fam)))
        hit = [f for f in findings if "'huge'" in f.message]
        self.assertEqual(len(hit), 1, [f.message for f in findings])

    def test_cache_029_evict_lru_satisfies_it(self) -> None:
        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        fam = dict(ci.cache.family)
        fam["huge"] = replace(fam["uv"], max="700MB", evict="lru", per=None)
        findings = check_cache_029(replace(ci, cache=replace(ci.cache, family=fam)))
        self.assertFalse(any("'huge'" in f.message for f in findings), [f.message for f in findings])

    def test_cache_029_per_alone_does_not_satisfy_it(self) -> None:
        """`per` declares the WRITER shape, not the entry count. kernal-api
        declares per = "none" on every family while `dylint` still holds 13
        live entries, so accepting `per` as an exemption would have missed
        the exact repositories this rule exists for."""

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        fam = dict(ci.cache.family)
        fam["huge"] = replace(fam["uv"], max="700MB", evict=None, per="platform")
        findings = check_cache_029(replace(ci, cache=replace(ci.cache, family=fam)))
        self.assertTrue(any("'huge'" in f.message for f in findings), [f.message for f in findings])

    def test_cache_029_small_family_is_not_flagged(self) -> None:
        """Below 256MB an unbounded entry count is noise, not a budget
        threat -- the uv family holds 22 entries totalling 2.3MB."""

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        fam = dict(ci.cache.family)
        fam["small"] = replace(fam["uv"], max="5MB", evict=None, per=None)
        findings = check_cache_029(replace(ci, cache=replace(ci.cache, family=fam)))
        self.assertFalse(any("'small'" in f.message for f in findings), [f.message for f in findings])

    def test_cache_030_promotion_is_needs_review_until_it_is_implemented(self) -> None:
        """`[cache.promote] mode = "ancestor"` is a policy statement today.
        setup-soldr declares an `auto-key` input in its action.yml, but the
        input appears nowhere in its source or its built bundle, and
        zackees/setup-soldr#552 is open. Recording the intent must be
        allowed; believing it happened must not."""

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        self.assertEqual(check_cache_030(ci), [])
        promoted = replace(ci, cache=replace(ci.cache, promote=CachePromote(mode="ancestor")))
        findings = check_cache_030(promoted)
        self.assertEqual([f.rule for f in findings], ["CACHE-030"])
        self.assertEqual(findings[0].status.value, "needs_review")
        self.assertIn("#552", findings[0].fix)

    @requires_yaml_tooling
    def test_cache_031_unpromotable_family_under_a_promotion_claim(self) -> None:
        """Promotion is a promise about a KEY SHAPE. A family that does not
        carry the lineage label cannot honor it -- a content hash discards
        the ancestry the nearest-ancestor search needs (fleet measurement
        2026-10-04: soldr 70 labeled entries = 0.00 GiB against 5.97 GiB of
        bare-hash build caches; bosn 85 / 0.00 against 4.41)."""

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        # Without a promotion claim there is nothing to be inconsistent with.
        self.assertEqual(check_cache_031(ci), [])

        promoted = replace(ci, cache=replace(ci.cache, promote=CachePromote(mode="ancestor")))
        self.assertEqual(check_cache_031(promoted), [])  # fixture families are 10MB, under the floor

        # A small family is noise; only one big enough to threaten the budget
        # can make a promotion claim that its keys cannot keep.
        big = replace(
            promoted,
            cache=replace(
                promoted.cache,
                family={**promoted.cache.family, "compile": replace(promoted.cache.family["compile"], max="500MB")},
            ),
        )
        findings = check_cache_031(big)
        self.assertEqual([f.rule for f in findings], ["CACHE-031"])
        self.assertIn("compile", findings[0].message)
        for f in findings:
            self.assertEqual(f.status.value, "needs_review")
            self.assertIn("#552", f.fix)

    @requires_yaml_tooling
    def test_cache_031_a_family_that_declares_the_label_is_quiet(self) -> None:
        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        promoted = replace(ci, cache=replace(ci.cache, promote=CachePromote(mode="ancestor")))
        fams = {
            fid: replace(fam, promote="ancestor") if parse_size(fam.max) and parse_size(fam.max) >= 256 * 1024**2 else fam
            for fid, fam in promoted.cache.family.items()
        }
        quiet = replace(promoted, cache=replace(promoted.cache, family=fams))
        self.assertEqual(check_cache_031(quiet), [])

    @requires_yaml_tooling
    def test_cache_032_opt_in_the_pinned_action_cannot_reach(self) -> None:
        """The pilot is MERGED but UNRELEASED. `git tag --contains afdd8bf`
        is empty and no v0.9.x action.yml declares `auto-key`, so every
        SHA-pinned setup-soldr predates it and the opt-in is inert. Silent:
        the input parses, the pilot never runs, restores fall back."""

        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        self.assertEqual(check_cache_032(ci), [])  # no promotion claim, nothing unreachable

        claimed = replace(ci, cache=replace(ci.cache, promote=CachePromote(mode="ancestor")))
        findings = check_cache_032(claimed)
        self.assertEqual([f.rule for f in findings], ["CACHE-032"])
        self.assertEqual(findings[0].status.value, "needs_review")
        self.assertIn("auto-key", findings[0].message)
        self.assertIn("#552", findings[0].fix)
        # The sanctioned v0 float is NOT an escape hatch: it resolves to
        # dfbe962 (#532), 33 commits behind main, with no auto-key. An
        # earlier revision told users the float "can run ahead of the
        # pilot"; measured against the tags on 2026-10-05, it trails.
        self.assertIn("trails main by 33 commits", findings[0].message)
        self.assertNotIn("sanctioned exception", findings[0].fix)

    def test_per_shape_max_is_the_family_footprint_not_max_times_cardinality(self) -> None:
        """A family whose entries differ in size cannot be declared with one
        ceiling. zackees/running-process#1321: `compile` holds two ~1.25 GiB
        shapes and six at <=529 MB, so `max x platforms` models 7.8 GB for a
        family that occupies 5.0 GB -- and the only `per` values that would
        fit UNDER-count, which is the dangerous direction, because
        `ci-lint cache janitor` LRU-evicts to `max x cardinality` and would
        delete live entries.

        `shapes` declares each shape's own ceiling, so the footprint is the
        SUM and the cardinality is the shape count."""

        import re as _re

        from ci_lint.rules.cache_static import cardinality, family_footprint

        base = (
            pathlib.Path(__file__).resolve().parents[2]
            / "examples" / "rust-pypi-app" / "ci.toml"
        ).read_text(encoding="utf-8").replace(
            "zackees/ci.yml@<40-hex-sha>", "zackees/ci.yml@" + "a" * 40
        )
        row = _re.search(r"^compile\s*=.*$", base, _re.M).group(0)
        shapes = '{ "macos-arm-shared" = { max = "1300MB" }, "linux" = { max = "1100MB" } }'
        _loaded = load_ci_toml_text(base.replace(row, row[:-2] + f", shapes = {shapes} }}"))
        ci = _loaded.ci
        findings = _loaded.findings
        self.assertEqual([f.rule for f in findings], [])
        fam = ci.cache.family["compile"]
        self.assertEqual([(s.scope, s.max) for s in fam.shapes],
                         [("macos-arm-shared", "1300MB"), ("linux", "1100MB")])
        self.assertEqual(cardinality(ci, fam.per, len(fam.shapes)), 2)
        self.assertEqual(family_footprint(ci, fam), 2400 * 1024**2)
        # Without shapes the same family models max x cardinality instead.
        plain = load_ci_toml_text(base).ci
        self.assertNotEqual(family_footprint(ci, plain.cache.family["compile"]), family_footprint(ci, fam))

    def test_a_shape_ceiling_that_is_not_a_size_is_rejected(self) -> None:
        """A shape dropped for an unusable ceiling would be missing from the
        footprint and silently UNDERSTATE CACHE-004 -- the direction that
        lets a repository overflow the cap."""

        import re as _re

        base = (
            pathlib.Path(__file__).resolve().parents[2]
            / "examples" / "rust-pypi-app" / "ci.toml"
        ).read_text(encoding="utf-8").replace(
            "zackees/ci.yml@<40-hex-sha>", "zackees/ci.yml@" + "a" * 40
        )
        row = _re.search(r"^compile\s*=.*$", base, _re.M).group(0)
        findings = load_ci_toml_text(
            base.replace(row, row[:-2] + ', shapes = { "broken" = { max = "notasize" } } }')
        ).findings
        self.assertIn("CT-002", [f.rule for f in findings])

    @requires_yaml_tooling
    def test_cache_014_no_optional_inputs_is_silent(self) -> None:
        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        findings = check_cache_014(ci)
        self.assertNotIn("CACHE-014", [f.rule for f in findings])

    @requires_yaml_tooling
    def test_cache_014_fleet_of_open_prs_exceeds_pr_budget(self) -> None:
        # max-per-pr=128MB, budget=1GB (from the fixture). 10 open PRs each
        # hitting the cap = 1.25GB > 1GB budget, even though no single PR
        # exceeded max-per-pr.
        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        red = replace(ci, cache=replace(ci.cache, pr=replace(ci.cache.pr, expected_open_prs=10)))
        findings = check_cache_014(red)
        self.assertIn("CACHE-014", [f.rule for f in findings])

        green = replace(ci, cache=replace(ci.cache, pr=replace(ci.cache.pr, expected_open_prs=2)))
        findings2 = check_cache_014(green)
        self.assertNotIn("CACHE-014", [f.rule for f in findings2])

    @requires_yaml_tooling
    def test_cache_014_measured_delta_exceeds_max_per_pr(self) -> None:
        ci = load_ci_toml(fixture("CACHE-004", "green")).ci
        red = replace(
            ci, cache=replace(ci.cache, pr=replace(ci.cache.pr, measured_largest_delta="200MB"))
        )
        findings = check_cache_014(red)
        self.assertIn("CACHE-014", [f.rule for f in findings])

        green = replace(
            ci, cache=replace(ci.cache, pr=replace(ci.cache.pr, measured_largest_delta="64MB"))
        )
        findings2 = check_cache_014(green)
        self.assertNotIn("CACHE-014", [f.rule for f in findings2])


class SizeParsingUnitTest(unittest.TestCase):
    def test_parse_size(self) -> None:
        self.assertEqual(9 * 1024**3, parse_size("9GB"))
        self.assertEqual(150 * 1024**2, parse_size("150MB"))
        self.assertIsNone(parse_size("not-a-size"))


if __name__ == "__main__":
    unittest.main()


def load_ci_toml_text(text: str):
    """Load a ci.toml from a string, for cases that need an inline edit."""
    import tempfile

    from ci_lint.schema import load_ci_toml

    d = pathlib.Path(tempfile.mkdtemp())
    (d / "ci.toml").write_text(text, encoding="utf-8")
    return load_ci_toml(d)


class Cache034PrepruneTest(unittest.TestCase):
    """CACHE-034: `pre-prune = true` waives a real term from CACHE-004's
    worst case, so it must correspond to an actual pre-prune call.

    Evidence: FastLED/fbuild#1652 (2026-10-05) declared `pre-prune = true` on
    `[flow.main]` with `ci-lint cache preprune` in zero workflow files.
    Removing the declaration moved the modelled worst case 7.92 -> 15.84 GB
    against a 10.20 GB budget.
    """

    def _repo_with(
        self,
        *,
        declared: bool,
        call: str | None,
        wf_name: str = "ci",
        wf_file: str = "ci.yml",
    ) -> pathlib.Path:
        import tempfile

        root = pathlib.Path(tempfile.mkdtemp(prefix="cache034-"))
        (root / "ci.toml").write_text(
            "[schema]\nnothing = true\n"
            if False
            else (
                'schema = 3\nprofile = "library"\n'
                '[platforms]\nlinux-x64 = { target = "x86_64-unknown-linux-gnu", runs-on = "ubuntu-24.04", group = "linux" }\n'
                "[suites]\nunit = { run = \"pytest\", required = true }\n"
                "[flow.main]\nextends = \"main\"\ncache = \"write\"\n"
                + ("pre-prune = true\n" if declared else "")
                + "[cache]\nbudget = \"9GB\"\nwrite-on = [\"main\"]\n"
                "[cache.family]\nsmall = { via = \"setup-uv\", max = \"5MB\", per = \"none\" }\n"
                "[allow]\nworkflows = { \"ci.yml\" = [\"push:main\"] }\n"
            ),
            encoding="utf-8",
        )
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / wf_file).write_text(
            f"name: {wf_name}\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
            + (f"      - run: {call}\n" if call else "      - run: echo hi\n"),
            encoding="utf-8",
        )
        return root

    @requires_yaml_tooling
    def test_declared_without_a_call_is_flagged(self) -> None:
        repo = self._repo_with(declared=True, call=None)
        ci = load_ci_toml(repo).ci
        findings = check_cache_034(ci, repo)
        self.assertEqual([f.rule for f in findings], ["CACHE-034"])
        self.assertEqual(findings[0].status.value, "needs_review")
        self.assertIn("main", findings[0].message)

    @requires_yaml_tooling
    def test_fix_text_does_not_read_as_just_delete_the_line(self) -> None:
        """The fix carries both remediations and the cost of each (#354).

        Adding the step needs a job with `actions: write` and same-run
        ordering ahead of the saves, which some repositories cannot express
        today; removing the declaration is then the honest fix -- but it
        returns the waived peak, so the finding must never present deletion
        as a costless completion of remediation.
        """

        repo = self._repo_with(declared=True, call=None)
        ci = load_ci_toml(repo).ci
        fix = check_cache_034(ci, repo)[0].fix
        self.assertIn("actions: write", fix)
        self.assertIn("WITHIN THE SAME RUN", fix)
        self.assertIn("remove `pre-prune = true`", fix)
        self.assertIn("footprint grows", fix)
        self.assertIn("zackees/ci.yml#354", fix)

    @requires_yaml_tooling
    def test_zccache_shaped_named_workflow_is_clean(self) -> None:
        """A workflow honouring the role by NAME is honoured (#352, #360).

        zccache implements the prune by hand in a workflow named
        `Cache Pre-prune` instead of invoking the ci-lint command; a
        literal-only scan reads that as unhonoured. The name (anchored
        canonical spelling) counts, so the finding does not fire.
        """

        repo = self._repo_with(declared=True, call=None, wf_name="Cache Pre-prune")
        ci = load_ci_toml(repo).ci
        self.assertEqual(check_cache_034(ci, repo), [])

    @requires_yaml_tooling
    def test_canonical_pre_prune_workflow_file_is_clean(self) -> None:
        """The canonical file stem counts too -- zccache's file is
        `.github/workflows/cache-pre-prune.yml`."""

        repo = self._repo_with(declared=True, call=None, wf_file="cache-pre-prune.yml")
        ci = load_ci_toml(repo).ci
        self.assertEqual(check_cache_034(ci, repo), [])

    @requires_yaml_tooling
    def test_lookalike_workflow_name_is_still_flagged(self) -> None:
        """The identity pattern is anchored: teaching the scan the name
        must not become a prefix match any workflow can ride in on."""

        repo = self._repo_with(declared=True, call=None, wf_name="Cache Pre-prune deployer")
        ci = load_ci_toml(repo).ci
        findings = check_cache_034(ci, repo)
        self.assertEqual([f.rule for f in findings], ["CACHE-034"])

    def test_unreadable_workflows_do_not_manufacture_a_finding(self) -> None:
        """A tree ci_lint cannot parse is not evidence of a contradiction.

        Without a YAML parser the workflows are skipped, so a naive scan would
        report every declaration as unhonoured. That would accuse a repository
        on the strength of a missing dependency, so the rule stays silent
        instead -- and this test pins that, so it cannot regress into a false
        positive when the selftest runs without PyYAML.
        """

        repo = self._repo_with(declared=True, call=None)
        ci = load_ci_toml(repo).ci
        unparsed = [
            ParsedYamlFile(
                path=".github/workflows/ci.yml",
                document=None,
                status=LoadStatus.NEEDS_REVIEW,
                reason="PyYAML not importable and yq not on PATH",
            )
        ]
        with (
            unittest.mock.patch.object(cache_static, "load_workflows", return_value=unparsed),
            unittest.mock.patch.object(cache_static, "load_composite_actions", return_value=[]),
        ):
            self.assertEqual(check_cache_034(ci, repo), [])

    def test_declared_with_a_call_is_clean(self) -> None:
        repo = self._repo_with(declared=True, call="ci-lint cache preprune --lockfile-changed")
        ci = load_ci_toml(repo).ci
        self.assertEqual(check_cache_034(ci, repo), [])

    def test_undeclared_is_clean_whatever_the_workflows_do(self) -> None:
        repo = self._repo_with(declared=False, call=None)
        ci = load_ci_toml(repo).ci
        self.assertEqual(check_cache_034(ci, repo), [])


class Cache038RacingSoldrWritersTest(unittest.TestCase):
    """CACHE-038 (#125): saving setup-soldr jobs sharing runner + suffix race."""

    SOLDR = "zackees/setup-soldr@v0"

    def _repo(self, workflow: str, action: str | None = None) -> pathlib.Path:
        import tempfile

        root = pathlib.Path(tempfile.mkdtemp(prefix="cache038-"))
        (root / "ci.toml").write_text(
            'schema = 3\nprofile = "library"\n'
            '[platforms]\nlinux-x64 = { target = "x86_64-unknown-linux-gnu", runs-on = "ubuntu-24.04", group = "linux" }\n'
            '[suites]\nunit = { run = "pytest", required = true }\n'
            '[cache]\nbudget = "9GB"\nwrite-on = ["main"]\n'
            '[cache.family]\nsmall = { via = "setup-uv", max = "5MB", per = "none" }\n'
            '[allow]\nworkflows = { "ci.yml" = ["push:main"] }\n',
            encoding="utf-8",
        )
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(workflow, encoding="utf-8")
        if action is not None:
            ad = root / ".github" / "actions" / "soldr"
            ad.mkdir(parents=True)
            (ad / "action.yml").write_text(action, encoding="utf-8")
        return root

    @staticmethod
    def _job(name: str, runs_on: str, with_: str) -> str:
        return (
            f"  {name}:\n    runs-on: {runs_on}\n    steps:\n"
            f"      - uses: zackees/setup-soldr@v0\n" + (f"        with:\n{with_}" if with_ else "")
        )

    def _check(self, workflow: str, action: str | None = None) -> list[tuple[str, str]]:
        repo = self._repo(workflow, action)
        ci = load_ci_toml(repo).ci
        return [(f.rule, f.status.value) for f in check_cache_038(ci, repo)]

    @requires_yaml_tooling
    def test_two_unsuffixed_writers_on_one_runner_race(self) -> None:
        wf = "on: [push]\njobs:\n" + self._job("check", "ubuntu-24.04", "") + self._job(
            "facade", "ubuntu-24.04", ""
        )
        self.assertEqual(self._check(wf), [("CACHE-038", "violation")])

    @requires_yaml_tooling
    def test_same_literal_suffix_races(self) -> None:
        w = "          cache-key-suffix: lint\n"
        wf = "on: [push]\njobs:\n" + self._job("a", "ubuntu-24.04", w) + self._job("b", "ubuntu-24.04", w)
        self.assertEqual(self._check(wf), [("CACHE-038", "violation")])

    @requires_yaml_tooling
    def test_distinct_suffix_runner_or_restore_only_is_clean(self) -> None:
        wf = (
            "on: [push]\njobs:\n"
            + self._job("a", "ubuntu-24.04", "          cache-key-suffix: a\n")
            + self._job("b", "ubuntu-24.04", "          cache-key-suffix: b\n")
            + self._job("c", "windows-2025", "")
            + self._job("d", "ubuntu-24.04", '          cache-key-suffix: a\n          save-cache: "false"\n')
        )
        self.assertEqual(self._check(wf), [])

    @requires_yaml_tooling
    def test_push_only_writer_and_push_skipping_validator_do_not_race(self) -> None:
        """GEN-022 step 5/6 (#432): a push-only writer job shares its key with a
        validation job that skips every push -- they never run in one event."""
        w = "          cache-key-suffix: linux\n"
        writer = self._job("writer", "ubuntu-24.04", w).replace(
            "    runs-on:", "    if: github.event_name == 'push'\n    runs-on:", 1
        )
        validator = self._job("linux", "ubuntu-24.04", w).replace(
            "    runs-on:",
            "    if: needs.verify.outputs.skip_linux != 'true' && github.event_name != 'push'\n    runs-on:",
            1,
        )
        self.assertEqual(self._check("on: [push]\njobs:\n" + writer + validator), [])
        # An `||` leaves the gate unproven, and two push-only writers still race.
        either = validator.replace("&& github.event_name != 'push'", "|| github.event_name != 'push'")
        self.assertEqual(self._check("on: [push]\njobs:\n" + writer + either), [("CACHE-038", "violation")])
        twin = writer.replace("  writer:", "  writer2:", 1)
        self.assertEqual(self._check("on: [push]\njobs:\n" + writer + twin), [("CACHE-038", "violation")])

    @requires_yaml_tooling
    def test_single_writer_is_clean_even_if_dynamic(self) -> None:
        wf = "on: [push]\njobs:\n" + self._job("a", "${{ matrix.os }}", "")
        self.assertEqual(self._check(wf), [])

    @requires_yaml_tooling
    def test_dynamic_suffix_or_runner_needs_review(self) -> None:
        wf = (
            "on: [push]\njobs:\n"
            + self._job("a", "ubuntu-24.04", "          cache-key-suffix: ${{ github.job }}\n")
            + self._job("b", "${{ matrix.os }}", "")
        )
        self.assertEqual(
            self._check(wf), [("CACHE-038", "needs_review"), ("CACHE-038", "needs_review")]
        )

    @requires_yaml_tooling
    def test_cook_subaction_is_not_a_build_cache_writer(self) -> None:
        wf = (
            "on: [push]\njobs:\n"
            + self._job("a", "ubuntu-24.04", "")
            + "  b:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: zackees/setup-soldr/cook@v0\n"
        )
        self.assertEqual(self._check(wf), [])

    @requires_yaml_tooling
    def test_wrapper_composite_resolves_caller_inputs(self) -> None:
        action = (
            "runs:\n  using: composite\n  steps:\n      - uses: zackees/setup-soldr@v0\n"
            "        with:\n          cache-key-suffix: ${{ inputs.suffix }}\n"
            "inputs:\n  suffix:\n    default: ''\n"
        )
        call = "    runs-on: ubuntu-24.04\n    steps:\n      - uses: ./.github/actions/soldr\n"
        racing = "on: [push]\njobs:\n  a:\n" + call + "  b:\n" + call
        self.assertEqual(self._check(racing, action), [("CACHE-038", "violation")])
        distinct = (
            "on: [push]\njobs:\n  a:\n" + call + "        with:\n          suffix: x\n  b:\n" + call
        )
        self.assertEqual(self._check(distinct, action), [])


    @requires_yaml_tooling
    def test_template_compound_suffix_wrapper_is_resolved(self) -> None:
        """template-python-rust-cmd: the wrapper's compound suffix expression resolves per job."""

        from ci_lint.tests.helpers import fixture

        for kind, want in (("green-template", []), ("red-template", [("CACHE-038", "violation")])):
            repo = fixture("CACHE-038", kind)
            ci = load_ci_toml(repo).ci
            got = [(f.rule, f.status.value) for f in check_cache_038(ci, repo)]
            self.assertEqual(got, want, kind)

    @requires_yaml_tooling
    def test_cross_target_distinguishes_shared_suffix(self) -> None:
        def job(name: str, target: str) -> str:
            return self._job(name, "ubuntu-24.04", f"          cache-key-suffix: x\n          cross-targets: {target}\n")

        distinct = "on: [push]\njobs:\n" + job("a", "aarch64-unknown-linux-gnu") + job("b", "x86_64-unknown-linux-musl")
        self.assertEqual(self._check(distinct), [])
        same = "on: [push]\njobs:\n" + job("a", "aarch64-unknown-linux-gnu") + job("b", "AARCH64-unknown-linux-gnu")
        self.assertEqual(self._check(same), [("CACHE-038", "violation")])
        legs = "on: [push]\njobs:\n" + job("a", "${{ matrix.target }}") + self._job(
            "b", "ubuntu-24.04", "          cache-key-suffix: x\n          cross-targets: aarch64-unknown-linux-gnu\n"
        )
        self.assertEqual(self._check(legs), [])

    def test_key_expression_value_sets(self) -> None:
        from ci_lint.rules.soldr_key_expr import Context, possible_values

        expr = (
            "${{ inputs.save-cache == 'true' && inputs.cache-key-pr != '' && (inputs.cache-key-suffix != '' && "
            "format('{0}-{1}', inputs.cache-key-suffix, inputs.cache-key-pr) || inputs.cache-key-pr) || inputs.cache-key-suffix }}"
        )
        caller = {
            "save-cache": "${{ needs.precheck.outputs.cache_save }}",
            "cache-key-pr": "${{ needs.precheck.outputs.cache_key_pr }}",
        }
        plain = possible_values(expr, Context(caller=caller, defaults=(), composite=True))
        self.assertEqual(plain, frozenset({"", "pr-1"}))
        named = possible_values(expr, Context(caller={**caller, "cache-key-suffix": "lint"}, defaults=(), composite=True))
        self.assertTrue(named and "lint" in named and not (named & frozenset({"", "pr-1"})))
        self.assertIsNone(possible_values("${{ github.job }}", Context(caller={}, defaults=(), composite=False)))
        self.assertIsNone(possible_values("${{ inputs.x }}", Context(caller={}, defaults=(), composite=False)))
        self.assertIsNone(possible_values("${{ toJSON(x) == 'a' }}", Context(caller={}, defaults=(), composite=False)))


class Cache024AutoKeyTrustedWritersTest(unittest.TestCase):
    """CACHE-024 (#185/#420): `key: auto` on GitHub needs auto-key-trusted-writers."""

    WRITER = "zackees/bosn/.github/workflows/ci.yml@" + "a" * 40 + ":123:1:456"

    def _c24(self, with_: str, runs_on: str = "ubuntu-24.04", action: str | None = None) -> list[tuple[str, str]]:
        repo = self._repo("on: [push]\njobs:\n" + self._job("build", runs_on, with_), action)
        ci = load_ci_toml(repo).ci
        return [(f.rule, f.status.value) for f in check_cache_024(ci, repo)]

    _repo = Cache038RacingSoldrWritersTest._repo
    _job = staticmethod(Cache038RacingSoldrWritersTest._job)

    @requires_yaml_tooling
    def test_literal_auto_without_writers_is_violation(self) -> None:
        self.assertEqual(self._c24("          key: auto\n"), [("CACHE-024", "violation")])
        self.assertEqual(
            self._c24("          key: auto\n          auto-key-trusted-writers: ''\n"), [("CACHE-024", "violation")]
        )
        self.assertEqual(self._c24("          auto-key: 'true'\n"), [("CACHE-024", "violation")])

    @requires_yaml_tooling
    def test_act_guard_default_and_self_hosted_are_clean(self) -> None:
        self.assertEqual(self._c24("          key: ${{ env.ACT && 'auto' || '' }}\n"), [])
        self.assertEqual(self._c24(""), [])
        self.assertEqual(self._c24("          key: mykey\n          auto-key: 'true'\n"), [])
        self.assertEqual(self._c24("          key: auto\n", runs_on="[self-hosted, linux]"), [])

    @requires_yaml_tooling
    def test_valid_writers_clean_malformed_violation(self) -> None:
        self.assertEqual(self._c24(f"          key: auto\n          auto-key-trusted-writers: {self.WRITER}\n"), [])
        self.assertEqual(
            self._c24("          key: auto\n          auto-key-trusted-writers: some-job\n"),
            [("CACHE-024", "violation")],
        )

    @requires_yaml_tooling
    def test_expressions_need_review(self) -> None:
        self.assertEqual(self._c24("          key: ${{ vars.KEY }}\n"), [("CACHE-024", "needs_review")])
        self.assertEqual(self._c24("          auto-key: ${{ inputs.auto }}\n"), [("CACHE-024", "needs_review")])
        self.assertEqual(
            self._c24("          key: auto\n          auto-key-trusted-writers: ${{ vars.W }}\n"),
            [("CACHE-024", "needs_review")],
        )

    @requires_yaml_tooling
    def test_composite_input_resolved(self) -> None:
        action = (
            "runs:\n  using: composite\n  steps:\n      - uses: zackees/setup-soldr@v0\n"
            "        with:\n          key: ${{ inputs.key }}\n"
            "inputs:\n  key:\n    default: auto\n"
        )
        repo = self._repo("on: [push]\njobs:\n  b:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: ./.github/actions/soldr\n", action)
        ci = load_ci_toml(repo).ci
        self.assertEqual([(f.rule, f.status.value) for f in check_cache_024(ci, repo)], [("CACHE-024", "violation")])


class SetupUvImpliedSaveGateTest(unittest.TestCase):
    """[cache.pr].mode = "none" implies the setup-uv save gate without an
    [allow].setup-uv opt-in (FastLED's ungated memory_report.yml saved a PR
    cache every push and failed every PR's live budget instead)."""

    def _ci(self, kind: str, mode: str):
        from ci_lint.schema import SetupUvAllow, StrMap

        repo = fixture("CACHE-003", kind)
        ci = load_ci_toml(repo).ci
        assert ci is not None
        ci = replace(
            ci,
            allow=replace(ci.allow, setup_uv=SetupUvAllow(require=StrMap())),
            cache=replace(ci.cache, pr=replace(ci.cache.pr, mode=mode, budget="")),
        )
        return ci, repo

    @requires_yaml_tooling
    def test_mode_none_flags_ungated_save(self) -> None:
        ci, repo = self._ci("red", "none")
        findings = check_cache_003_setup_uv(ci, repo)
        self.assertEqual(["CACHE-003"], [f.rule for f in findings])
        self.assertIn('mode = "none"', findings[0].message)

    @requires_yaml_tooling
    def test_mode_none_accepts_gated_save(self) -> None:
        ci, repo = self._ci("green", "none")
        self.assertEqual([], check_cache_003_setup_uv(ci, repo))

    @requires_yaml_tooling
    def test_mode_none_skips_disabled_cache(self) -> None:
        import shutil
        import tempfile

        src = fixture("CACHE-003", "red")
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp) / "repo"
            shutil.copytree(src, repo)
            wf = repo / ".github" / "workflows" / "ci.yml"
            wf.write_text(wf.read_text(encoding="utf-8").replace('save-cache: "true"', "enable-cache: false"),
                          encoding="utf-8")
            ci, _ = self._ci("red", "none")
            self.assertEqual([], check_cache_003_setup_uv(ci, repo))

    @requires_yaml_tooling
    def test_pr_caching_mode_without_opt_in_is_silent(self) -> None:
        ci, repo = self._ci("red", "delta")
        self.assertEqual([], check_cache_003_setup_uv(ci, repo))


class PrBudgetDerivedTest(unittest.TestCase):
    def _ci(self, **pr):
        repo = fixture("CACHE-003", "green")
        ci = load_ci_toml(repo).ci
        assert ci is not None
        return replace(ci, cache=replace(ci.cache, pr=replace(ci.cache.pr, **pr)))

    def test_explicit_budget_wins(self) -> None:
        self.assertEqual(1024 * 1024, cache_static.pr_budget_bytes(self._ci(budget="1MB", mode="none")))

    def test_mode_none_derives_zero(self) -> None:
        self.assertEqual(0, cache_static.pr_budget_bytes(self._ci(budget="", mode="none")))

    def test_per_pr_times_open_prs(self) -> None:
        ci = self._ci(budget="", mode="delta", max_per_pr="10MB", expected_open_prs=3)
        self.assertEqual(30 * 1024 * 1024, cache_static.pr_budget_bytes(ci))

    def test_underivable_is_none(self) -> None:
        ci = self._ci(budget="", mode="delta", max_per_pr="10MB", expected_open_prs=None)
        self.assertIsNone(cache_static.pr_budget_bytes(ci))
