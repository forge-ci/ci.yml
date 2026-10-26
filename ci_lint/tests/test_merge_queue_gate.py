"""#419 clause 5 -- `CI OK` credits a merge_group skip only on its own re-derivation.

Replay-style: the aggregator re-runs `ci_lint.merge_queue_trust` GET-only
against recorded API fixtures and the throwaway GATE-008 repository; the
verify job's `skip_<job>` outputs only narrow the decision, never make it.
"""

from __future__ import annotations

import copy
import unittest

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Status
from ci_lint.hosted_attestations import verified_merge_group_jobs
from ci_lint.runtime.gate import compute_gate, render_text, to_json_dict
from ci_lint.tests.helpers import requires_yaml_tooling
from ci_lint.tests.test_gate_trust import TrustCase, _git
from ci_lint.tests.test_merge_queue_trust import API, Replay

PLAN: dict[str, JsonValue] = {"required_jobs": ["lint"]}


@requires_yaml_tooling
class MergeQueueGateTest(TrustCase):
    def setUp(self) -> None:
        super().setUp()
        self.head = self.commit("feature", {"src.txt": "two\n"})
        self.tree = _git(self.repo, "rev-parse", f"{self.head}^{{tree}}")
        self.queue = _git(self.repo, "commit-tree", self.tree, "-p", self.base, "-m", "Merge pull request #7")

    def payload(self, base: str | None = None) -> dict[str, JsonValue]:
        base = base or self.base
        return {"repository": {"full_name": "o/r"},
                "merge_group": {"head_ref": f"refs/heads/gh-readonly-queue/main/pr-7-{base}",
                                "head_sha": self.queue, "base_sha": base}}

    def needs(self) -> dict[str, JsonValue]:
        return {"verify": {"result": "success", "outputs": {"skip_lint": "true"}}, "lint": {"result": "skipped"}}

    def fixtures(self, *, queue_tree: str | None = None, compare: str = "identical",
                 parents: list[str] | None = None) -> dict[str, tuple[int, JsonValue]]:
        return {
            f"{API}/git/commits/{self.queue}": (200, {"tree": {"sha": queue_tree or self.tree},
                                                      "parents": [{"sha": p} for p in (parents or [self.base])]}),
            f"{API}/compare/main...{self.base}": (200, {"status": compare}),
            f"{API}/pulls/7": (200, {"state": "open", "author_association": "OWNER", "labels": [],
                                     "head": {"sha": self.head, "repo": {"full_name": "o/r"}},
                                     "base": {"ref": "main", "repo": {"full_name": "o/r"}}}),
            f"{API}/git/commits/{self.head}": (200, {"tree": {"sha": self.tree}}),
        }

    def gate(self, *, event: str = "merge_group", payload: dict[str, JsonValue] | None = None,
             needs: dict[str, JsonValue] | None = None, fetch: Replay | None = None, token: str = "t"):  # type: ignore[no-untyped-def]
        needs = needs if needs is not None else self.needs()
        queue = verified_merge_group_jobs(
            self.repo, "ci.yml", payload if payload is not None else self.payload(), event=event, needs=needs,
            fetch=fetch if fetch is not None else Replay(self.fixtures()), token=token)
        return queue, compute_gate(PLAN, needs, queue_reuse=queue)

    def test_rederived_single_pr_attested_entry_is_accepted_with_provenance(self) -> None:
        queue, report = self.gate()
        self.assertTrue(report.ok, report.findings)
        status = report.statuses[0]
        self.assertEqual((status.queue_reused_from_pr, status.queue_tree), (7, self.tree))
        self.assertIn(f"reused from PR #7 (attestation) tree {self.tree}", render_text(report))
        self.assertEqual(to_json_dict(report)["statuses"][0]["queue_reused_from_pr"], 7)

    def assertTest001Failure(self, report) -> None:  # type: ignore[no-untyped-def]
        self.assertFalse(report.ok)
        self.assertEqual([(f.rule, f.status) for f in report.findings], [("TEST-001", Status.VIOLATION)])

    def test_each_refused_decision_is_the_test_001_failure(self) -> None:
        other = "b" * 40
        cases = {
            "multi-pr-group (ref base)": {"payload": self.payload(other)},
            "multi-pr-group (base not on main)": {"fetch": Replay(self.fixtures(compare="ahead"))},
            "multi-pr-group (two parents)": {"fetch": Replay(self.fixtures(parents=[self.base, other]))},
            "tree-mismatch": {"fetch": Replay(self.fixtures(queue_tree="c" * 40))},
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                queue, report = self.gate(**kwargs)
                self.assertEqual(queue.jobs if queue else None, ())
                self.assertTest001Failure(report)

    def test_unattested_pr_head_is_refused(self) -> None:
        self.head = self.commit("unattested", {"src.txt": "three\n"}, lanes=None)
        self.tree = _git(self.repo, "rev-parse", f"{self.head}^{{tree}}")
        self.queue = _git(self.repo, "commit-tree", self.tree, "-p", self.base, "-m", "q")
        self.assertTest001Failure(self.gate()[1])

    def test_verifier_outputs_never_replace_the_rederivation(self) -> None:
        for alteration in ('needs["verify"]["result"] = "failure"', 'needs.pop("verify")',
                           'needs["verify"]["outputs"]["skip_lint"] = "false"'):
            with self.subTest(alteration=alteration):
                needs = copy.deepcopy(self.needs())
                exec(alteration)
                self.assertTest001Failure(self.gate(needs=needs)[1])
        # A claimed skip with no matching re-derived trust still fails.
        self.assertTest001Failure(self.gate(fetch=Replay(self.fixtures(queue_tree="c" * 40)))[1])

    def test_unreadable_api_or_no_token_is_needs_review(self) -> None:
        broken = self.fixtures()
        broken[f"{API}/pulls/7"] = (403, {"message": "Forbidden"})
        for name, kwargs in {"timeout": {"fetch": Replay({}, broken=True)}, "403": {"fetch": Replay(broken)},
                             "no token": {"token": ""}}.items():
            with self.subTest(name):
                queue, report = self.gate(**kwargs)
                self.assertIsNotNone(queue.unverifiable if queue else None)
                self.assertFalse(report.ok)
                self.assertEqual([(f.rule, f.status) for f in report.findings],
                                 [("TEST-001", Status.NEEDS_REVIEW)])

    def test_push_and_pull_request_never_take_the_queue_path(self) -> None:
        for event in ("push", "pull_request", "workflow_dispatch"):
            with self.subTest(event=event):
                fetch = Replay(self.fixtures())
                queue, report = self.gate(event=event, fetch=fetch)
                self.assertIsNone(queue)
                self.assertEqual(fetch.urls, [])
                self.assertTest001Failure(report)


del TrustCase

if __name__ == "__main__":
    unittest.main()
