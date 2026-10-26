"""`ci-lint cache audit` -- ci_lint.cache.audit (round-4A brief, deliverable
4). No network: every test injects a fake fetch/graphql that reads the
recorded fixtures under ci_lint/tests/fixtures/runtime/cache/ (one copied
verbatim from the live template repo, one built for rule coverage the live
repo doesn't currently exhibit -- see caches-synthetic-rules.json's own
"_note").
"""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import replace

from ci_lint.cache.audit import (
    _TRAILING_HASH_RE,
    _check_cache_001,
    _check_cache_005,
    _check_cache_006,
    _check_cache_006_same_lock,
    _same_lock_family,
    _shape,
    ClassifiedEntry,
    audit_classified,
    classify,
    infer_default_branch,
    run_audit,
)
from ci_lint.cache_lineage import parse_ancestor_key
from ci_lint.cache.github_cache import CacheEntry, GitHubApiError, list_caches
from ci_lint.finding import Finding, Status
from ci_lint.schema import CiToml, load_ci_toml
from ci_lint.tests.helpers import FIXTURES

CACHE_FIXTURES = FIXTURES / "runtime" / "cache"
REPO = CACHE_FIXTURES / "repo"
TOKEN = "t"
REPO_SLUG = "zackees/template-python-rust-cmd"


def _load_ci_toml_text(text: str) -> CiToml:
    """Load a ci.toml from a literal, in a temp dir (schema 3 is strict, so a
    test cannot skip keys)."""
    import tempfile
    from pathlib import Path as _P

    d = _P(tempfile.mkdtemp())
    (d / "ci.toml").write_text(text)
    _loaded = load_ci_toml(d)
    ci = _loaded.ci
    findings = _loaded.findings
    hard = [f for f in findings if f.rule != "CT-002"]
    assert not hard, [f.message for f in hard]
    return ci


def _load(name: str) -> dict[str, object]:
    return json.loads((CACHE_FIXTURES / name).read_text(encoding="utf-8"))


def _fetch_from(payload: dict[str, object]):
    def fetch(url: str, token: str):
        assert token
        if "actions/runs" in url:
            # RUST-004 (round-6E): audit_classified/run_audit now always
            # probes the default branch's latest successful dylint run
            # when fetch is given and a dylint family is declared. None of
            # THIS module's other fixtures are about RUST-004, so answer
            # "no runs" here -- _find_default_branch_dylint_run then
            # returns None and RUST-004 stays silent, exactly like before
            # round-6E for every test that doesn't ask about it.
            return {"workflow_runs": []}
        assert "actions/caches" in url
        return payload

    return fetch


def _graphql_from(payload: dict[str, object]):
    def graphql(query: str, token: str):
        assert token
        assert "pullRequest" in query
        return payload

    return graphql


class ClassifyLiveTemplateTest(unittest.TestCase):
    """caches-live-template.json is copied verbatim from `gh api repos/
    zackees/template-python-rust-cmd/actions/caches` (round-4A brief:
    "copy the JSON, don't mutate anything")."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        payload = _load("caches-live-template.json")
        entries = list_caches(_fetch_from(payload), TOKEN, REPO_SLUG)
        self.classified = classify(self.ci, entries)
        self.by_id = {c.entry.id: c for c in self.classified}

    def test_soldr_mini_classifies_once_declared(self) -> None:
        c = self.by_id[8237216840]
        self.assertEqual("soldr-mini", c.family_id)
        self.assertFalse(c.is_delta)
        self.assertFalse(c.is_retired)

    def test_registry_and_compile_classify(self) -> None:
        self.assertEqual("registry", self.by_id[8237268308].family_id)
        self.assertEqual("deps", self.by_id[8237210664].family_id)
        self.assertEqual("compile", self.by_id[8237210113].family_id)
        self.assertEqual("compile", self.by_id[8237266493].family_id)  # the "-dylint-" suffixed shape too

    def test_setup_uv_own_cache_classifies_once_declared(self) -> None:
        # round-4A amendment: astral-sh/setup-uv's own cache is now a
        # declared family in the fixture repo (via = "setup-uv", prefix
        # "setup-uv-2-") -- no longer a permanent CACHE-001. All three
        # entries in this fixture are the "setup-uv-2-..." shape.
        for cache_id in (8238163287, 8237209367, 8236693780):
            with self.subTest(cache_id=cache_id):
                c = self.by_id[cache_id]
                self.assertEqual("setup-uv-cache", c.family_id)
                self.assertFalse(c.is_delta)
                self.assertFalse(c.is_retired)

    def test_audit_findings_cache_001_cache_003_and_cache_006(self) -> None:
        report = audit_classified(
            self.ci, self.classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )

        # Two entries (8237209367 on refs/heads/main, 8236693780 on
        # refs/pull/17/merge) share an IDENTICAL key string, so `path`
        # ("cache:<key>") alone can't disambiguate which entry a finding is
        # about -- match on the SUBJECT "cache id=<N>" the message always
        # leads with instead (CACHE-006's message also mentions a second,
        # different id -- "kept newest id=<other>" -- so a bare substring
        # search on "id=<N>" would false-match that entry too).
        def findings_for(rule: str, cache_id: int) -> list:
            needle = f"cache id={cache_id} "
            return [f for f in report.findings if f.rule == rule and needle in f.message]

        # No more CACHE-001s: setup-uv-2-... is a declared family now.
        self.assertEqual(0, sum(1 for f in report.findings if f.rule == "CACHE-001"))
        # ...but two of its three entries live on refs/pull/*, exactly like
        # a base layer would -- CACHE-003, the same rule, no special case.
        self.assertEqual(1, len(findings_for("CACHE-003", 8238163287)))  # refs/pull/19/merge
        self.assertEqual(1, len(findings_for("CACHE-003", 8236693780)))  # refs/pull/17/merge
        self.assertEqual(0, len(findings_for("CACHE-003", 8237209367)))  # refs/heads/main
        self.assertEqual(2, sum(1 for f in report.findings if f.rule == "CACHE-003"))
        # zackees/ci.yml#88: the two "registry" entries share the Cargo.lock
        # hash (a87d2454829c34f1) and differ only in the trailing TOOLCHAIN
        # digest (5820... = the main build job, b72e... = the dylint job --
        # the same digests the two buildcache keys carry), so both are live
        # per-job entries, not generations. A real superseded registry
        # generation is covered by RegistryPerJobDigestTest below.
        self.assertEqual(0, len(findings_for("CACHE-006", 8237210495)))
        self.assertEqual(0, len(findings_for("CACHE-006", 8237268308)))
        # zackees/ci.yml#104: 8238163287 (23:42), 8237209367 (main, 23:37)
        # and 8236693780 (pr17, 22:47) are all the SAME "setup-uv-2-...
        # ubuntu-24.04-3.12.3" platform/python stem; only their trailing
        # 64-hex (sha256) dependency-file hash differs. Before #104's fix
        # `_shape` only stripped 6-40 hex chars, so the 64-hex tail was
        # never stripped and the older two never grouped with the newest
        # -- exactly the kernal-api "superseded setup-uv generation missed"
        # evidence. Now all three group together and both older-accessed
        # entries are superseded, keeping only the newest (8238163287).
        self.assertEqual(1, len(findings_for("CACHE-006", 8236693780)))
        self.assertEqual(1, len(findings_for("CACHE-006", 8237209367)))
        self.assertEqual(0, len(findings_for("CACHE-006", 8238163287)))
        self.assertEqual(250382443, report.total_bytes)
        self.assertEqual(0, sum(1 for f in report.findings if f.rule == "CACHE-009"))

    def test_gen_009_rollup_fires_when_cache_003_and_006_do(self) -> None:
        """RED: this fixture already produces CACHE-003 and CACHE-006
        findings (previous test) -- GEN-009 (issue #5) is the single
        fleet-level rollup a human/agent reads instead of grepping four
        rule IDs separately; it must fire here and cite both."""

        report = audit_classified(
            self.ci, self.classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )
        gen_009 = [f for f in report.findings if f.rule == "GEN-009"]
        self.assertEqual(1, len(gen_009), report.findings)
        self.assertIn("CACHE-003", gen_009[0].message)
        self.assertIn("CACHE-006", gen_009[0].message)

    def test_gen_009_absent_when_no_rollup_contributor_fires(self) -> None:
        """GREEN: an empty cache listing has nothing for CACHE-003/004/006/
        008 to flag, so GEN-009 must not fire either."""

        report = audit_classified(
            self.ci, (), graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )
        self.assertEqual([], [f for f in report.findings if f.rule == "GEN-009"])


class ClassifySyntheticRulesTest(unittest.TestCase):
    """caches-synthetic-rules.json + pr-states.json (both synthetic, per
    their own "_note") exercise CACHE-003/005/008/009, which the live
    template repo does not currently exhibit."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        payload = _load("caches-synthetic-rules.json")
        entries = list_caches(_fetch_from(payload), TOKEN, REPO_SLUG)
        self.classified = classify(self.ci, entries)
        self.report = audit_classified(
            self.ci,
            self.classified,
            graphql=_graphql_from(_load("pr-states.json")),
            token=TOKEN,
            repo=REPO_SLUG,
            default_branch="main",
        )
        self.by_rule_id = {(f.rule, f.path) for f in self.report.findings}

    def test_cache_003_base_layer_on_a_pr_ref(self) -> None:
        self.assertIn(("CACHE-003", "cache:setup-soldr-buildcache-v2-linux-x64-deadbeefdeadbeef-cafebabecafebabe"), self.by_rule_id)

    def test_cache_005_tiny_payload(self) -> None:
        self.assertIn(("CACHE-005", "cache:setup-soldr-cargoregistry-v1-linux-x64-deadbeefdeadbeef-cafebabecafebabe"), self.by_rule_id)

    def test_cache_008_closed_pr_delta(self) -> None:
        closed = [f for f in self.report.findings if f.rule == "CACHE-008" and "pr7" in f.path]
        self.assertEqual(1, len(closed))
        self.assertIn("closed", closed[0].message)

    def test_cache_008_stale_base_delta(self) -> None:
        # id 9002's key has no "pr7"/"pr8" substring conflict issue since we
        # match by path instead.
        stale = [
            f
            for f in self.report.findings
            if f.rule == "CACHE-008" and f.path == "cache:delta-v1-pr8-compile-linux-x64-bdeadbeef"
        ]
        self.assertEqual(1, len(stale))
        self.assertIn("base hash", stale[0].message)

    def test_cache_009_retired_family_live(self) -> None:
        self.assertIn(("CACHE-009", "cache:cook-delta-v2-linux-x64-glibc-rustc1.95.0-fnone-la87d2454829c34f1-soldrv0.9.25"), self.by_rule_id)

    def test_matching_delta_base_is_not_flagged_stale(self) -> None:
        # id 9001's b43cef837 DOES match the live "compile" base entry's
        # own key hash -- it must be reported via the "closed" reason only,
        # never also as a stale-base mismatch.
        for f in self.report.findings:
            if f.path == "cache:delta-v1-pr7-compile-linux-x64-b43cef837":
                self.assertIn("closed", f.message)


class ClassifyGenerationSuffixedDeltaTest(unittest.TestCase):
    """Round-4C: a delta key carries a `-g<sha8>` per-generation commit-sha
    suffix (template-python-rust-cmd#31 -- cache entries are immutable, so
    each save needs a distinct key; the previous generation is healed once
    a new one lands). `classify()` predates that suffix and, before this
    fix, silently fell through to CACHE-001 "undeclared family" instead of
    recognizing it as a delta at all -- which meant CACHE-008 (closed/
    merged-PR trim) never saw it either. RED: reverting DELTA_OUTER_RE's
    trailing `(?:-g...)?` group reproduces the miss (is_delta=False)."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def test_delimited_pr_delta_key_is_classified_as_a_delta(self) -> None:
        # zackees/ci.yml#23 §5: build_delta_key now emits `delta-v1-pr-<N>-...`.
        entry = CacheEntry(
            id=2,
            ref="refs/pull/36/merge",
            key="delta-v1-pr-36-compile-linux-x64-ba37ca145-g5601e597",
            version="v1",
            size_in_bytes=1_224_921,
            created_at="2026-09-29T02:49:27Z",
            last_accessed_at="2026-09-29T02:49:27Z",
        )
        (classified,) = classify(self.ci, [entry])
        self.assertTrue(classified.is_delta, "delimited pr-<N> delta key must classify as a delta, not CACHE-001")
        assert classified.delta is not None
        self.assertEqual(36, classified.delta.pr)
        self.assertEqual("compile", classified.delta.family)
        self.assertEqual("linux-x64", classified.delta.platform)
        self.assertEqual(36, classified.pr)

    def test_generation_suffixed_key_is_still_classified_as_a_delta(self) -> None:
        entry = CacheEntry(
            id=1,
            ref="refs/pull/31/merge",
            key="delta-v1-pr31-compile-linux-x64-ba37ca145-g5601e597",
            version="v1",
            size_in_bytes=1_224_921,
            created_at="2026-09-29T02:49:27Z",
            last_accessed_at="2026-09-29T02:49:27Z",
        )
        (classified,) = classify(self.ci, [entry])
        self.assertTrue(classified.is_delta, "generation-suffixed delta key must classify as a delta, not CACHE-001")
        assert classified.delta is not None
        self.assertEqual(31, classified.delta.pr)
        self.assertEqual("compile", classified.delta.family)
        self.assertEqual("linux-x64", classified.delta.platform)
        self.assertEqual("a37ca145", classified.delta.base8)

    def test_non_suffixed_key_still_classifies_identically(self) -> None:
        # The optional group must not change parsing for every EXISTING
        # (pre-round-4C) delta key shape.
        entry = CacheEntry(
            id=2,
            ref="refs/pull/7/merge",
            key="delta-v1-pr7-compile-linux-x64-b43cef837",
            version="v1",
            size_in_bytes=1000,
            created_at="2026-09-29T02:49:27Z",
            last_accessed_at="2026-09-29T02:49:27Z",
        )
        (classified,) = classify(self.ci, [entry])
        self.assertTrue(classified.is_delta)
        assert classified.delta is not None
        self.assertEqual("43cef837", classified.delta.base8)


class RunAuditErrorHandlingTest(unittest.TestCase):
    def test_fetch_failure_raises_audit_error_not_a_crash(self) -> None:
        from ci_lint.cache.audit import AuditError

        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings

        def broken_fetch(url: str, token: str):
            raise GitHubApiError("network is down")

        with self.assertRaises(AuditError):
            run_audit(ci, fetch=broken_fetch, graphql=None, token=TOKEN, repo=REPO_SLUG)

    def test_graphql_failure_degrades_to_a_warning_not_a_crash(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        payload = _load("caches-synthetic-rules.json")

        def broken_graphql(query: str, token: str):
            raise GitHubApiError("graphql is down")

        report = run_audit(
            ci, fetch=_fetch_from(payload), graphql=broken_graphql, token=TOKEN, repo=REPO_SLUG
        )
        self.assertIsNotNone(report.warning)
        # CACHE-008's PR-state half degraded, but the base-hash-mismatch
        # half (which needs no GraphQL) must still have fired for PR #8's
        # deliberately-stale delta.
        self.assertTrue(
            any(f.rule == "CACHE-008" and "pr8" in f.path for f in report.findings if f.path)
        )


def _runs_jobs_fetch(runs_name: str, jobs_by_run_id: dict[int, str]):
    """A fake fetch answering `actions/runs?...` with `runs_name`'s fixture
    and `actions/runs/{id}/jobs` with `jobs_by_run_id[id]`'s fixture --
    RUST-004's own two-call shape (`_find_default_branch_dylint_run`).
    Never answers `actions/caches` -- these tests build `classified`
    directly (frozen `ClassifiedEntry`s), no cache listing involved."""

    def fetch(url: str, token: str):
        assert token
        if "/jobs" in url:
            for run_id, name in jobs_by_run_id.items():
                if f"/actions/runs/{run_id}/jobs" in url:
                    return _load(name)
            raise AssertionError(f"no jobs fixture registered for {url}")
        if "actions/runs" in url:
            return _load(runs_name)
        raise AssertionError(f"unexpected url in RUST-004 test: {url}")

    return fetch


def _classified_entry(entry_id: int, family_id: str, ref: str, created_at: str) -> ClassifiedEntry:
    entry = CacheEntry(
        id=entry_id,
        ref=ref,
        key=f"{family_id}-synthetic-{entry_id}",
        version="1",
        size_in_bytes=5_000_000,
        created_at=created_at,
        last_accessed_at=created_at,
    )
    return ClassifiedEntry(entry=entry, family_id=family_id, is_delta=False, delta=None, is_retired=False)


DYLINT_RUN_ID = 36509863041
MAIN_REF = "refs/heads/main"


class Rust004DylintOutputCacheSaveTest(unittest.TestCase):
    """Round-6E, item 3 (zackees/ci.yml#1): a 'setup-soldr:dylint-output'/
    'setup-soldr:dylint' family with NO live entry at all on the default
    branch, while the default branch's latest run has a successful
    'dylint' job -> RUST-004. `ci_lint/tests/fixtures/runtime/cache/repo/
    ci.toml` already declares both families ('dylint' via=setup-soldr:
    dylint, 'dylint-out' via=setup-soldr:dylint-output).

    NOT "no entry created since the job started": a live run against the
    real template repo (round-6E) proved that stricter signal false-
    positives on an entirely healthy case -- an unrelated commit can
    legitimately reproduce dylintOutputKey's IDENTICAL hash and correctly
    get an "exact hit - skipping save" (confirmed in zackees/
    template-python-rust-cmd run 36514507620's own job log), so an older
    entry is not itself evidence of anything wrong -- see
    `test_stale_but_present_entry_is_green` below."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def test_no_entry_at_all_on_default_branch_is_rust_004(self) -> None:
        # Only a PR-ref entry exists for 'dylint-out'; 'dylint' has a
        # fresh main-ref entry -- RUST-004 must fire for 'dylint-out'
        # ONLY, proving this is evaluated per family, not as one blanket
        # pass/fail.
        classified = (
            _classified_entry(1, "dylint-out", "refs/pull/9/merge", "2026-09-29T01:52:10Z"),
            _classified_entry(2, "dylint", MAIN_REF, "2026-09-29T01:52:10Z"),
        )
        fetch = _runs_jobs_fetch(
            "rust004-runs-main-success.json", {DYLINT_RUN_ID: "rust004-jobs-dylint-success.json"}
        )
        report = audit_classified(
            self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main", fetch=fetch
        )
        rust004 = [f for f in report.findings if f.rule == "RUST-004"]
        self.assertEqual(1, len(rust004))
        self.assertIn("dylint-out", rust004[0].path)
        self.assertIn("no live entry at all", rust004[0].message)
        self.assertIn("setup-soldr#538", rust004[0].message)

    def test_stale_but_present_entry_is_green(self) -> None:
        # A main-ref entry exists for BOTH families, but 'dylint-out's is
        # from well before the qualifying job started -- an "exact hit,
        # skipping save" run (a legitimate, healthy warm cache, per the
        # class docstring's live evidence) looks EXACTLY like this. An
        # older-but-present entry is never itself a RUST-004 violation.
        classified = (
            _classified_entry(1, "dylint-out", MAIN_REF, "2026-09-20T00:00:00Z"),  # old, still present
            _classified_entry(2, "dylint", MAIN_REF, "2026-09-29T01:52:10Z"),
        )
        fetch = _runs_jobs_fetch(
            "rust004-runs-main-success.json", {DYLINT_RUN_ID: "rust004-jobs-dylint-success.json"}
        )
        report = audit_classified(
            self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main", fetch=fetch
        )
        self.assertEqual([], [f for f in report.findings if f.rule == "RUST-004"])

    def test_entry_present_on_default_branch_is_green(self) -> None:
        classified = (
            _classified_entry(1, "dylint-out", MAIN_REF, "2026-09-29T01:52:10Z"),
            _classified_entry(2, "dylint", MAIN_REF, "2026-09-29T01:52:10Z"),
        )
        fetch = _runs_jobs_fetch(
            "rust004-runs-main-success.json", {DYLINT_RUN_ID: "rust004-jobs-dylint-success.json"}
        )
        report = audit_classified(
            self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main", fetch=fetch
        )
        self.assertEqual([], [f for f in report.findings if f.rule == "RUST-004"])

    def test_no_successful_dylint_job_yet_is_silent_not_a_false_positive(self) -> None:
        classified = ()  # no live cache entries at all
        fetch = _runs_jobs_fetch(
            "rust004-runs-main-success.json", {DYLINT_RUN_ID: "rust004-jobs-dylint-failure.json"}
        )
        report = audit_classified(
            self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main", fetch=fetch
        )
        self.assertEqual([], [f for f in report.findings if f.rule == "RUST-004"])

    def test_no_fetch_given_skips_rust_004_entirely(self) -> None:
        """`fetch=None` (the default, and what ci_lint.cache.ops's
        trim/janitor/preprune pass implicitly by never naming it) means
        zero RUST-004 network calls -- identical to pre-round-6E
        behavior."""

        classified = (_classified_entry(1, "dylint-out", "refs/pull/9/merge", "2026-09-29T01:52:10Z"),)
        report = audit_classified(
            self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )
        self.assertEqual([], [f for f in report.findings if f.rule == "RUST-004"])

    def test_no_declared_dylint_family_never_calls_fetch(self) -> None:
        _loaded = load_ci_toml(FIXTURES / "runtime" / "suite")
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.assertFalse(any(fam.via in ("setup-soldr:dylint", "setup-soldr:dylint-output") for fam in ci.cache.family.values()))

        def unreachable_fetch(url: str, token: str):
            raise AssertionError("fetch must not be called when no dylint family is declared")

        report = audit_classified(
            ci, (), graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main", fetch=unreachable_fetch
        )
        self.assertEqual([], [f for f in report.findings if f.rule == "RUST-004"])


class SetupUv64HexGenerationTest(unittest.TestCase):
    """zackees/ci.yml#104: setup-uv's own trailing key hash is a full
    sha256 (64 hex chars), longer than the 6-40 hex bound `_shape` used to
    strip. A superseded setup-uv-2- generation for the SAME platform stem
    must still be detected (RED before the fix, GREEN after); a different
    platform stem with the same hash shape must never be grouped with it,
    and setup-soldr's cargo-registry toolchain-digest handling (#101) must
    stay unaffected since it matches its own regex before the trailing-hash
    strip ever runs."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        entries = list_caches(_fetch_from(_load("caches-setup-uv-64hex-generation.json")), TOKEN, REPO_SLUG)
        self.classified = classify(ci, entries)
        self.by_id = {c.entry.id: c for c in self.classified}

    def test_all_three_classify_as_setup_uv_cache(self) -> None:
        self.assertEqual("setup-uv-cache", self.by_id[1].family_id)
        self.assertEqual("setup-uv-cache", self.by_id[2].family_id)
        self.assertEqual("setup-uv-cache", self.by_id[3].family_id)

    def test_older_same_platform_generation_is_superseded(self) -> None:
        report = audit_classified(
            self.ci, self.classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )
        c006 = [f for f in report.findings if f.rule == "CACHE-006"]
        self.assertEqual(1, len(c006), [f.message for f in c006])
        self.assertTrue(c006[0].message.startswith("cache id=1 "), c006[0].message)
        self.assertIn("kept newest id=2 ", c006[0].message)
        # id 3 (different platform) never appears as superseded or as "kept".
        self.assertNotIn("id=3", c006[0].message)


if __name__ == "__main__":
    unittest.main()


class SetupUvLegacyGenerationTest(unittest.TestCase):
    """zackees/ci.yml#87: astral-sh/setup-uv v6 writes `setup-uv-1-` keys;
    a declared `via = "setup-uv"` family must own them (GREEN) while an
    unknown generation still reports CACHE-001 (RED)."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        entries = list_caches(_fetch_from(_load("caches-setup-uv-v1.json")), TOKEN, REPO_SLUG)
        self.classified = classify(ci, entries)
        self.by_id = {c.entry.id: c for c in self.classified}

    def test_green_v1_and_v2_generations_classify(self) -> None:
        self.assertEqual("setup-uv-cache", self.by_id[1].family_id)
        self.assertEqual("setup-uv-cache", self.by_id[2].family_id)

    def test_red_unknown_generation_stays_cache_001(self) -> None:
        self.assertIsNone(self.by_id[3].family_id)
        report = audit_classified(
            self.ci, self.classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )
        c001 = [f for f in report.findings if f.rule == "CACHE-001"]
        self.assertEqual(1, len(c001))
        self.assertIn("id=3 ", c001[0].message)


class RegistryPerJobDigestTest(unittest.TestCase):
    """zackees/ci.yml#88: setup-soldr cargo-registry keys end in
    `<Cargo.lock hash>-<toolchain digest>`; per-job digests are concurrent
    live entries (GREEN), only an older Cargo.lock hash under the SAME digest
    is superseded (RED, CACHE-006)."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        entries = list_caches(_fetch_from(_load("caches-registry-per-job.json")), TOKEN, REPO_SLUG)
        self.classified = classify(ci, entries)

    def test_only_the_older_lockfile_generation_is_superseded(self) -> None:
        self.assertTrue(all(c.family_id == "registry" for c in self.classified))
        report = audit_classified(
            self.ci, self.classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main"
        )
        c006 = [f for f in report.findings if f.rule == "CACHE-006"]
        self.assertEqual(1, len(c006), [f.message for f in c006])
        self.assertTrue(c006[0].message.startswith("cache id=4 "), c006[0].message)
        self.assertIn("kept newest id=1 ", c006[0].message)


class FamilyLiveExcessTest(unittest.TestCase):
    """ci.yml#139 (kernal-api #93): live default-branch entries beyond a
    family's declared per-cardinality or max are CACHE-004 -- a violation
    when the family's live bytes exceed its declared worst case, else
    needs_review. The fixture's soldr-mini family is per = "platform" (2
    platforms) with max = "5MB"."""

    MB = 1024 * 1024

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def _family_findings(self, specs: list[tuple[str, str, int]]):
        entries = [
            {
                "id": i, "ref": ref, "key": key, "version": "v", "size_in_bytes": size,
                "created_at": "2026-09-01T00:00:00Z", "last_accessed_at": "2026-09-01T00:00:00Z",
            }
            for i, (key, ref, size) in enumerate(specs, start=1)
        ]
        classified = classify(self.ci, list_caches(_fetch_from({"actions_caches": entries}), TOKEN, REPO_SLUG))
        report = audit_classified(self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG)
        return [f for f in report.findings if f.rule == "CACHE-004" and f.path == "cache:family:soldr-mini"]

    def test_green_within_model(self) -> None:
        found = self._family_findings([
            ("soldr-mini-v2-linux-x64-glibc-v0.9.25", "refs/heads/main", 4 * self.MB),
            ("soldr-mini-v2-windows-x64-msvc-v0.9.25", "refs/heads/main", 4 * self.MB),
            # A PR-ref entry is CACHE-003/008's business, not this model check.
            ("soldr-mini-v2-linux-x64-glibc-v0.9.26", "refs/pull/7/merge", 4 * self.MB),
        ])
        self.assertEqual([], found)

    def test_red_leftover_writer_shapes_break_the_proof(self) -> None:
        found = self._family_findings([
            ("soldr-mini-v2-linux-x64-glibc-v0.9.25", "refs/heads/main", 4 * self.MB),
            ("soldr-mini-v2-linux-x64-musl-v0.9.25", "refs/heads/main", 4 * self.MB),
            ("soldr-mini-v2-linux-arm64-glibc-v0.9.25", "refs/heads/main", 4 * self.MB),
            ("soldr-mini-v2-windows-x64-msvc-v0.9.25", "refs/heads/main", 6 * self.MB),
        ])
        self.assertEqual(1, len(found), [f.message for f in found])
        self.assertEqual(Status.VIOLATION, found[0].status)
        self.assertIn("4 live entries", found[0].message)
        self.assertIn("exceed max", found[0].message)

    def test_count_excess_that_fits_the_bytes_is_needs_review(self) -> None:
        found = self._family_findings([
            ("soldr-mini-v2-linux-x64-glibc-v0.9.25", "refs/heads/main", 2 * self.MB),
            ("soldr-mini-v2-linux-x64-musl-v0.9.25", "refs/heads/main", 2 * self.MB),
            ("soldr-mini-v2-windows-x64-msvc-v0.9.25", "refs/heads/main", 2 * self.MB),
        ])
        self.assertEqual(1, len(found), [f.message for f in found])
        self.assertEqual(Status.NEEDS_REVIEW, found[0].status)


class AncestorPilotShapeTest(unittest.TestCase):
    """setup-soldr's ancestor pilot (#566, zackees/ci.yml#335) carries its sha
    MID-key, so `_shape`'s trailing-hash strip left the key intact and two
    saves of one lineage never grouped -- CACHE-006 would go blind to every
    ancestor entry exactly when the pilot is adopted.

    Same class of bug as #104 (a 64-hex tail `_shape` never matched), one
    position worse: there the hash was at the end, here it is in the middle.
    """

    IDENTITY = "63942eb6ab326045"

    def _key(self, sha: str, run: int, pr: int | None = None, identity: str | None = None) -> str:
        tag = f"-pr-{pr}" if pr is not None else ""
        ident = identity or self.IDENTITY
        return f"setup-soldr-ancestor-build-v1-{ident}-source-{sha}-run-{run}-attempt-1{tag}"

    def test_same_lineage_saves_group_despite_differing_provenance(self) -> None:
        a = parse_ancestor_key(self._key("a" * 40, 1, pr=42))
        b = parse_ancestor_key(self._key("b" * 40, 9, pr=42))
        assert a is not None and b is not None
        # sha/run/attempt are per-save provenance; they must NOT split a group.
        self.assertEqual(a.family, b.family)

    def test_identity_and_pr_separate_groups(self) -> None:
        base = parse_ancestor_key(self._key("a" * 40, 1, pr=42))
        assert base is not None
        other_ident = parse_ancestor_key(self._key("a" * 40, 1, pr=42, identity="deadbeefdeadbeef"))
        other_pr = parse_ancestor_key(self._key("a" * 40, 1, pr=43))
        main_ref = parse_ancestor_key(self._key("a" * 40, 1))
        assert other_ident is not None and other_pr is not None and main_ref is not None
        self.assertNotEqual(base.family, other_ident.family)
        self.assertNotEqual(base.family, other_pr.family)
        self.assertNotEqual(base.family, main_ref.family)

    def test_shape_groups_two_saves_that_the_old_strip_missed(self) -> None:
        k1, k2 = self._key("a" * 40, 1, pr=42), self._key("b" * 40, 9, pr=42)
        # What the old trailing-hash strip produced: two distinct shapes.
        old1 = _TRAILING_HASH_RE.sub("", k1) or k1
        old2 = _TRAILING_HASH_RE.sub("", k2) or k2
        self.assertNotEqual(old1, old2, "fixture no longer reproduces the bug")
        self.assertEqual(_shape(k1), _shape(k2))

    def test_end_to_end_cache_006_sees_two_ancestor_saves(self) -> None:
        """Both halves are load-bearing, and neither alone is enough.

        #337 fixed `_shape` (which collapses the mid-key provenance), but a
        repo can only reach it once `classify()` has resolved the entry to a
        DECLARED family. Without the `setup-soldr-ancestor-build-v1-` shape,
        `family_id` is None, `_check_cache_006` skips on `family_id is None`,
        and the rule is blind -- verified by removing the shape and re-running.
        """

        ci = _load_ci_toml_text(
            """
schema = 3
profile = "rust-pypi-app"
linter = "zackees/ci.yml@0000000000000000000000000000000000000000"
[platforms]
linux-x64 = { target = "x86_64-unknown-linux-gnu", runs-on = "ubuntu-24.04", group = "linux" }
[python]
backend = "soldr"
cli = { name = "x", crate = "x", native = false }
abi3 = "cp310"
pythons = ["3.12"]
[rust]
public = "."
private = ""
[rust.tests]
binaries = []
[suites]
unit = { run = "ci/test.py unit", required = true }
[flow.pr]
[flow.main]
[tags]
[cache]
budget = "9GB"
write-on = ["main"]
pr = { mode = "none", families = [], max-per-pr = "0MB", budget = "0MB", trim = "on-close" }
[cache.family]
compile = { via = "setup-soldr:ancestor-build", max = "2GB", lockfile = true, per = "platform", promote = "ancestor" }
[cache.promote]
mode = "ancestor"
max-age-hours = 168
"""
        )
        ident = self.IDENTITY
        old = f"setup-soldr-ancestor-build-v1-{ident}-source-{'a' * 40}-run-1-attempt-1-pr-42"
        new = f"setup-soldr-ancestor-build-v1-{ident}-source-{'b' * 40}-run-2-attempt-1-pr-42"
        entries = [
            CacheEntry(id=1, ref="refs/pull/42/merge", key=old, version="1", size_in_bytes=10**9,
                       created_at="2026-10-01T00:00:00Z", last_accessed_at="2026-10-01T00:00:00Z"),
            CacheEntry(id=2, ref="refs/pull/42/merge", key=new, version="1", size_in_bytes=10**9,
                       created_at="2026-10-02T00:00:00Z", last_accessed_at="2026-10-03T00:00:00Z"),
        ]
        classified = classify(ci, entries)
        # Both must resolve to the declared family, or the rule never runs.
        self.assertEqual(["compile", "compile"], [c.family_id for c in classified])
        findings = _check_cache_006(ci, classified)
        self.assertEqual(["CACHE-006"], [f.rule for f in findings])
        self.assertEqual(1, findings[0].cache_id)  # the older access is superseded

    def test_parser_rejects_other_encodings(self) -> None:
        # The m<n> label belongs to parse_key; anything else is not ours.
        self.assertIsNone(parse_ancestor_key("att1-rust.x86_64-unknown-linux-gnu.test-m2189-c1-18204251ce-pr-3532"))
        self.assertIsNone(parse_ancestor_key("setup-soldr-buildcache-v2-linux-x64-63942eb6ab326045"))
        # Bounds mirror setup-soldr's own makeAncestorKey: identity >= 16 hex.
        self.assertIsNone(parse_ancestor_key(self._key("a" * 40, 1, identity="abcd")))
        # run/attempt must be positive integers.
        self.assertIsNone(parse_ancestor_key(f"setup-soldr-ancestor-build-v1-{self.IDENTITY}-source-{'a' * 40}-run-0-attempt-1"))

class LiveExcessShapesTest(unittest.TestCase):
    """`ci-lint cache audit`'s live-excess check must read the SAME number
    CACHE-004's static proof reads.

    Third consumer found to disagree after the janitor fix (#343):
    `_check_family_live_excess` computed `cardinality(per) * max` while a
    `shapes` family's static budget is the SUM of its shape ceilings. On
    zackees/wild that charged `cook` a modelled 2 entries against 3 live
    ones and raised a spurious violation for a family whose declaration was
    correct -- and it also printed the wrong "declared worst case" in the
    message. A family that is over its real budget should be the only
    thing this check reports.
    """

    def test_a_shapes_family_is_not_charged_max_times_cardinality(self) -> None:
        from dataclasses import replace as _replace

        from ci_lint.cache.audit import _check_family_live_excess
        from ci_lint.schema import CacheShape

        ci = load_ci_toml(REPO).ci
        fam = replace(
            ci.cache.family["deps"],
            max="10MB",
            per=None,
            evict="lru",
            shapes=(CacheShape("a", "700MB"), CacheShape("b", "660MB"), CacheShape("c", "420MB")),
        )
        shaped = replace(ci, cache=replace(ci.cache, family={**ci.cache.family, "deps": fam}))
        def big(i: int, mb: int) -> ClassifiedEntry:
            base = _classified_entry(i, "deps", "refs/heads/main", "2026-10-01T00:00:00Z")
            return replace(
                base,
                entry=replace(base.entry, size_in_bytes=mb * 1024**2),
            )

        entries = tuple(big(i, mb) for i, mb in ((1, 666), (2, 640), (3, 410)))
        # 3 live entries against 3 declared shapes: no finding.
        self.assertEqual(_check_family_live_excess(shaped, entries, "main"), [])
        # The same 3 entries under `per = None` (cardinality 1) IS a real
        # excess, so the check still bites when the model really is too small.
        plain = replace(shaped, cache=replace(shaped.cache, family={**shaped.cache.family,
                     "deps": replace(fam, shapes=())}))
        self.assertTrue(_check_family_live_excess(plain, entries, "main"))


class LiveExcessLruCountTest(unittest.TestCase):
    """An `evict = "lru"` family is byte-bounded: many tiny content-addressed
    entries inside its footprint are not a CACHE-004 live excess (kernal-api's
    soldr-verdict, 225 entries / 106 KB against `max = "1MB"`, forge-ci/ci.yml#37)."""

    def test_lru_family_counts_bytes_not_entries(self) -> None:
        from ci_lint.cache.audit import _check_family_live_excess

        ci = load_ci_toml(REPO).ci
        fam = replace(ci.cache.family["deps"], max="1MB", per=None, evict="lru", shapes=(), lockfile=False, min="100B")
        lru = replace(ci, cache=replace(ci.cache, family={**ci.cache.family, "deps": fam}))

        def small(i: int, size: int) -> ClassifiedEntry:
            base = _classified_entry(i, "deps", "refs/heads/main", "2026-10-01T00:00:00Z")
            return replace(base, entry=replace(base.entry, size_in_bytes=size))

        tiny = tuple(small(i, 470) for i in range(1, 226))
        self.assertEqual(_check_family_live_excess(lru, tiny, "main"), [])
        # Over the byte footprint it still fires, and a non-lru family still counts entries.
        heavy = tuple(small(i, 300 * 1024) for i in range(1, 6))
        self.assertTrue(_check_family_live_excess(lru, heavy, "main"))
        plain = replace(lru, cache=replace(lru.cache, family={**lru.cache.family, "deps": replace(fam, evict=None)}))
        self.assertTrue(_check_family_live_excess(plain, tiny, "main"))


class Cache005DeclaredMinTest(unittest.TestCase):
    """zackees/ci.yml#417: a family whose entries are tiny by design (soldr
    verdict records, ~400 B; empty pass markers) declares `min`, and the
    declared minimum replaces the 1 KB poison-guard floor. A family that
    declares nothing keeps the floor."""

    KEY = "soldr-mini-v2-linux-x64-glibc-v0.9.25"

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def _with_min(self, value: str | None) -> CiToml:
        fam = replace(self.ci.cache.family["soldr-mini"], min=value)
        family = {**self.ci.cache.family, "soldr-mini": fam}
        return replace(self.ci, cache=replace(self.ci.cache, family=family))

    def _flagged(self, ci: CiToml, size: int) -> list[str]:
        entry = CacheEntry(
            id=1, ref=MAIN_REF, key=self.KEY, version="v", size_in_bytes=size,
            created_at="2026-10-24T00:00:00Z", last_accessed_at="2026-10-24T00:00:00Z",
        )
        classified = (ClassifiedEntry(entry=entry, family_id="soldr-mini", is_delta=False, delta=None, is_retired=False),)
        return [f.message for f in _check_cache_005(ci, classified)]

    def test_undeclared_min_keeps_the_floor(self) -> None:
        self.assertEqual(1, len(self._flagged(self._with_min(None), 400)))

    def test_declared_zero_min_keeps_tiny_entries(self) -> None:
        self.assertEqual([], self._flagged(self._with_min("0B"), 400))

    def test_declared_min_still_flags_below_it(self) -> None:
        found = self._flagged(self._with_min("300B"), 200)
        self.assertEqual(1, len(found))
        self.assertIn("below [cache.family.soldr-mini].min", found[0])

    def test_unclassified_tiny_entry_keeps_the_floor(self) -> None:
        entry = CacheEntry(
            id=2, ref=MAIN_REF, key="unknown-x", version="v", size_in_bytes=10,
            created_at="2026-10-24T00:00:00Z", last_accessed_at="2026-10-24T00:00:00Z",
        )
        classified = (ClassifiedEntry(entry=entry, family_id=None, is_delta=False, delta=None, is_retired=False),)
        self.assertEqual(1, len(_check_cache_005(self._with_min("0B"), classified)))


class CacheAttestationSideEntryTest(unittest.TestCase):
    """zackees/ci.yml#412: `ci-lint attest keys` writes one ~440 B side entry
    per gate, keyed `att1-<gate>-<lineage>` (attest_cli.SIDE_FAMILY). The
    attestation family is known to ci-lint itself: CACHE-005's 1 KB floor
    never judges it (bosn#721 planned 78 of 196 as poisoned, and CACHE-005
    bypasses the newest-entry guard, so the janitor fought
    `attest resolve --require-gate`), and CACHE-001 never calls it
    undeclared (obs-studio's janitor deleted the lineage outright). A
    declared family `min` still wins over the builtin exemption, so a
    repository can opt the family back under a floor of its own."""

    KEY = "att1-rust.x86_64-unknown-linux-gnu.test-m2189-c1-18204251ce"
    PR_KEY = "att1-rust.x86_64-unknown-linux-gnu.test-m2189-c1-18204251ce-pr-3532"

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def _entry(self, key: str = KEY, size: int = 440) -> CacheEntry:
        return CacheEntry(
            id=7, ref=MAIN_REF, key=key, version="v", size_in_bytes=size,
            created_at="2026-10-24T00:00:00Z", last_accessed_at="2026-10-24T00:00:00Z",
        )

    def _undeclared(self, key: str = KEY) -> ClassifiedEntry:
        return ClassifiedEntry(
            entry=self._entry(key), family_id=None, is_delta=False, delta=None, is_retired=False
        )

    def _declared(self, min_value: str | None, size: int = 440) -> tuple[CiToml, tuple[ClassifiedEntry, ...]]:
        # The issue's exact shape: a family declared for the attestation
        # entries (prefix matches) without a `min`.
        fam = replace(self.ci.cache.family["soldr-mini"], prefix="att1-", min=min_value)
        ci = replace(self.ci, cache=replace(self.ci.cache, family={**self.ci.cache.family, "soldr-mini": fam}))
        entry = self._entry(size=size)
        classified = (ClassifiedEntry(entry=entry, family_id="soldr-mini", is_delta=False, delta=None, is_retired=False),)
        return ci, classified

    def test_floor_never_judges_undeclared_attestation_entries(self) -> None:
        self.assertEqual([], _check_cache_005(self.ci, (self._undeclared(),)))

    def test_declared_family_without_min_is_still_exempt(self) -> None:
        ci, classified = self._declared(min_value=None)
        self.assertEqual([], _check_cache_005(ci, classified))

    def test_declared_min_still_wins_over_the_builtin_exempt(self) -> None:
        ci, classified = self._declared(min_value="500B", size=440)
        found = [f.message for f in _check_cache_005(ci, classified)]
        self.assertEqual(1, len(found))
        self.assertIn("below [cache.family.soldr-mini].min", found[0])

    def test_non_attestation_tiny_entry_keeps_the_floor(self) -> None:
        entry = ClassifiedEntry(
            entry=self._entry(key="unknown-x", size=10),
            family_id=None, is_delta=False, delta=None, is_retired=False,
        )
        self.assertEqual(1, len(_check_cache_005(self.ci, (entry,))))

    def test_cache_001_never_calls_attestation_entries_undeclared(self) -> None:
        self.assertEqual([], _check_cache_001((self._undeclared(),)))
        # Control: any other undeclared key is still a finding.
        self.assertEqual(1, len(_check_cache_001((self._undeclared(key="unknown-x"),))))

    def test_attestation_entries_remain_pr_reclaimable(self) -> None:
        # The exemption is only from CACHE-001/CACHE-005: a pr-<N>-tagged
        # side entry is still claimed by its PR, so CACHE-008 reclaims it
        # when the PR closes (janitor plans come from findings, so this is
        # the lineage's remaining bounded-shed path).
        classified = classify(self.ci, [self._entry(key=self.PR_KEY)])
        self.assertEqual(3532, classified[0].pr)
        self.assertTrue(classified[0].pr_in_key)


class DefaultBranchInferenceTest(unittest.TestCase):
    """zackees/ci.yml#425: `cache audit` / `janitor` / `trim` / `preprune`
    and `precheck --live` never guess `main`. With no explicit
    `--default-branch` (the CLI default is now "infer") the audit resolves
    the branch from the Actions event payload, then one read-only
    `GET /repos/{repo}`; unresolvable, the default-branch-relative checks
    are skipped behind a single needs_review finding instead of emitting a
    guessed one (obs-studio's `master` drew 33 false CACHE-003)."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self._saved_env = os.environ.get("GITHUB_EVENT_PATH")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("GITHUB_EVENT_PATH", None)
        else:
            os.environ["GITHUB_EVENT_PATH"] = self._saved_env

    def _payload(self, branch: str) -> None:
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "event.json"
        path.write_text(json.dumps({"repository": {"default_branch": branch}}), encoding="utf-8")
        os.environ["GITHUB_EVENT_PATH"] = str(path)

    def _classified(self, *refs: str) -> tuple[ClassifiedEntry, ...]:
        # setup-uv-2-... is a declared base-layer family in the fixture repo,
        # so each entry is CACHE-003-eligible on a non-default ref.
        entries = [
            CacheEntry(
                id=900 + i, ref=ref, key=f"setup-uv-2-linux-x64-py3.11-abcd{i:04d}", version="v",
                size_in_bytes=4096, created_at="2026-10-25T00:00:00Z",
                last_accessed_at="2026-10-25T00:00:00Z",
            )
            for i, ref in enumerate(refs)
        ]
        return classify(self.ci, entries)

    def _report(self, classified, *, default_branch, fetch=None):
        return audit_classified(
            self.ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG,
            default_branch=default_branch, fetch=fetch,
        )

    @staticmethod
    def _cache003(report) -> list[Finding]:
        return [f for f in report.findings if f.rule == "CACHE-003"]

    @staticmethod
    def _repo_fetch(branch: str):
        def fetch(url: str, token: str):
            if url == f"https://api.github.com/repos/{REPO_SLUG}":
                return {"default_branch": branch}
            if "actions/runs" in url:  # RUST-004's probe, answered with "no runs"
                return {"workflow_runs": []}
            raise AssertionError(f"unexpected fetch {url}")

        return fetch

    def test_payload_env_resolves_master(self) -> None:
        self._payload("master")
        report = self._report(self._classified("refs/heads/master", "refs/heads/main"), default_branch=None)
        messages = [f.message for f in self._cache003(report)]
        self.assertFalse([m for m in messages if "was saved on ref 'refs/heads/master'" in m])
        self.assertTrue([m for m in messages if "was saved on ref 'refs/heads/main'" in m])

    def test_api_lookup_resolves_when_no_payload(self) -> None:
        os.environ.pop("GITHUB_EVENT_PATH", None)
        report = self._report(
            self._classified("refs/heads/master", "refs/heads/main"),
            default_branch=None, fetch=self._repo_fetch("master"),
        )
        messages = [f.message for f in self._cache003(report)]
        self.assertFalse([m for m in messages if "was saved on ref 'refs/heads/master'" in m])
        self.assertTrue([m for m in messages if "was saved on ref 'refs/heads/main'" in m])

    def test_unresolvable_fails_closed(self) -> None:
        os.environ.pop("GITHUB_EVENT_PATH", None)
        # Add an undeclared key: its CACHE-001 must still fire, proving the
        # rest of the audit runs while only the default-branch checks pause.
        classified = classify(
            self.ci,
            [
                CacheEntry(
                    id=910, ref="refs/heads/main", key="setup-uv-2-linux-x64-py3.11-abcd0910",
                    version="v", size_in_bytes=4096, created_at="2026-10-25T00:00:00Z",
                    last_accessed_at="2026-10-25T00:00:00Z",
                ),
                CacheEntry(
                    id=911, ref="refs/heads/main", key="undeclared-shape-x",
                    version="v", size_in_bytes=4096, created_at="2026-10-25T00:00:00Z",
                    last_accessed_at="2026-10-25T00:00:00Z",
                ),
            ],
        )
        report = self._report(classified, default_branch=None)
        per_entry = [f for f in self._cache003(report) if f.path != "cache:default-branch"]
        self.assertEqual([], per_entry, "never a guessed per-entry finding")
        notes = [f for f in self._cache003(report) if f.path == "cache:default-branch"]
        self.assertEqual(1, len(notes))
        self.assertEqual(Status.NEEDS_REVIEW, notes[0].status)
        self.assertTrue(any(f.rule == "CACHE-001" and f.cache_id == 911 for f in report.findings))

    def test_explicit_default_branch_wins_over_payload(self) -> None:
        self._payload("master")
        report = self._report(self._classified("refs/heads/master"), default_branch="main")
        messages = [f.message for f in self._cache003(report)]
        self.assertTrue([m for m in messages if "was saved on ref 'refs/heads/master'" in m])
        self.assertFalse([f for f in self._cache003(report) if f.path == "cache:default-branch"])

    def test_broken_payload_falls_through(self) -> None:
        os.environ["GITHUB_EVENT_PATH"] = "/nonexistent/event.json"
        self.assertIsNone(infer_default_branch(None, TOKEN, REPO_SLUG))
        self.assertEqual("master", infer_default_branch(self._repo_fetch("master"), TOKEN, REPO_SLUG))


class Cache006LruFamilyTest(unittest.TestCase):
    """zackees/ci.yml#421: an `evict = "lru"` family is bounded by its
    footprint, not by CACHE-006. Content-addressed keys (soldr verdicts)
    share a shape once the hash is stripped, but every one is live."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def _findings(self, evict: str | None) -> list[Finding]:
        fam = replace(self.ci.cache.family["soldr-mini"], evict=evict)
        ci = replace(self.ci, cache=replace(self.ci.cache, family={**self.ci.cache.family, "soldr-mini": fam}))
        classified = tuple(
            ClassifiedEntry(
                entry=CacheEntry(
                    id=i, ref=MAIN_REF, key=f"soldr-verdict-v1-dylint-ws-{h * 32}", version="v",
                    size_in_bytes=400, created_at="2026-10-24T00:00:00Z",
                    last_accessed_at=f"2026-10-24T00:00:0{i}Z",
                ),
                family_id="soldr-mini", is_delta=False, delta=None, is_retired=False,
            )
            for i, h in ((1, "a"), (2, "b"))
        )
        return _check_cache_006(ci, classified)

    def test_lru_family_keeps_every_same_shape_entry(self) -> None:
        self.assertEqual([], self._findings("lru"))

    def test_default_family_still_supersedes(self) -> None:
        self.assertEqual([1], [f.cache_id for f in self._findings(None)])


class SameLockGenerationTest(unittest.TestCase):
    """zackees/ci.yml#222: the zccache#1876 same-lock generation rule.

    A soldr release rewrites setup-soldr's toolchain digest without touching
    Cargo.lock, growing one lock into two full cache sets (zccache#1875:
    12.41 GB live) where every trailing-hash shape keeps the generations
    apart. A base entry created BEFORE the generation epoch (earliest
    default-branch `soldr-mini` save of the highest present soldr version,
    less 10 minutes of slack) is dead weight once a replacement of its
    restore family exists AFTER the epoch; post-epoch entries -- concurrent
    job shapes among them -- are never targets; without mini entries the
    whole check stays silent.
    """

    OLD = "2026-09-30T00:00:00Z"
    EPOCH_DAY = "2026-10-02T00:00:00Z"   # mini of the highest version
    NEW = "2026-10-03T00:00:00Z"

    MINI_OLD = "soldr-mini-v2-linux-x64-glibc-v0.9.25"
    MINI_NEW = "soldr-mini-v2-linux-x64-glibc-v0.9.27"

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def _entry(self, cid: int, key: str, created: str, ref: str = MAIN_REF) -> CacheEntry:
        return CacheEntry(
            id=cid, ref=ref, key=key, version="v", size_in_bytes=4096,
            created_at=created, last_accessed_at=created,
        )

    def _findings(self, *entries: CacheEntry, default_branch: str = "main") -> list:
        classified = classify(self.ci, list(entries))
        return _check_cache_006_same_lock(self.ci, classified, default_branch)

    def _with_epoch(self, *entries: CacheEntry) -> list:
        return self._findings(
            self._entry(900, self.MINI_OLD, self.OLD),
            self._entry(901, self.MINI_NEW, self.EPOCH_DAY),
            *entries,
        )

    def test_real_key_shapes_parse_into_their_families(self) -> None:
        parsed = {
            "setup-soldr-buildcache-v2-linux-x64-4f471a0fa0a2a00c-static-314e10a8550aaa80": "buildcache",
            "setup-soldr-cargoregistry-v1-linux-x64-deadbeefdeadbeef-cafebabecafebabe": "registry",
            "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-la87d2454829c34f1-soldrv0.9.25": "cook",
            "setup-soldr-dylint-output-v2-linux-x64-deadbeefcafebabe": "dylint",
        }
        for key, kind in parsed.items():
            result = _same_lock_family(key)
            assert result is not None, key
            self.assertEqual(kind, result.kind, key)
        # Unknown shapes and the epoch source itself are never targets.
        self.assertIsNone(_same_lock_family("totally-unknown-key"))
        self.assertIsNone(_same_lock_family(self.MINI_NEW))

    def test_buildcache_generation_is_flagged_only_with_a_post_epoch_replacement(self) -> None:
        old = "setup-soldr-buildcache-v2-linux-x64-4f471a0fa0a2a00c-static-314e10a8550aaa80"
        new = "setup-soldr-buildcache-v2-linux-x64-5820c6969e26aa8b-static-314e10a8550aaa80"  # same suffix+lock
        findings = self._with_epoch(self._entry(1, old, self.OLD), self._entry(2, new, self.NEW))
        self.assertEqual([1], [f.cache_id for f in findings])
        self.assertIn("same-lock buildcache generation", findings[0].message)
        self.assertEqual("CACHE-006", findings[0].rule)
        # Same family but ONLY pre-epoch entries: the producer has not
        # re-seeded, so nothing is dead weight yet.
        other = "setup-soldr-buildcache-v2-linux-x64-aaaaaaaaaaaaaaaa-static-55c1323f88b8e77c"
        self.assertEqual([], self._with_epoch(self._entry(3, other, self.OLD)))
        # A different suffix is a different restore family: its un-reseeded
        # members stay even when another family has a replacement.
        pair = "setup-soldr-buildcache-v2-linux-x64-bbbbbbbbbbbbbbbb-integration-55c1323f88b8e77c"
        findings = self._with_epoch(self._entry(4, old, self.OLD), self._entry(5, new, self.NEW),
                                    self._entry(6, pair, self.OLD))
        self.assertEqual([4], [f.cache_id for f in findings])

    def test_post_epoch_entries_are_never_targets(self) -> None:
        old = "setup-soldr-buildcache-v2-linux-x64-4f471a0fa0a2a00c-static-314e10a8550aaa80"
        newer = "setup-soldr-buildcache-v2-linux-x64-5820c6969e26aa8b-static-314e10a8550aaa80"
        # Both created after the epoch (concurrent job shapes): untouched.
        findings = self._with_epoch(self._entry(1, old, self.NEW), self._entry(2, newer, self.NEW))
        self.assertEqual([], findings)

    def test_registry_generation_groups_on_head_and_lock_not_the_tail(self) -> None:
        # #88 keeps the tail in the CACHE-006 shape (so the old rule never
        # grouped these); the same-lock rule drops exactly that tail.
        old = "setup-soldr-cargoregistry-v1-linux-x64-deadbeefdeadbeef-cafebabecafebabe"
        new = "setup-soldr-cargoregistry-v1-linux-x64-deadbeefdeadbeef-0123456789abcdef"
        findings = self._with_epoch(self._entry(1, old, self.OLD), self._entry(2, new, self.NEW))
        self.assertEqual([1], [f.cache_id for f in findings])
        self.assertIn("same-lock registry generation", findings[0].message)
        # A different lock is a different family: never grouped.
        other_lock = "setup-soldr-cargoregistry-v1-linux-x64-1111111111111111-cafebabecafebabe"
        self.assertEqual([], self._with_epoch(self._entry(3, old, self.OLD),
                                               self._entry(4, other_lock, self.NEW)))

    def test_cook_compares_explicit_versions_across_any_lock(self) -> None:
        old = "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-la87d2454829c34f1-soldrv0.9.25"
        new = "cook-base-v2-linux-x64-glibc-rustc1.95.0-fnone-l40d011b8bf414a69-soldrv0.9.26"  # any lock
        findings = self._with_epoch(self._entry(1, old, self.OLD), self._entry(2, new, self.NEW))
        self.assertEqual([1], [f.cache_id for f in findings])
        self.assertIn("same-lock cook generation", findings[0].message)
        # No strictly newer version after the epoch: kept (and a post-epoch
        # lower-version save -- the soldr-rollback shape -- is never a target).
        self.assertEqual([], self._with_epoch(self._entry(3, old, self.OLD)))
        self.assertEqual([], self._with_epoch(self._entry(4, old, self.NEW), self._entry(5, new, self.NEW)))

    def test_without_an_epoch_the_check_stays_silent(self) -> None:
        old = "setup-soldr-buildcache-v2-linux-x64-4f471a0fa0a2a00c-static-314e10a8550aaa80"
        new = "setup-soldr-buildcache-v2-linux-x64-5820c6969e26aa8b-static-314e10a8550aaa80"
        self.assertEqual([], self._findings(self._entry(1, old, self.OLD), self._entry(2, new, self.NEW)))

    def test_only_default_branch_base_entries_are_evaluated(self) -> None:
        old = "setup-soldr-buildcache-v2-linux-x64-4f471a0fa0a2a00c-static-314e10a8550aaa80"
        new = "setup-soldr-buildcache-v2-linux-x64-5820c6969e26aa8b-static-314e10a8550aaa80"
        base = (self._entry(900, self.MINI_OLD, self.OLD), self._entry(901, self.MINI_NEW, self.EPOCH_DAY))
        # PR-scoped and off-default-ref entries are never targets.
        self.assertEqual([], self._findings(
            *base, self._entry(1, old + "-pr-9", self.OLD), self._entry(2, old, self.OLD, ref="refs/heads/topic")))
        # ...and no epoch is found from PR-ref mini entries alone.
        self.assertEqual([], self._findings(
            self._entry(900, self.MINI_OLD, self.OLD, ref="refs/pull/1/merge"),
            self._entry(901, self.MINI_NEW, self.EPOCH_DAY, ref="refs/pull/1/merge"),
            self._entry(1, old, self.OLD), self._entry(2, new, self.NEW),
        ))

    def test_lru_families_stay_exempt_and_wired_into_the_full_audit(self) -> None:
        old = "setup-soldr-buildcache-v2-linux-x64-4f471a0fa0a2a00c-static-314e10a8550aaa80"
        new = "setup-soldr-buildcache-v2-linux-x64-5820c6969e26aa8b-static-314e10a8550aaa80"
        # evict = "lru" keeps the #421 all-live semantics.
        compile_family = replace(self.ci.cache.family["compile"], evict="lru")
        ci = replace(self.ci, cache=replace(self.ci.cache, family={**self.ci.cache.family, "compile": compile_family}))
        entries = [self._entry(900, self.MINI_OLD, self.OLD), self._entry(901, self.MINI_NEW, self.EPOCH_DAY),
                   self._entry(1, old, self.OLD), self._entry(2, new, self.NEW)]
        classified = classify(ci, entries)
        self.assertEqual([], _check_cache_006_same_lock(ci, classified, "main"))
        # ...and the rule reaches the full audit under a resolved default branch.
        report = audit_classified(ci, classified, graphql=None, token=TOKEN, repo=REPO_SLUG, default_branch="main")
        hits = [f for f in report.findings if f.rule == "CACHE-006" and "same-lock" in f.message]
        self.assertEqual([], hits)  # lru exempt end to end
        report = audit_classified(self.ci, classify(self.ci, entries), graphql=None, token=TOKEN,
                                  repo=REPO_SLUG, default_branch="main")
        hits = [f for f in report.findings if f.rule == "CACHE-006" and "same-lock" in f.message]
        self.assertEqual([1], [f.cache_id for f in hits])  # wired, not just callable
