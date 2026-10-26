"""`ci-lint cache trim|janitor|heal|preprune` -- ci_lint.cache.ops
(round-4A brief, deliverable 5). No network: fake fetch/delete/graphql
record calls instead of touching the API; every test asserts `--dry-run`
(or omitting the delete function) never calls delete.
"""

from __future__ import annotations

import json
import unittest

from ci_lint.cache.audit import ClassifiedEntry
from ci_lint.cache.github_cache import CacheEntry
from ci_lint.cache.ops import _lru_evictions, heal, janitor, preprune, trim
from ci_lint.github_api import GitHubApiError
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

CACHE_FIXTURES = FIXTURES / "runtime" / "cache"
REPO = CACHE_FIXTURES / "repo"
TOKEN = "t"
REPO_SLUG = "zackees/template-python-rust-cmd"


def _load(name: str) -> dict[str, object]:
    return json.loads((CACHE_FIXTURES / name).read_text(encoding="utf-8"))


def _fetch_from(payload: dict[str, object]):
    def fetch(url: str, token: str):
        assert token
        return payload

    return fetch


def _graphql_from(payload: dict[str, object]):
    def graphql(query: str, token: str):
        assert token
        return payload

    return graphql


def _recording_delete(calls: list[str]):
    def delete(url: str, token: str) -> None:
        assert token
        calls.append(url)

    return delete


def _failing_delete():
    def delete(url: str, token: str) -> None:
        raise GitHubApiError("boom")

    return delete


class TrimTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self.fetch = _fetch_from(_load("caches-synthetic-rules.json"))
        self.graphql = _graphql_from(_load("pr-states.json"))

    def test_dry_run_plans_both_cache_008_deltas_but_deletes_nothing(self) -> None:
        result = trim(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=None, token=TOKEN, repo=REPO_SLUG,
            dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertEqual((), result.deleted)
        planned_ids = {p.id for p in result.planned}
        self.assertEqual({9001, 9002}, planned_ids)

    def test_live_run_deletes_exactly_the_planned_ids(self) -> None:
        calls: list[str] = []
        result = trim(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=_recording_delete(calls),
            token=TOKEN, repo=REPO_SLUG, dry_run=False,
        )
        self.assertFalse(result.dry_run)
        self.assertEqual({9001, 9002}, {p.id for p in result.deleted})
        self.assertEqual(2, len(calls))
        self.assertTrue(all("actions/caches/900" in c for c in calls))

    def test_max_deletes_caps_the_plan(self) -> None:
        result = trim(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=None, token=TOKEN, repo=REPO_SLUG,
            dry_run=True, max_deletes=1,
        )
        self.assertEqual(1, len(result.planned))

    def test_never_deletes_a_non_cache_008_entry(self) -> None:
        result = trim(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=None, token=TOKEN, repo=REPO_SLUG,
            dry_run=True, max_deletes=100,
        )
        planned_ids = {p.id for p in result.planned}
        self.assertNotIn(9003, planned_ids)  # CACHE-009 (retired), not CACHE-008 -- trim's job, not janitor's
        self.assertNotIn(9004, planned_ids)  # CACHE-003
        self.assertNotIn(9005, planned_ids)  # CACHE-005

    def test_delete_error_is_recorded_not_raised(self) -> None:
        result = trim(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=_failing_delete(), token=TOKEN,
            repo=REPO_SLUG, dry_run=False,
        )
        self.assertEqual(2, len(result.errors))
        self.assertEqual((), result.deleted)


class JanitorTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        self.fetch = _fetch_from(_load("caches-synthetic-rules.json"))
        self.graphql = _graphql_from(_load("pr-states.json"))

    def test_deletes_cache_003_005_009_and_closed_pr_but_not_stale_base_delta(self) -> None:
        """#23 section 4.1: the janitor deletes every entry of a closed PR
        (9001, PR #7 CLOSED); an open PR's stale-base delta (9002) stays
        trim's job."""

        report = janitor(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=None, token=TOKEN, repo=REPO_SLUG,
            dry_run=True, stale_days=999999,  # disable the stale-entry sweep for this assertion
        )
        result, table = report.result, report.text
        planned_ids = {p.id for p in result.planned}
        self.assertEqual({9001, 9003, 9004, 9005}, planned_ids)
        self.assertIn("family", table)

    def test_stale_entries_are_swept_too(self) -> None:
        # every synthetic entry's last_accessed_at is 2026-09-2*; "now" far
        # in the future makes all of them stale.
        import datetime

        far_future = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
        report = janitor(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=None, token=TOKEN, repo=REPO_SLUG,
            dry_run=True, stale_days=5, now=far_future, max_deletes=100,
        )
        result, _table = report.result, report.text
        planned_ids = {p.id for p in result.planned}
        self.assertIn(9002, planned_ids)  # an open PR's delta, stale by access time
        # 8237210113 (the live "compile" base entry) is stale too, but it is the newest
        # entry a required job restores (#23 section 4.3) -- never deleted.
        self.assertNotIn(8237210113, planned_ids)

    def test_before_after_table_reflects_a_live_delete(self) -> None:
        calls: list[str] = []
        report = janitor(
            self.ci, fetch=self.fetch, graphql=self.graphql, delete=_recording_delete(calls), token=TOKEN,
            repo=REPO_SLUG, dry_run=False, stale_days=999999,
        )
        result, table = report.result, report.text
        self.assertEqual(4, len(result.deleted))
        self.assertIn("retired", table)  # id 9003's family label

    def test_cache_004_drift_surfaces_as_a_measured_warning_not_a_delete(self) -> None:
        """zackees/ci.yml#425 (Problem 2): a family whose live entries exceed
        its declared max must name the measured size in `result.warning` on
        every janitor run -- the eviction point where a stale max would
        otherwise be applied blind. The sweep itself must not delete on that
        basis (CACHE-004 is not in JANITOR_RULES; the fix is a corrected
        `max`, a pre-prune, or eviction after review, never a blind sweep)."""

        entry = {
            "id": 7777, "ref": "refs/heads/main",
            "key": f"setup-soldr-buildcache-v2-LinuxX64-{'d' * 16}",
            "version": "v", "size_in_bytes": 500_000_000,
            "created_at": "2026-10-20T00:00:00Z", "last_accessed_at": "2026-10-25T00:00:00Z",
        }
        fetch = _fetch_from({"total_count": 1, "actions_caches": [entry]})
        report = janitor(
            self.ci, fetch=fetch, graphql=self.graphql, delete=None, token=TOKEN, repo=REPO_SLUG,
            dry_run=True, stale_days=999999,
        )
        result, _table = report.result, report.text
        warning = result.warning or ""
        self.assertIn("live footprint 500000000B", warning)
        self.assertIn("exceed max", warning)
        self.assertIn("id=7777", warning)
        self.assertNotIn(7777, {p.id for p in result.planned}, "drift is reported, never swept")


def _entry(cid: int, key: str, ref: str, size: int, created: str, accessed: str) -> dict[str, object]:
    return {
        "id": cid, "ref": ref, "key": key, "version": "v", "size_in_bytes": size,
        "created_at": created, "last_accessed_at": accessed,
    }


MB = 1024 * 1024
NOW = 1790000000.0


def _iso(offset_seconds: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(NOW + offset_seconds, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class JanitorIssue23Test(unittest.TestCase):
    """zackees/ci.yml#23 section 4: closed-PR deletion by delimited pr-<N> key,
    fail-safe lookup, grace window, LRU convergence, newest-entry protection."""

    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci
        old = _iso(-3600)
        self.entries = [
            _entry(1, "setup-soldr-buildcache-v2-linux-x64-pr-7-aaaaaaaaaaaaaaaa", "refs/heads/feat", 5 * MB, old, old),
            _entry(2, "setup-soldr-buildcache-v2-linux-x64-pr-8-bbbbbbbbbbbbbbbb", "refs/heads/other", 5 * MB, old, old),
            # PR #7 is closed, but this one is inside the 10-minute grace window
            _entry(3, "setup-soldr-buildcache-v2-linux-x64-pr-7-young-cccccccccccccccc", "refs/heads/feat", 5 * MB,
                   _iso(-60), _iso(-60)),
        ]

    def _run(self, entries, graphql, ci=None):
        report = janitor(
            ci or self.ci, fetch=_fetch_from({"actions_caches": entries}), graphql=graphql, delete=None,
            token=TOKEN, repo=REPO_SLUG, dry_run=True, stale_days=999999, now=NOW,
        )
        result, _table = report.result, report.text
        return result

    def test_closed_pr_keyed_entries_deleted_open_and_young_kept(self) -> None:
        result = self._run(self.entries, _graphql_from(_load("pr-states.json")))
        self.assertEqual({1}, {p.id for p in result.planned})
        self.assertIn("closed PR", result.planned[0].reason)

    def test_same_key_on_two_refs_targets_the_closed_pr_entry_by_id(self) -> None:
        """ci.yml#137 (kernal-api #360): one key lives on refs/heads/main
        AND on a closed PR's merge ref. Only the PR-ref id is deleted -- the
        main entry with the identical key is never planned."""

        old = _iso(-3600)
        key = "setup-soldr-buildcache-v2-linux-x64-dddddddddddddddd"
        entries = [
            _entry(20, key, "refs/pull/7/merge", 5 * MB, old, old),
            _entry(21, key, "refs/heads/main", 5 * MB, old, old),
        ]
        graphql = _graphql_from(_load("pr-states.json"))
        result = self._run(entries, graphql)
        self.assertEqual({20}, {p.id for p in result.planned})
        trimmed = trim(
            self.ci, fetch=_fetch_from({"actions_caches": entries}), graphql=graphql, delete=None,
            token=TOKEN, repo=REPO_SLUG, dry_run=True,
        )
        self.assertNotIn(21, {p.id for p in trimmed.planned})

    def test_failed_pr_lookup_keeps_everything(self) -> None:
        def graphql(query: str, token: str):
            raise GitHubApiError("boom")

        result = self._run(self.entries, graphql)
        self.assertEqual((), result.planned)
        self.assertIn("fail safe", result.warning or "")

    def test_lru_family_converges_to_its_footprint(self) -> None:
        from dataclasses import replace

        fams = dict(self.ci.cache.family)
        fams["dylint"] = replace(fams["dylint"], evict="lru")  # max 80MB, per none -> 80MB footprint
        ci = replace(self.ci, cache=replace(self.ci.cache, family=fams))
        old = _iso(-7200)
        entries = [
            _entry(11, "setup-soldr-dylint-v2-1111111111111111", "refs/heads/main", 50 * MB, old, _iso(-100)),
            _entry(12, "setup-soldr-dylint-v2-2222222222222222-x", "refs/heads/main", 20 * MB, old, _iso(-200)),
            _entry(13, "setup-soldr-dylint-v2-3333333333333333-y", "refs/heads/main", 20 * MB, old, _iso(-300)),
        ]
        result = self._run(entries, None, ci)
        planned = {p.id: p.reason for p in result.planned}
        self.assertNotIn(11, planned)  # newest
        self.assertNotIn(12, planned)  # 70MB cumulative: fits
        self.assertIn(13, planned)  # 90MB cumulative: evicted, least recently used
        self.assertTrue(planned[13].startswith("lru"))

    def test_non_lru_family_keeps_newest_per_prefix(self) -> None:
        old = _iso(-7200)
        entries = [
            _entry(21, "setup-soldr-dylint-v2-1111111111111111", "refs/heads/main", 50 * MB, old, _iso(-100)),
            _entry(22, "setup-soldr-dylint-v2-2222222222222222", "refs/heads/main", 50 * MB, old, _iso(-200)),
        ]
        result = self._run(entries, None)
        self.assertEqual({22}, {p.id for p in result.planned})
        self.assertTrue(result.planned[0].reason.startswith("CACHE-006"))


class HealTest(unittest.TestCase):
    def test_dry_run_never_deletes(self) -> None:
        result = heal(delete=None, token=TOKEN, repo=REPO_SLUG, key="some-poisoned-key", dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual((), result.deleted)
        self.assertEqual(1, len(result.planned))

    def test_live_run_deletes_by_key_with_no_id_lookup(self) -> None:
        calls: list[str] = []
        result = heal(delete=_recording_delete(calls), token=TOKEN, repo=REPO_SLUG, key="some-poisoned-key")
        self.assertEqual(1, len(result.deleted))
        self.assertEqual(1, len(calls))
        self.assertIn("key=some-poisoned-key", calls[0])

    def test_delete_failure_is_recorded(self) -> None:
        result = heal(delete=_failing_delete(), token=TOKEN, repo=REPO_SLUG, key="k")
        self.assertEqual(1, len(result.errors))


class PrepruneTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def test_under_budget_without_lockfile_change_prunes_nothing(self) -> None:
        report = preprune(
            self.ci, fetch=_fetch_from(_load("caches-live-template.json")), graphql=None, delete=None,
            token=TOKEN, repo=REPO_SLUG, lockfile_changed=False, dry_run=True,
        )
        result, summary = report.result, report.text
        self.assertEqual((), result.planned)
        self.assertIn("under budget", summary)

    def test_over_budget_with_lockfile_change_prunes_the_superseded_lockfile_family_entry(self) -> None:
        from dataclasses import replace

        ci_tiny_budget = replace(self.ci, cache=replace(self.ci.cache, budget="1B"))
        report = preprune(
            ci_tiny_budget, fetch=_fetch_from(_load("caches-registry-per-job.json")), graphql=None, delete=None,
            token=TOKEN, repo=REPO_SLUG, lockfile_changed=True, dry_run=True,
        )
        result, summary = report.result, report.text
        self.assertIn("OVER budget", summary)
        # the superseded (CACHE-006) "registry" entry -- an older Cargo.lock
        # hash under the same toolchain digest (ci.yml#88) -- is lockfile-keyed
        # and about to be rewritten; the per-job digest siblings are kept.
        self.assertEqual(1, len(result.planned))
        self.assertEqual(4, result.planned[0].id)


if __name__ == "__main__":
    unittest.main()


class LruFootprintTest(unittest.TestCase):
    """The janitor's LRU budget and CACHE-004's arithmetic must be the
    SAME number.

    Found by an agent adopting the policy in zackees/reld: `shapes`
    (ci.yml#334) makes CACHE-004 sum the shape maxima, but `_lru_evictions`
    still computed `max x cardinality(per)` and ignored `shapes` entirely.
    When those diverge the janitor is the stricter of the two, so it evicts
    live entries precheck had just approved -- the one failure mode this
    whole policy exists to avoid.
    """

    def _ci(self, fam: object):
        from dataclasses import replace as _replace

        ci = load_ci_toml(REPO).ci
        return _replace(ci, cache=_replace(ci.cache, family={"compile": fam}))

    def _entry(self, eid: int, used: str, mb: int) -> object:
        from ci_lint.cache.audit import ClassifiedEntry

        return ClassifiedEntry(
            entry=CacheEntry(
                id=eid, ref="refs/heads/main", key=f"k{eid}", version="v",
                size_in_bytes=mb * 1024**2,
                created_at="2026-10-01T00:00:00Z", last_accessed_at=used,
            ),
            family_id="compile", is_delta=False, delta=None,
            is_retired=False, pr=None, pr_in_key=False,
        )

    def _family(self, shapes: tuple) -> object:
        from ci_lint.schema import CacheFamily

        return CacheFamily(
            id="compile", via="setup-soldr:build-cache", max="1300MB",
            per="none", evict="lru", shapes=shapes,
        )

    def test_a_shapes_family_is_evicted_against_its_shape_sum(self) -> None:
        from ci_lint.schema import CacheShape

        # shapes sum to 2900 MB, so 2400 MB of live entries FITS. The old
        # `max x cardinality(per)` was 1300 x 1 = 1300 MB and would have
        # evicted both.
        ci = self._ci(self._family((CacheShape("a", "1300MB"),
                                    CacheShape("b", "1100MB"),
                                    CacheShape("c", "500MB"))))
        entries = [self._entry(1, "2026-10-04T00:00:00Z", 1200),
                   self._entry(2, "2026-10-03T00:00:00Z", 1200)]
        self.assertEqual(_lru_evictions(ci, entries), ())

    def test_an_over_budget_shapes_family_still_evicts(self) -> None:
        from ci_lint.schema import CacheShape

        ci = self._ci(self._family((CacheShape("a", "1300MB"),)))
        entries = [self._entry(1, "2026-10-04T00:00:00Z", 1200),
                   self._entry(2, "2026-10-03T00:00:00Z", 1200)]
        self.assertEqual({e.cache_id for e in _lru_evictions(ci, entries)}, {2})


class JanitorRepoBudgetTest(unittest.TestCase):
    """zackees/ci.yml#405: the end-of-queue janitor keeps the repository
    total under [cache].budget by LRU itself, so GitHub's eviction never
    fires; protected and in-grace entries are never evicted for budget."""

    def setUp(self) -> None:
        from dataclasses import replace

        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = replace(ci, cache=replace(ci.cache, budget="100MB"))

    def _run(self, entries, **kwargs):
        report = janitor(
            self.ci, fetch=_fetch_from({"actions_caches": entries}), graphql=None, delete=None,
            token=TOKEN, repo=REPO_SLUG, dry_run=True, stale_days=999999, now=NOW, **kwargs,
        )
        result, _table = report.result, report.text
        return result

    def test_over_budget_evicts_least_recently_accessed_first(self) -> None:
        old = _iso(-7200)
        entries = [
            _entry(31, "setup-soldr-dylint-v2-1111111111111111-k31", "refs/heads/main", 60 * MB, old, _iso(-100)),
            _entry(32, "setup-soldr-dylint-v2-2222222222222222-k32", "refs/heads/main", 30 * MB, old, _iso(-200)),
            _entry(33, "setup-soldr-dylint-v2-3333333333333333-k33", "refs/heads/main", 30 * MB, old, _iso(-300)),
            _entry(34, "setup-soldr-dylint-v2-4444444444444444-k34", "refs/heads/main", 30 * MB, old, _iso(-400)),
        ]
        planned = {p.id: p.reason for p in self._run(entries).planned}
        # 150MB live, 100MB budget: the two least recently accessed go.
        self.assertEqual({33, 34}, set(planned))
        self.assertTrue(all(r.startswith("budget") for r in planned.values()))

    def test_budget_pass_respects_protection_and_grace(self) -> None:
        old = _iso(-7200)
        entries = [
            _entry(41, "setup-soldr-dylint-v2-1111111111111111-k41", "refs/heads/main", 90 * MB, old, _iso(-10)),
            _entry(42, "setup-soldr-dylint-v2-2222222222222222-k42", "refs/heads/main", 30 * MB, _iso(-60), _iso(-60)),
        ]
        result = self._run(entries)
        # 41 is its family's newest; 42 is inside the grace window.
        self.assertEqual((), result.planned)
        self.assertIn("over [cache].budget", result.warning or "")

    def test_budget_order_survives_max_deletes(self) -> None:
        old = _iso(-7200)
        entries = [
            _entry(51, "setup-soldr-dylint-v2-1111111111111111-k51", "refs/heads/main", 10 * MB, old, _iso(-100)),
            _entry(52, "setup-soldr-dylint-v2-2222222222222222-k52", "refs/heads/main", 80 * MB, old, _iso(-200)),
            _entry(53, "setup-soldr-dylint-v2-3333333333333333-k53", "refs/heads/main", 20 * MB, old, _iso(-900)),
            _entry(54, "setup-soldr-dylint-v2-4444444444444444-k54", "refs/heads/main", 20 * MB, old, _iso(-800)),
        ]
        result = self._run(entries, max_deletes=1)
        self.assertEqual([53], [p.id for p in result.planned])  # oldest access, not largest

    def test_enforce_budget_off_keeps_previous_behaviour(self) -> None:
        old = _iso(-7200)
        entries = [
            _entry(61, "setup-soldr-dylint-v2-1111111111111111-k61", "refs/heads/main", 90 * MB, old, _iso(-100)),
            _entry(62, "setup-soldr-dylint-v2-2222222222222222-k62", "refs/heads/main", 90 * MB, old, _iso(-200)),
        ]
        self.assertEqual((), self._run(entries, enforce_budget=False).planned)
