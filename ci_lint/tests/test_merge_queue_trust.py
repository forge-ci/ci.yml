"""#419 -- ci_lint/merge_queue_trust.py: queue-time verified reuse.

Replay-style: GitHub API responses are recorded fixtures keyed by URL, the
git repository is the GATE-008 test's throwaway base + attested PR head,
and the queue head is a fresh commit with the PR head's tree on the base.
Every row except a single-PR, tree-identical, attested entry fails closed.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.gate_trust import audit_sampled
from ci_lint.github_api import GitHubApiError
from ci_lint.merge_queue_trust import MergeGroupEvent, decide_merge_group, merge_group_event, parse_queue_ref
from ci_lint.tests.test_gate_trust import TrustCase, _git

API = "https://api.github.com/repos/o/r"


class Replay:
    def __init__(self, responses: dict[str, tuple[int, JsonValue]], *, broken: bool = False) -> None:
        self.responses = responses
        self.broken = broken
        self.urls: list[str] = []

    def __call__(self, url: str, token: str) -> tuple[int, JsonValue]:
        self.urls.append(url)
        if self.broken:
            raise GitHubApiError(f"GET {url} failed: timeout")
        return self.responses.get(url, (404, {"message": "Not Found"}))


class MergeGroupTest(TrustCase):
    def setUp(self) -> None:
        super().setUp()
        self.head = self.commit("feature", {"src.txt": "two\n"})
        self.tree = _git(self.repo, "rev-parse", f"{self.head}^{{tree}}")
        self.queue = _git(self.repo, "commit-tree", self.tree, "-p", self.base, "-m", "Merge pull request #7")

    def event(self, **over: str) -> MergeGroupEvent:
        fields = {"repo_slug": "o/r", "head_ref": f"refs/heads/gh-readonly-queue/main/pr-7-{self.base}",
                  "head_sha": self.queue, "base_sha": self.base}
        fields.update(over)
        return MergeGroupEvent(**fields)

    def exclude_docs(self) -> None:
        """Rebase the fixture onto a base whose lanes exclude docs/** and .github/**."""
        gate = (self.repo / "local-gate.toml").read_text(encoding="utf-8")
        gate = gate.replace('tools = ["git"]\n', 'tools = ["git"]\nexclude = ["docs/**", ".github/**"]\n')
        _git(self.repo, "checkout", "-q", self.base)
        self.base = self.commit("lanes exclude docs", {"local-gate.toml": gate}, lanes=None)
        self.head = self.commit("feature", {"src.txt": "two\n"})
        self.tree = _git(self.repo, "rev-parse", f"{self.head}^{{tree}}")

    def moved_queue(self, files: dict[str, str]) -> tuple[str, str]:
        """(queue head, its tree): main moved by `files` since the PR's base,
        then a merge-method queue commit merged the PR head onto it."""
        _git(self.repo, "checkout", "-q", self.base)
        moved_base = self.commit("main moved", files, lanes=None)
        _git(self.repo, "checkout", "-q", moved_base)
        (self.repo / "src.txt").write_text(_git(self.repo, "show", f"{self.head}:src.txt") + "\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        tree = _git(self.repo, "write-tree")
        queue = _git(self.repo, "commit-tree", tree, "-p", moved_base, "-p", self.head, "-m", "Merge pull request #7")
        _git(self.repo, "checkout", "-q", "-f", self.head)
        self.base = moved_base
        return queue, tree

    def fixtures(self, *, queue_tree: str | None = None, pr_state: str = "open",
                 compare: str = "identical", parents: list[str] | None = None,
                 queue: tuple[str, str] | None = None) -> dict[str, tuple[int, JsonValue]]:
        head_sha, tree = queue if queue else (self.queue, queue_tree or self.tree)
        default_parents = [self.base, self.head] if queue else [self.base]
        return {
            f"{API}/git/commits/{head_sha}": (200, {
                "sha": head_sha, "tree": {"sha": tree},
                "parents": [{"sha": p} for p in (parents or default_parents)]}),
            f"{API}/compare/main...{self.base}": (200, {"status": compare}),
            f"{API}/pulls/7": (200, {"state": pr_state, "author_association": "OWNER", "labels": [],
                                     "head": {"sha": self.head, "repo": {"full_name": "o/r"}},
                                     "base": {"ref": "main", "repo": {"full_name": "o/r"}}}),
            f"{API}/git/commits/{self.head}": (200, {"sha": self.head, "tree": {"sha": self.tree}}),
        }

    def run_case(self, event_name: str = "merge_group", event: MergeGroupEvent | None = None,
                 fetch: Replay | None = None):  # type: ignore[no-untyped-def]
        return decide_merge_group(self.repo, event_name, event if event is not None else self.event(),
                                  fetch=fetch or Replay(self.fixtures()), token="t")

    def assertReason(self, result, reason: str) -> None:  # type: ignore[override,no-untyped-def]
        self.assertEqual(result.decision.reason, reason, result.decision.detail)
        self.assertEqual(result.decision.trusted, reason == "trusted")

    def test_single_pr_tree_identical_attested_entry_is_trusted(self) -> None:
        result = self.run_case()
        self.assertReason(result, "trusted")
        self.assertEqual(result.pr, 7)
        self.assertEqual(result.pr_input.head_sha, self.head)
        self.assertIn(f"tree {self.tree[:12]}", result.decision.detail)

    def test_push_and_pull_request_never_take_the_queue_path(self) -> None:
        fetch = Replay(self.fixtures())
        self.assertReason(self.run_case("push", fetch=fetch), "not-merge-group")
        self.assertReason(self.run_case("pull_request", fetch=fetch), "not-merge-group")
        self.assertEqual(fetch.urls, [])

    def test_unparseable_ref_and_missing_payload(self) -> None:
        self.assertReason(self.run_case(event=self.event(head_ref="refs/heads/main")), "queue-ref-unparseable")
        result = decide_merge_group(self.repo, "merge_group", None, fetch=Replay({}), token="t")
        self.assertReason(result, "queue-unreadable")

    def test_multi_pr_group_fails_closed(self) -> None:
        # Ref base differs from the group base.
        other = "b" * 40
        self.assertReason(self.run_case(event=self.event(head_ref=f"gh-readonly-queue/main/pr-7-{other}")),
                          "multi-pr-group")
        # Group base is an earlier queue entry, not on main, and the queue head
        # differs from the attested head under a lane input: no credit (#537).
        moved = self.moved_queue({"other.txt": "three\n"})
        for compare in ("ahead", "diverged"):
            fetch = Replay(self.fixtures(compare=compare, queue=moved))
            self.assertReason(self.run_case(event=self.event(head_sha=moved[0]), fetch=fetch), "multi-pr-group")
        # Queue head is not a single commit on the base.
        self.assertReason(self.run_case(fetch=Replay(self.fixtures(parents=[self.base, other]))), "multi-pr-group")

    def test_merge_method_queue_head_merging_the_pr_head_is_trusted(self) -> None:
        # #538: a `mergeMethod: MERGE` queue builds `[base, PR head]` merge commits.
        result = self.run_case(fetch=Replay(self.fixtures(parents=[self.base, self.head])))
        self.assertReason(result, "trusted")
        # A merge commit whose second parent is not the PR head, or with the base
        # second, or with a third parent, still fails closed.
        for parents in ([self.base, "c" * 40], [self.head, self.base], [self.base, self.head, "d" * 40]):
            self.assertReason(self.run_case(fetch=Replay(self.fixtures(parents=parents))), "multi-pr-group")

    def test_tree_mismatch_fails_closed(self) -> None:
        # The API's trees differ but git finds no differing path: contradictory.
        self.assertReason(self.run_case(fetch=Replay(self.fixtures(queue_tree="c" * 40))), "tree-mismatch")
        # The base moved under a path a lane reads.
        moved = self.moved_queue({"other.txt": "x\n"})
        result = self.run_case(event=self.event(head_sha=moved[0]), fetch=Replay(self.fixtures(queue=moved)))
        self.assertReason(result, "tree-mismatch")
        self.assertIn("lane lint reads other.txt", result.decision.detail)

    def test_base_moved_only_outside_lane_inputs_is_credited(self) -> None:
        # #537: main moved under docs/**, which every lane excludes.
        self.exclude_docs()
        moved = self.moved_queue({"docs/a.md": "notes\n"})
        result = self.run_case(event=self.event(head_sha=moved[0]), fetch=Replay(self.fixtures(queue=moved)))
        self.assertReason(result, "trusted")
        self.assertIn("lane inputs unchanged after tree-mismatch", result.decision.detail)
        # The same move queued behind an earlier entry is credited too.
        result = self.run_case(event=self.event(head_sha=moved[0]),
                               fetch=Replay(self.fixtures(queue=moved, compare="ahead")))
        self.assertReason(result, "trusted")
        self.assertIn("after multi-pr-group", result.decision.detail)

    def test_base_moved_under_a_surface_or_mandatory_input_is_not_credited(self) -> None:
        self.exclude_docs()
        # A workflow is a trust surface even when every lane excludes .github/.
        moved = self.moved_queue({"docs/a.md": "n\n", ".github/workflows/ci.yml": "changed: true\n"})
        result = self.run_case(event=self.event(head_sha=moved[0]), fetch=Replay(self.fixtures(queue=moved)))
        self.assertReason(result, "tree-mismatch")
        # The gate script a lane runs is a mandatory input despite its exclusion.
        moved = self.moved_queue({"gate.py": "print('changed')\n"})
        result = self.run_case(event=self.event(head_sha=moved[0]), fetch=Replay(self.fixtures(queue=moved)))
        self.assertReason(result, "tree-mismatch")

    def test_unreadable_api_fails_closed(self) -> None:
        self.assertReason(self.run_case(fetch=Replay({}, broken=True)), "queue-unreadable")
        fixtures = self.fixtures()
        fixtures[f"{API}/pulls/7"] = (403, {"message": "Forbidden"})
        self.assertReason(self.run_case(fetch=Replay(fixtures)), "queue-unreadable")
        fixtures = self.fixtures()
        fixtures[f"{API}/git/commits/{self.head}"] = (200, {"sha": self.head})
        self.assertReason(self.run_case(fetch=Replay(fixtures)), "queue-unreadable")

    def test_closed_or_retargeted_pr_fails_closed(self) -> None:
        self.assertReason(self.run_case(fetch=Replay(self.fixtures(pr_state="closed"))), "queue-pr-mismatch")

    def test_unattested_pr_head_fails_closed(self) -> None:
        self.head = self.commit("unattested", {"src.txt": "three\n"}, lanes=None)
        self.tree = _git(self.repo, "rev-parse", f"{self.head}^{{tree}}")
        self.queue = _git(self.repo, "commit-tree", self.tree, "-p", self.base, "-m", "q")
        self.assertReason(self.run_case(), "head-not-attested")

    def test_audit_sample_and_shadow_follow_gate_008(self) -> None:
        trust = Path(self.repo, "local-gate.toml").read_text(encoding="utf-8")
        self.base = self.commit("shadow", {"local-gate.toml": trust.replace('mode = "enforce"', 'mode = "shadow"')},
                                lanes=None)
        self.setUp_queue()
        result = self.run_case()
        self.assertEqual(result.decision.reason, "shadow")
        self.assertFalse(result.decision.trusted)
        self.assertTrue(result.decision.would_trust)

    def test_audit_sample_is_drawn_on_the_pr_head_like_gate_008(self) -> None:
        trust = Path(self.repo, "local-gate.toml").read_text(encoding="utf-8")
        self.base = self.commit("audit", {"local-gate.toml": trust.replace("audit-rate = 0", "audit-rate = 2")},
                                lanes=None)
        for i in range(6):
            self.setUp_queue(f"v{i}\n")
            expected = audit_sampled(self.head, 2)
            self.assertReason(self.run_case(), "audit-sample" if expected else "trusted")

    def setUp_queue(self, body: str = "four\n") -> None:
        self.head = self.commit("feature2", {"src.txt": body})
        self.tree = _git(self.repo, "rev-parse", f"{self.head}^{{tree}}")
        self.queue = _git(self.repo, "commit-tree", self.tree, "-p", self.base, "-m", "q")

    def test_parsers(self) -> None:
        ref = parse_queue_ref(f"gh-readonly-queue/release/v1/pr-12-{'a' * 40}")
        self.assertIsNotNone(ref)
        self.assertEqual((ref.base, ref.pr), ("release/v1", 12))
        self.assertIsNone(parse_queue_ref(f"gh-readonly-queue/main/pr-0-{'a' * 40}"))
        self.assertIsNone(merge_group_event({"pull_request": {}}, "o/r"))
        event = merge_group_event({"merge_group": {"head_ref": "x", "head_sha": "y", "base_sha": "z"}}, "o/r")
        self.assertEqual(event.repo_slug if event else None, "o/r")


del TrustCase  # do not re-run the GATE-008 base class's (empty) tests here

if __name__ == "__main__":
    unittest.main()
