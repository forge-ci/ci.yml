"""`ci-lint cache budget` (ci_lint.cache.budget) and the delimited `pr-<N>`
classifier (ci_lint.cache.audit.pr_from_key) -- zackees/ci.yml#23 sections 4-6.
No network: a fake fetch returns a synthetic cache listing."""

from __future__ import annotations

import unittest
from dataclasses import replace

from ci_lint.cache.audit import classify, pr_from_key, pr_from_ref
from ci_lint.cache.budget import is_hard_context, run_budget
from ci_lint.cache.github_cache import CacheEntry
from ci_lint.github_api import GitHubApiError
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

REPO = FIXTURES / "runtime" / "cache" / "repo"
MB = 1024 * 1024


def _fetch(entries: list[tuple[int, str, str, int]]):
    payload = {
        "actions_caches": [
            {"id": i, "key": k, "ref": r, "version": "v", "size_in_bytes": n, "created_at": "", "last_accessed_at": ""}
            for i, k, r, n in entries
        ]
    }

    def fetch(url: str, token: str):
        return payload

    return fetch


class PrComponentTest(unittest.TestCase):
    def test_delimited_pr_component(self) -> None:
        self.assertEqual(12, pr_from_key("setup-soldr-buildcache-v2-linux-x64-pr-12-abcdef"))
        self.assertEqual(12, pr_from_key("pr-12"))
        self.assertEqual(12, pr_from_key("uv-v1-linux_pr-12"))
        self.assertIsNone(pr_from_key("spr-12-x"))  # not delimited
        self.assertIsNone(pr_from_key("x-pr-12a"))  # not delimited
        self.assertIsNone(pr_from_key("setup-soldr-buildcache-v2-linux-x64-abcdef"))

    def test_legacy_delta_form_still_recognized(self) -> None:
        self.assertEqual(7, pr_from_key("delta-v1-pr7-compile-linux-x64-b43cef837"))

    def test_merge_ref(self) -> None:
        self.assertEqual(5, pr_from_ref("refs/pull/5/merge"))
        self.assertIsNone(pr_from_ref("refs/heads/main"))

    def test_pr_keyed_entry_is_not_a_cache_003_base_layer(self) -> None:
        ci = load_ci_toml(REPO).ci
        assert ci is not None
        entry = CacheEntry(
            id=1, ref="refs/heads/feature", key="setup-soldr-buildcache-v2-linux-x64-pr-3-abcdef12",
            version="v", size_in_bytes=5 * MB, created_at="", last_accessed_at="",
        )
        (c,) = classify(ci, [entry])
        self.assertEqual(("compile", 3, True), (c.family_id, c.pr, c.pr_in_key))


class BudgetVerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        ci = load_ci_toml(REPO).ci
        assert ci is not None
        # 100MB account budget, 30MB PR budget
        self.ci = replace(ci, cache=replace(ci.cache, budget="100MB", pr=replace(ci.cache.pr, budget="30MB")))

    def _run(self, entries, *, event_name: str, ref: str, pr: int | None):
        return run_budget(
            self.ci, fetch=_fetch(entries), token="t", repo="o/r", event_name=event_name, ref=ref, pr_number=pr
        )

    def test_hard_context(self) -> None:
        self.assertTrue(is_hard_context("push", "refs/heads/main", "main"))
        self.assertTrue(is_hard_context("schedule", "refs/heads/main", "main"))
        self.assertFalse(is_hard_context("push", "refs/heads/feature", "main"))
        self.assertFalse(is_hard_context("pull_request", "refs/pull/3/merge", "main"))

    def test_over_budget_fails_on_main(self) -> None:
        v = self._run([(1, "setup-soldr-buildcache-v2-a", "refs/heads/main", 120 * MB)],
                      event_name="push", ref="refs/heads/main", pr=None)
        self.assertEqual("fail", v.verdict)

    def test_over_budget_not_caused_by_pr_is_warn_only(self) -> None:
        v = self._run(
            [(1, "setup-soldr-buildcache-v2-a", "refs/heads/main", 120 * MB),
             (2, "setup-soldr-buildcache-v2-pr-3-b", "refs/heads/f", 5 * MB)],
            event_name="pull_request", ref="refs/pull/3/merge", pr=3,
        )
        self.assertEqual("warn", v.verdict)
        self.assertFalse(v.failed)

    def test_pr_own_entries_over_pr_budget_fail(self) -> None:
        v = self._run(
            [(2, "setup-soldr-buildcache-v2-pr-3-b", "refs/heads/f", 40 * MB),
             (3, "setup-soldr-buildcache-v2-pr-4-c", "refs/heads/g", 1 * MB)],
            event_name="pull_request", ref="refs/pull/3/merge", pr=3,
        )
        self.assertEqual("fail", v.verdict)
        self.assertEqual(40 * MB, v.own_bytes)
        self.assertIn("setup-soldr-buildcache-v2-pr-3-b", v.reason)  # names the culprit
        self.assertNotIn("pr-4-c", v.reason)

    def test_pr_own_additions_cause_the_breach_fail(self) -> None:
        v = self._run(
            [(1, "setup-soldr-buildcache-v2-a", "refs/heads/main", 90 * MB),
             (2, "x", "refs/pull/3/merge", 20 * MB)],
            event_name="pull_request", ref="refs/pull/3/merge", pr=3,
        )
        self.assertEqual("fail", v.verdict)

    def test_non_main_push_is_warn_only(self) -> None:
        v = self._run([(1, "setup-soldr-buildcache-v2-a", "refs/heads/main", 120 * MB)],
                      event_name="push", ref="refs/heads/feature", pr=None)
        self.assertEqual("warn", v.verdict)

    def test_listing_failure_is_warn(self) -> None:
        def fetch(url: str, token: str):
            raise GitHubApiError("boom")

        v = run_budget(self.ci, fetch=fetch, token="t", repo="o/r", event_name="push", ref="refs/heads/main",
                       pr_number=None)
        self.assertEqual("warn", v.verdict)


class Gen009BudgetRollupTest(unittest.TestCase):
    """GEN-009 (issue #5, M2-23): `cache budget`'s cheap same-call rollup
    over CACHE-003 -- no extra live call, so CACHE-008 (needs GraphQL) is
    out of scope here; `cache audit` is the full rollup."""

    def _ci(self):
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        return ci

    def test_red_base_layer_on_pr_ref_sets_gen_009_reason(self) -> None:
        entries = [
            (1, "setup-soldr-cargoregistry-v1-abc123", "refs/pull/9/merge", 5 * MB),
        ]
        v = run_budget(
            self._ci(), fetch=_fetch(entries), token="t", repo="o/r",
            event_name="pull_request", ref="refs/pull/9/merge", pr_number=None,
        )
        self.assertIsNotNone(v.gen_009_reason)
        self.assertIn("CACHE-003", v.gen_009_reason)

    def test_green_default_branch_entry_has_no_gen_009_reason(self) -> None:
        entries = [
            (1, "setup-soldr-cargoregistry-v1-abc123", "refs/heads/main", 5 * MB),
        ]
        v = run_budget(
            self._ci(), fetch=_fetch(entries), token="t", repo="o/r",
            event_name="push", ref="refs/heads/main", pr_number=None,
        )
        self.assertIsNone(v.gen_009_reason)


class EvictSchemaTest(unittest.TestCase):
    """`[cache.family.<id>].evict`: optional, only "lru" (else CT-002)."""

    def _load(self, replacement: str):
        import shutil
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "repo"
            shutil.copytree(REPO, dst)
            toml = dst / "ci.toml"
            text = toml.read_text(encoding="utf-8")
            old = 'dylint     = { via = "setup-soldr:dylint",         max = "80MB",  min = "1MB" }'
            self.assertIn(old, text)
            toml.write_text(text.replace(old, replacement), encoding="utf-8")
            return load_ci_toml(dst)

    def test_lru_accepted(self) -> None:
        _loaded = self._load('dylint = { via = "setup-soldr:dylint", max = "80MB", evict = "lru" }')
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.assertEqual("lru", ci.cache.family["dylint"].evict)

    def test_unknown_value_is_ct_002(self) -> None:
        _loaded = self._load('dylint = { via = "setup-soldr:dylint", max = "80MB", evict = "fifo" }')
        _ci = _loaded.ci
        findings = _loaded.findings
        self.assertTrue(any(f.rule == "CT-002" and "evict" in f.message for f in findings))


if __name__ == "__main__":
    unittest.main()
