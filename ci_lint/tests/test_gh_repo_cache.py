"""ci_lint/gh_repo_cache.py: the repo-keyed incremental GraphQL cache
(forge-ci/ci.yml#428, piloted as obs-rust/.github workflow/ghcache.py)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from ci_lint import gh_repo_cache as ghc
from ci_lint.cargo_messages import JsonValue

FIELDS = "id number updatedAt state"


def node(n: int, updated: str, state: str = "OPEN") -> JsonValue:
    return {"id": f"I_{n}", "number": n, "updatedAt": updated, "state": state}


class FakeGitHub:
    """One repo connection served page by page, newest-updated first."""

    def __init__(self, nodes: list[JsonValue], page: int = 2) -> None:
        self.nodes = nodes
        self.page = page
        self.calls: list[str] = []

    def fetch(self, args: str, cursor: str | None) -> ghc.Page:
        self.calls.append(args)
        rows = sorted(self.nodes, key=lambda n: n["updatedAt"], reverse=True)  # type: ignore[index]
        if "states: OPEN" in args:
            rows = [n for n in rows if n["state"] == "OPEN"]  # type: ignore[index]
        start = int(cursor or 0)
        chunk = rows[start : start + self.page]
        more = start + self.page < len(rows)
        return ghc.Page(tuple(chunk), more, str(start + self.page) if more else None)


class GhRepoCache(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def read(self, gh: FakeGitHub, volatile: str = "", repo: str = "obs-rust/obs-studio") -> ghc.ReadResult:
        owner, name = repo.split("/")
        return ghc.read(owner, name, "issues", FIELDS, gh.fetch, volatile=volatile, root=self.root)

    def test_key_is_host_owner_repo_connection_and_field_hash(self) -> None:
        p = ghc.cache_path("Obs-Rust", "OBS-Studio", "issues", FIELDS, root=self.root)
        self.assertEqual(p.parent, self.root / "github.com" / "obs-rust" / "obs-studio")
        self.assertTrue(p.name.startswith("issues-") and p.name.endswith(".json"))
        # Whitespace never changes the key; a different selection does.
        self.assertEqual(p, ghc.cache_path("obs-rust", "obs-studio", "issues", "id  number\nupdatedAt state", root=self.root))
        self.assertNotEqual(p, ghc.cache_path("obs-rust", "obs-studio", "issues", "id updatedAt", root=self.root))

    def test_root_defaults_to_env_then_home(self) -> None:
        old = os.environ.get("GH_REPO_CACHE")
        self.addCleanup(lambda: os.environ.__setitem__("GH_REPO_CACHE", old) if old else os.environ.pop("GH_REPO_CACHE", None))
        os.environ["GH_REPO_CACHE"] = str(self.root)
        self.assertEqual(ghc.default_root(), self.root)
        os.environ.pop("GH_REPO_CACHE")
        self.assertEqual(ghc.default_root(), Path.home() / ".cache" / "gh-repo-cache")

    def test_cold_read_fetches_everything(self) -> None:
        gh = FakeGitHub([node(i, f"2026-10-0{i}T00:00:00Z") for i in range(1, 6)])
        r = self.read(gh)
        self.assertEqual(r.fetched, 5)
        self.assertEqual(sorted(n["number"] for n in r.nodes), [1, 2, 3, 4, 5])  # type: ignore[index]

    def test_warm_read_stops_at_the_high_water_mark(self) -> None:
        gh = FakeGitHub([node(i, f"2026-10-0{i}T00:00:00Z", "CLOSED") for i in range(1, 7)])
        self.read(gh)
        gh.nodes.append(node(7, "2026-10-08T00:00:00Z", "CLOSED"))
        r = self.read(gh)
        self.assertLessEqual(r.fetched, 2)  # the new node, plus the boundary
        self.assertEqual(len(r.nodes), 7)

    def test_updated_node_replaces_its_cached_copy(self) -> None:
        gh = FakeGitHub([node(1, "2026-10-01T00:00:00Z"), node(2, "2026-10-02T00:00:00Z")])
        self.read(gh)
        gh.nodes[0] = node(1, "2026-10-09T00:00:00Z", "CLOSED")
        r = self.read(gh)
        by = {n["number"]: n for n in r.nodes}  # type: ignore[index]
        self.assertEqual(by[1]["state"], "CLOSED")  # type: ignore[index]
        self.assertEqual(len(r.nodes), 2)

    def test_volatile_set_is_refetched_even_without_an_updated_at_bump(self) -> None:
        # A PR's checks or queue state change without bumping updatedAt.
        gh = FakeGitHub([node(1, "2026-10-01T00:00:00Z"), node(2, "2026-10-02T00:00:00Z", "CLOSED")])
        self.read(gh, volatile="states: OPEN")
        gh.nodes[0] = {**gh.nodes[0], "number": 1, "state": "OPEN", "checks": "SUCCESS"}  # type: ignore[dict-item]
        r = self.read(gh, volatile="states: OPEN")
        by = {n["number"]: n for n in r.nodes}  # type: ignore[index]
        self.assertEqual(by[1].get("checks"), "SUCCESS")  # type: ignore[union-attr]
        self.assertIn("states: OPEN", gh.calls[-1])

    def test_repos_never_share_a_file(self) -> None:
        a = FakeGitHub([node(1, "2026-10-01T00:00:00Z")])
        b = FakeGitHub([node(9, "2026-10-01T00:00:00Z")])
        self.read(a, repo="obs-rust/obs-studio")
        r = self.read(b, repo="obs-rust/obs-deps")
        self.assertEqual([n["number"] for n in r.nodes], [9])  # type: ignore[index]

    def test_same_repo_from_two_tools_shares_one_cache(self) -> None:
        gh = FakeGitHub([node(i, f"2026-10-0{i}T00:00:00Z", "CLOSED") for i in range(1, 6)])
        self.read(gh)
        calls = len(gh.calls)
        r = ghc.read("OBS-RUST", "obs-studio", "issues", FIELDS, gh.fetch, root=self.root)
        self.assertEqual(len(gh.calls) - calls, 1)  # one page reaches the mark
        self.assertEqual(len(r.nodes), 5)

    def test_corrupt_file_falls_back_to_a_full_read(self) -> None:
        gh = FakeGitHub([node(1, "2026-10-01T00:00:00Z")])
        p = self.read(gh).path
        p.write_text("{not json", encoding="utf-8")
        r = self.read(gh)
        self.assertEqual(r.fetched, 1)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["mark"], "2026-10-01T00:00:00Z")

    def test_parallel_writers_never_leave_half_a_file(self) -> None:
        gh = FakeGitHub([node(i, f"2026-10-{i:02d}T00:00:00Z") for i in range(1, 30)], page=50)
        errors: list[BaseException] = []

        def work() -> None:
            try:
                self.read(gh)
            except BaseException as exc:  # noqa: BLE001 -- surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        p = ghc.cache_path("obs-rust", "obs-studio", "issues", FIELDS, root=self.root)
        self.assertEqual(len(json.loads(p.read_text(encoding="utf-8"))["nodes"]), 29)
        self.assertEqual([x for x in p.parent.iterdir() if x.suffix == ".tmp"], [])

    def test_graphql_page_fetch_builds_one_connection_query(self) -> None:
        seen: list[str] = []

        def graphql(query: str, token: str) -> JsonValue:
            seen.append(query)
            return {"data": {"repository": {"issues": {
                "nodes": [node(1, "2026-10-01T00:00:00Z")],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}

        fetch = ghc.graphql_page_fetch(graphql, "tok", "obs-rust", "obs-studio", "issues", FIELDS)
        page = fetch("orderBy: {field: UPDATED_AT, direction: DESC}", None)
        self.assertEqual(len(page.nodes), 1)
        self.assertFalse(page.has_next)
        self.assertIn('repository(owner: "obs-rust", name: "obs-studio")', seen[0])
        self.assertIn("issues(first: 100, orderBy:", seen[0])

    def test_graphql_errors_raise(self) -> None:
        def graphql(query: str, token: str) -> JsonValue:
            return {"errors": [{"message": "API rate limit exceeded"}]}

        fetch = ghc.graphql_page_fetch(graphql, "tok", "o", "r", "issues", FIELDS)
        with self.assertRaises(ghc.GhCacheError):
            fetch("", None)


if __name__ == "__main__":
    unittest.main()
