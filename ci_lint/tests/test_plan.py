"""The planner (section D of the round-1A brief): untagged PR; [ci-windows];
[ci-windows][ci-test-integration]; [ci-full]; [no-test] (mergeable false);
[release][no-test] (error); push main (no tag reading, cache write);
workflow_dispatch release; unknown [ci-foo]."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.plan import PlanError, compute_plan
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture

SHA = "c" * 40


def load_ci():
    _loaded = load_ci_toml(fixture("TAG-001", "green"))
    ci = _loaded.ci
    findings = _loaded.findings
    assert ci is not None, findings
    return ci


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ci = load_ci()

    def test_untagged_pr(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="fix a bug")
        self.assertEqual("pr", plan.flow)
        self.assertEqual((), plan.tags)
        self.assertEqual({"linux-x64"}, {p.id for p in plan.platforms})
        self.assertEqual({"unit", "smoke"}, set(plan.suites))
        self.assertTrue(plan.mergeable)
        self.assertEqual("read", plan.cache_mode)
        self.assertEqual("none", plan.publish)

    def test_ci_windows_adds_platform_group(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] fix")
        self.assertEqual({"linux-x64", "windows-x64"}, {p.id for p in plan.platforms})
        self.assertEqual({"unit", "smoke"}, set(plan.suites))
        self.assertTrue(plan.mergeable)

    def test_ci_windows_and_ci_test_integration(self) -> None:
        plan = compute_plan(
            self.ci, event_name="pull_request", title="[ci-windows][ci-test-integration] fix"
        )
        self.assertEqual({"linux-x64", "windows-x64"}, {p.id for p in plan.platforms})
        self.assertEqual({"unit", "smoke", "integration"}, set(plan.suites))

    def test_ci_full(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-full] fix")
        self.assertEqual({"linux-x64", "windows-x64"}, {p.id for p in plan.platforms})
        self.assertEqual({"unit", "smoke", "integration"}, set(plan.suites))

    def test_no_test_removes_all_suites_and_is_not_mergeable(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[no-test] wip")
        self.assertEqual(set(), set(plan.suites))
        self.assertFalse(plan.mergeable)

    def test_ci_perf_no_test_keeps_perf_suite(self) -> None:
        # ci.yml#46: the perf loop must stay build+perf only -- [no-test]
        # removes test-kind suites (unit/smoke/...) but never a
        # `kind = "bench"` suite like perf, regardless of bracket order.
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-perf][no-test] wip")
        self.assertEqual({"perf"}, set(plan.suites))
        self.assertFalse(plan.mergeable)

    def test_no_test_ci_perf_order_independent(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[no-test][ci-perf] wip")
        self.assertEqual({"perf"}, set(plan.suites))
        self.assertFalse(plan.mergeable)

    def test_release_with_no_test_is_an_error(self) -> None:
        with self.assertRaises(PlanError):
            compute_plan(self.ci, event_name="pull_request", title="[release][no-test] wip")

    def test_release_with_no_test_suffix_is_also_an_error(self) -> None:
        with self.assertRaises(PlanError):
            compute_plan(
                self.ci, event_name="pull_request", title="[release][no-test-integration] wip"
            )

    def test_push_main_ignores_tags_and_writes_cache(self) -> None:
        plan = compute_plan(self.ci, event_name="push", title="[ci-windows] should be ignored")
        self.assertEqual("main", plan.flow)
        self.assertEqual((), plan.tags)
        self.assertEqual({"linux-x64"}, {p.id for p in plan.platforms})
        self.assertEqual("write", plan.cache_mode)
        self.assertTrue(plan.mergeable)

    def test_workflow_dispatch_release_requires_sha(self) -> None:
        with self.assertRaises(PlanError):
            compute_plan(self.ci, event_name="workflow_dispatch", title="")

        plan = compute_plan(
            self.ci, event_name="workflow_dispatch", title="", dispatch_sha=SHA
        )
        self.assertEqual("release", plan.flow)
        self.assertEqual(SHA, plan.dispatch_sha)
        self.assertEqual({"linux-x64", "windows-x64"}, {p.id for p in plan.platforms})
        self.assertEqual({"unit", "smoke", "integration", "perf"}, set(plan.suites))
        self.assertEqual("mock", plan.publish)  # flow.release.publish = "pypi" -> publish.pypi.mode

    def test_unknown_reserved_tag_is_ignored_by_the_planner(self) -> None:
        base = compute_plan(self.ci, event_name="pull_request", title="untagged")
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-foo] weird")
        self.assertEqual(base.platforms, plan.platforms)
        self.assertEqual(base.suites, plan.suites)

    def test_schedule_selects_nightly(self) -> None:
        plan = compute_plan(self.ci, event_name="schedule", title="")
        self.assertEqual("nightly", plan.flow)
        self.assertEqual("write", plan.cache_mode)
        self.assertEqual("none", plan.publish)  # nightly overrides release's publish

    def test_release_tag_switches_flow_and_publish_is_rehearsal(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[release] ship it")
        self.assertEqual("release", plan.flow)
        self.assertEqual("rehearsal", plan.publish)

    def test_digest_is_stable_for_the_same_selection(self) -> None:
        p1 = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] x")
        p2 = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] y")
        self.assertEqual(p1.digest, p2.digest)
        p3 = compute_plan(self.ci, event_name="pull_request", title="untagged")
        self.assertNotEqual(p1.digest, p3.digest)


class RequiredJobsAndPlatformLanesTest(unittest.TestCase):
    """Round-2A amendments 1 and 3: the gate job-id convention and the
    needs_platform_lanes definition (true exactly when the plan selects
    any platform other than [flow.pr]'s own default fast-lane platforms,
    not merely "more than one platform")."""

    def setUp(self) -> None:
        self.ci = load_ci()

    def test_untagged_pr_is_the_fast_lane_only(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="fix a bug")
        self.assertFalse(plan.needs_platform_lanes)
        self.assertEqual(("precheck", "fast", "dylint"), plan.required_jobs)
        self.assertNotIn("ci-ok", plan.required_jobs)
        self.assertFalse(any(j.startswith("build-") for j in plan.required_jobs))

    def test_ci_windows_tag_needs_platform_lanes_and_matrix_jobs(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] fix")
        self.assertTrue(plan.needs_platform_lanes)
        self.assertEqual(
            ("precheck", "fast", "dylint", "platform-build", "platform-run"), plan.required_jobs
        )

    def test_release_needs_platform_lanes_even_though_nothing_was_added_by_a_tag(self) -> None:
        # flow.release's own base is platforms = "all" (both linux-x64 and
        # windows-x64 in this fixture) -- no tag added anything, but the
        # selection still goes beyond [flow.pr]'s single-platform default,
        # so the platform-build/platform-run matrix is still required.
        plan = compute_plan(self.ci, event_name="pull_request", title="[release] ship it")
        self.assertEqual({"linux-x64", "windows-x64"}, {p.id for p in plan.platforms})
        self.assertTrue(plan.needs_platform_lanes)
        self.assertIn("platform-build", plan.required_jobs)
        self.assertIn("platform-run", plan.required_jobs)

    def test_nightly_needs_platform_lanes(self) -> None:
        plan = compute_plan(self.ci, event_name="schedule", title="")
        self.assertEqual({"linux-x64", "windows-x64"}, {p.id for p in plan.platforms})
        self.assertTrue(plan.needs_platform_lanes)
        self.assertIn("platform-build", plan.required_jobs)
        self.assertIn("platform-run", plan.required_jobs)

    def test_push_main_matches_the_pr_default_fast_lane(self) -> None:
        # flow.main extends pr, so it inherits the same single-platform
        # base -- no platform lanes needed on a plain main push.
        plan = compute_plan(self.ci, event_name="push", title="")
        self.assertEqual({"linux-x64"}, {p.id for p in plan.platforms})
        self.assertFalse(plan.needs_platform_lanes)
        self.assertEqual(("precheck", "fast", "dylint"), plan.required_jobs)


class PlatformLanesFastSuitesLaneDigestsTest(unittest.TestCase):
    """Round-3A brief, Part 1: platform_lanes_json, fast_suites_json,
    lane_digests_json."""

    def setUp(self) -> None:
        self.ci = load_ci()

    def test_untagged_pr_has_no_platform_lanes(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="fix a bug")
        self.assertEqual((), plan.platform_lanes)
        self.assertEqual(set(plan.suites), set(plan.fast_suites))
        self.assertEqual({"fast", "dylint"}, set(plan.lane_digests))

    def test_ci_windows_produces_one_platform_lane(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] fix")
        self.assertEqual(1, len(plan.platform_lanes))
        lane = plan.platform_lanes[0]
        self.assertEqual("windows-x64", lane.id)
        self.assertEqual("x86_64-pc-windows-msvc", lane.target)
        self.assertEqual("windows-2025", lane.runs_on)
        self.assertEqual("windows", lane.group)
        self.assertIsNone(lane.wheel)  # this fixture's windows-x64 declares no `wheel`
        self.assertEqual(set(plan.suites), set(lane.suites))
        self.assertEqual({"fast", "dylint", "platform:windows-x64"}, set(plan.lane_digests))

    def test_platform_lane_wheel_is_carried_when_declared(self) -> None:
        _loaded = load_ci_toml(fixture("_e2e", "green"))
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        plan = compute_plan(ci, event_name="pull_request", title="[ci-windows] fix")
        lane = next(pl for pl in plan.platform_lanes if pl.id == "windows-x64")
        self.assertIsNone(lane.wheel)  # _e2e/green also declares no wheel for windows-x64

    def test_lane_digests_are_12_hex_chars(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] fix")
        for lane_id, digest in plan.lane_digests.items():
            self.assertRegex(digest, r"^[0-9a-f]{12}$", msg=f"lane {lane_id!r}: {digest!r}")

    def test_lane_digest_is_stable_across_calls(self) -> None:
        p1 = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] add feature x")
        p2 = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] add feature y")
        self.assertEqual(p1.lane_digests, p2.lane_digests)

    def test_lane_digest_changes_when_suites_change(self) -> None:
        base = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] x")
        full = compute_plan(self.ci, event_name="pull_request", title="[ci-windows][ci-test-integration] x")
        self.assertNotEqual(base.lane_digests["fast"], full.lane_digests["fast"])
        self.assertNotEqual(
            base.lane_digests["platform:windows-x64"], full.lane_digests["platform:windows-x64"]
        )
        # dylint's digest only depends on the target set, which tags never
        # change (issue #6 Section 2/3: Dylint covers every declared
        # platform regardless of what this run builds) -- unaffected here.
        self.assertEqual(base.lane_digests["dylint"], full.lane_digests["dylint"])

    def test_repo_root_lockfile_hash_changes_the_digest(self) -> None:
        without_repo = compute_plan(self.ci, event_name="pull_request", title="x")
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
            with_repo = compute_plan(
                self.ci, event_name="pull_request", title="x", repo_root=repo_root
            )
            (repo_root / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
            with_repo_changed = compute_plan(
                self.ci, event_name="pull_request", title="x", repo_root=repo_root
            )
        self.assertNotEqual(without_repo.lane_digests["fast"], with_repo.lane_digests["fast"])
        self.assertNotEqual(with_repo.lane_digests["fast"], with_repo_changed.lane_digests["fast"])

    def test_no_platform_lanes_means_empty_platform_lane_digests(self) -> None:
        plan = compute_plan(self.ci, event_name="push", title="")
        self.assertFalse(plan.needs_platform_lanes)
        self.assertEqual((), plan.platform_lanes)
        self.assertEqual(set(), {k for k in plan.lane_digests if k.startswith("platform:")})

    def test_to_json_dict_carries_the_new_fields(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="[ci-windows] x")
        payload = plan.to_json_dict()
        self.assertIn("platform_lanes", payload)
        self.assertIn("fast_suites", payload)
        self.assertIn("lane_digests", payload)
        lanes = payload["platform_lanes"]
        assert isinstance(lanes, list)
        self.assertEqual(1, len(lanes))
        self.assertEqual({"id", "target", "runs_on", "group", "wheel", "suites"}, set(lanes[0]))


if __name__ == "__main__":
    unittest.main()


class RequiredJobsForSuitesAndPublishTest(unittest.TestCase):
    """Round-4 follow-up: the gate must also require the from-zero `init`
    job, the `perf` job, and the release jobs when the plan selects them."""

    def test_suite_and_publish_jobs(self) -> None:
        from ci_lint.plan import _required_job_ids

        base = ("precheck", "fast", "dylint")
        self.assertEqual(_required_job_ids(False, ("t",), ("smoke", "unit"), "none"), base)
        self.assertEqual(
            _required_job_ids(False, ("t",), ("init", "smoke", "unit"), "none"), base + ("init",)
        )
        self.assertEqual(
            _required_job_ids(False, ("t",), ("perf", "unit"), "none"), base + ("perf",)
        )
        self.assertEqual(
            _required_job_ids(True, ("t",), ("unit",), "rehearsal"),
            base + ("platform-build", "platform-run", "release-verify"),
        )
        self.assertEqual(
            _required_job_ids(True, ("t",), ("unit",), "mock"),
            base + ("platform-build", "platform-run", "release-verify", "publish"),
        )
