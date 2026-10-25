"""Repo-keyed incremental GraphQL connection cache (forge-ci/ci.yml#428).

GraphQL has no ETag/304, so a tool that lists every issue or PR of a repo
pays the whole connection on every run, out of the 5,000-point hourly
budget its account shares with every other agent. This module caches each
repo connection on disk with a high-water mark instead:

* **Read**: ask for nodes ordered `UPDATED_AT DESC` and stop at the first
  one older than the cached mark; merge by node `id`.
* **Volatile set**: some fields change without bumping `updatedAt` (a PR's
  check rollup, `mergeStateStatus`, merge-queue membership, an issue's
  blockers). The caller names connection arguments (e.g. `states: OPEN`)
  whose nodes are refetched in full every warm run; keep that set small.
* **Key**: `<root>/github.com/<owner>/<repo>/<connection>-<sha256(fields)
  [:12]>.json`, lower-cased, whitespace-normalized. Every tool and checkout
  reading the same repo URL shares one cache; two field selections never
  collide.
* **Writes** are atomic (`os.replace`), so parallel tools never read half a
  file; a corrupt file falls back to a full read.

`<root>` is `$GH_REPO_CACHE`, else `~/.cache/gh-repo-cache`. In Actions,
keep it with `actions/cache` (`key: gh-repo-cache-${{ github.run_id }}`,
`restore-keys: gh-repo-cache-`). Piloted as obs-rust/.github
`workflow/ghcache.py`: a whole-org read went from 863 s (30 rate-limit
hits) cold to 22 s and 72 points warm.

`ci-lint gh-cache path|read` is the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import GraphQLFn

HOST = "github.com"
ORDER = "orderBy: {field: UPDATED_AT, direction: DESC}"
PAGE_SIZE = 100


class GhCacheError(Exception):
    """A GraphQL response carried `errors` or had no such connection."""


@dataclass(frozen=True)
class Page:
    nodes: tuple[JsonValue, ...]
    has_next: bool
    end_cursor: str | None


@dataclass(frozen=True)
class ReadResult:
    nodes: tuple[JsonValue, ...]
    fetched: int
    path: Path


# (connection arguments, cursor) -> one page of the connection.
PageFetch = Callable[[str, "str | None"], Page]


def default_root() -> Path:
    env = os.environ.get("GH_REPO_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "gh-repo-cache"


def cache_path(owner: str, repo: str, connection: str, fields: str, root: Path | None = None) -> Path:
    digest = hashlib.sha256(" ".join(fields.split()).encode("utf-8")).hexdigest()[:12]
    base = root if root is not None else default_root()
    return base / HOST / owner.lower() / repo.lower() / f"{connection}-{digest}.json"


@dataclass
class _Cached:
    mark: str
    nodes: dict[str, JsonValue]  # node id -> node, the file's JSON shape


def _load(path: Path) -> _Cached:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mark, nodes = data["mark"], data["nodes"]
        if isinstance(mark, str) and isinstance(nodes, dict):
            return _Cached(mark, nodes)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _Cached("", {})


def _save(path: Path, mark: str, nodes: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mark": mark, "nodes": nodes}, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _updated(n: JsonValue) -> str:
    return str(n.get("updatedAt", "")) if isinstance(n, dict) else ""


def _id(n: JsonValue) -> str:
    return str(n.get("id", "")) if isinstance(n, dict) else ""


def _pages(fetch: PageFetch, args: str, stop: Callable[[JsonValue], bool]) -> Iterator[JsonValue]:
    cursor: str | None = None
    while True:
        page = fetch(args, cursor)
        for n in page.nodes:
            if stop(n):
                return
            yield n
        if not page.has_next:
            return
        cursor = page.end_cursor


def read(
    owner: str,
    repo: str,
    connection: str,
    fields: str,
    fetch: PageFetch,
    volatile: str = "",
    root: Path | None = None,
) -> ReadResult:
    """Every node of the connection, refreshing only what changed.

    `fields` must select `id` and `updatedAt`."""
    path = cache_path(owner, repo, connection, fields, root)
    cached = _load(path)
    mark, nodes = cached.mark, cached.nodes
    fetched = 0
    for n in _pages(fetch, ORDER, lambda n: bool(mark) and _updated(n) < mark):
        nodes[_id(n)] = n
        fetched += 1
    if volatile and mark:
        for n in _pages(fetch, volatile, lambda n: False):
            nodes[_id(n)] = n
            fetched += 1
    if nodes:
        _save(path, max(_updated(n) for n in nodes.values()), nodes)
    return ReadResult(tuple(nodes.values()), fetched, path)


def graphql_page_fetch(
    graphql: GraphQLFn, token: str, owner: str, repo: str, connection: str, fields: str
) -> PageFetch:
    """A `PageFetch` over one repository connection through `graphql`."""

    def fetch(args: str, cursor: str | None) -> Page:
        after = f", after: {json.dumps(cursor)}" if cursor else ""
        extra = f", {args}" if args else ""
        query = (
            f"{{ repository(owner: {json.dumps(owner)}, name: {json.dumps(repo)}) {{ "
            f"{connection}(first: {PAGE_SIZE}{extra}{after}) {{ "
            f"pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }} }} }}"
        )
        body = graphql(query, token)
        if not isinstance(body, dict) or body.get("errors"):
            raise GhCacheError(f"{owner}/{repo} {connection}: {body.get('errors') if isinstance(body, dict) else body}")
        try:
            conn = body["data"]["repository"][connection]  # type: ignore[index,call-overload]
            info = conn["pageInfo"]
            return Page(tuple(conn["nodes"]), bool(info["hasNextPage"]), info["endCursor"])
        except (KeyError, TypeError) as exc:
            raise GhCacheError(f"{owner}/{repo}: no {connection} connection in the response") from exc

    return fetch


# ---- CLI --------------------------------------------------------------------


def _split(repo: str) -> tuple[str, ...]:
    parts = tuple(repo.split("/"))
    if len(parts) != 2 or not all(parts):
        raise SystemExit(f"ci-lint gh-cache: --repo must be owner/name, got {repo!r}")
    return parts


def _cmd_path(args: argparse.Namespace) -> int:
    owner, name = _split(args.repo)
    print(cache_path(owner, name, args.connection, args.fields))
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    from ci_lint.github_api import default_graphql

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("ci-lint gh-cache read: GITHUB_TOKEN (or GH_TOKEN) is required", file=sys.stderr)
        return 2
    owner, name = _split(args.repo)
    fetch = graphql_page_fetch(default_graphql, token, owner, name, args.connection, args.fields)
    try:
        result = read(owner, name, args.connection, args.fields, fetch, volatile=args.volatile)
    except GhCacheError as exc:
        print(f"ci-lint gh-cache read: {exc}", file=sys.stderr)
        return 1
    print(f"{args.repo} {args.connection}: {result.fetched} fetched, {len(result.nodes)} cached", file=sys.stderr)
    if args.json:
        json.dump(list(result.nodes), sys.stdout)
        print()
    return 0


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("gh-cache", help="repo-keyed incremental GraphQL connection cache (#428)")
    gsub = p.add_subparsers(dest="gh_cache_command", required=True)
    for name, func, help_ in (
        ("path", _cmd_path, "print the cache file for a repo connection"),
        ("read", _cmd_read, "read a repo connection through the cache (live)"),
    ):
        q = gsub.add_parser(name, help=help_)
        q.add_argument("--repo", required=True, help="owner/name")
        q.add_argument("--connection", required=True, help="e.g. issues, pullRequests")
        q.add_argument("--fields", required=True, help="node selection; must include id and updatedAt")
        if name == "read":
            q.add_argument("--volatile", default="", help="connection args refetched every run, e.g. 'states: OPEN'")
            q.add_argument("--json", action="store_true", help="print the nodes as JSON")
        q.set_defaults(func=func)
