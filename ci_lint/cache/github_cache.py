"""Injectable GitHub Actions cache + PR-state REST/GraphQL calls, shared by
`ci_lint.cache.audit` (read-only) and `ci_lint.cache.ops` (trim/janitor/
heal/preprune -- reads then deletes). No network in tests: every function
takes `ci_lint.github_api.FetchFn`/`DeleteFn`/`GraphQLFn`, matching
`ci_lint.reuse`'s and `ci_lint.runtime.gate`'s existing convention. Fixtures
recorded under ci_lint/tests/fixtures/runtime/cache/ (round-4A brief:
"record small API JSON fixtures ... copy the JSON, don't mutate anything" --
some are copied verbatim from `gh api repos/zackees/template-python-rust-cmd
/actions/caches`, some are small synthetic ones built for a rule this repo's
live cache state doesn't currently exercise; each fixture file says which).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.error import HTTPError

from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import DeleteFn, FetchFn, GitHubApiError, GraphQLFn

_CACHES_PER_PAGE = 100
# Page ceiling for one listing. Without it a repository with a very large
# cache (zackees/wezterm has 22713 entries) walks hundreds of pages, and a
# misbehaving endpoint that keeps returning a full page never terminates.
# Hitting the ceiling RAISES rather than returning a short list: a silently
# truncated cache audit undercounts the footprint and would let a repository
# over budget pass the very check meant to catch it.
_MAX_CACHE_PAGES = 40


@dataclass(frozen=True)
class CacheEntry:
    id: int
    ref: str
    key: str
    version: str
    size_in_bytes: int
    created_at: str
    last_accessed_at: str


class CacheApiError(Exception):
    """A live cache/PR-state call failed. Every caller degrades this to a
    `needs_review` finding (never a silent pass, never a crash) -- matching
    `ci_lint.reuse`'s "on any API error: warn, never fail" convention."""


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _entry_from_json(raw: JsonValue) -> CacheEntry | None:
    d = _as_dict(raw)
    cache_id = d.get("id")
    key = d.get("key")
    if not isinstance(cache_id, int) or not isinstance(key, str):
        return None
    return CacheEntry(
        id=cache_id,
        ref=d.get("ref") if isinstance(d.get("ref"), str) else "",
        key=key,
        version=d.get("version") if isinstance(d.get("version"), str) else "",
        size_in_bytes=d.get("size_in_bytes") if isinstance(d.get("size_in_bytes"), int) else 0,
        created_at=d.get("created_at") if isinstance(d.get("created_at"), str) else "",
        last_accessed_at=d.get("last_accessed_at") if isinstance(d.get("last_accessed_at"), str) else "",
    )


def list_caches(fetch: FetchFn, token: str, repo: str) -> list[CacheEntry]:
    """`GET /repos/{repo}/actions/caches`, paginated (`actions: read`)."""

    entries: list[CacheEntry] = []
    page = 1
    while True:
        try:
            payload = fetch(
                f"https://api.github.com/repos/{repo}/actions/caches"
                f"?per_page={_CACHES_PER_PAGE}&page={page}&sort=size_in_bytes&direction=desc",
                token,
            )
        except GitHubApiError as exc:
            raise CacheApiError(f"listing caches: {exc}") from exc
        body = _as_dict(payload)
        raw_entries = _as_list(body.get("actions_caches"))
        for raw in raw_entries:
            entry = _entry_from_json(raw)
            if entry is not None:
                entries.append(entry)
        if len(raw_entries) < _CACHES_PER_PAGE:
            break
        page += 1
        if page > _MAX_CACHE_PAGES:
            raise CacheApiError(
                f"listing caches: more than {_MAX_CACHE_PAGES * _CACHES_PER_PAGE} entries for "
                f"{repo}; refusing to report a truncated cache footprint"
            )
    return entries


def delete_cache_by_id(delete: DeleteFn, token: str, repo: str, cache_id: int) -> bool:
    """`DELETE /repos/{repo}/actions/caches/{id}` (`actions: write`); False means already absent (404)."""

    try:
        delete(f"https://api.github.com/repos/{repo}/actions/caches/{cache_id}", token)
    except GitHubApiError as exc:
        # Only cache DELETE treats absence as success; API reads retain their
        # existing fail-closed behavior. Never infer a status from message text.
        if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code == 404:
            return False
        raise CacheApiError(f"deleting cache id={cache_id}: {exc}") from exc
    return True


def delete_cache_by_key(delete: DeleteFn, token: str, repo: str, key: str, ref: str | None = None) -> bool:
    """`DELETE /repos/{repo}/actions/caches?key=<key>[&ref=<ref>]`
    (`actions: write`) -- `ci-lint cache heal`'s "delete exactly that key",
    with no id lookup needed first. False means already absent (404)."""

    url = f"https://api.github.com/repos/{repo}/actions/caches?key={key}"
    if ref:
        url += f"&ref={ref}"
    try:
        delete(url, token)
    except GitHubApiError as exc:
        # Only cache DELETE treats absence as success; API reads retain their
        # existing fail-closed behavior. Never infer a status from message text.
        if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code == 404:
            return False
        raise CacheApiError(f"deleting cache key={key!r}: {exc}") from exc
    return True


@dataclass(frozen=True)
class PrState:
    number: int
    state: str  # "OPEN" | "CLOSED" | "MERGED", GraphQL PullRequestState
    found: bool  # False if GitHub returned null (PR number doesn't exist)

    @property
    def closed_or_merged(self) -> bool:
        return self.found and self.state in ("CLOSED", "MERGED")


def fetch_pr_states(graphql: GraphQLFn, token: str, owner: str, name: str, numbers: list[int]) -> dict[int, PrState]:
    """ONE GraphQL query for every referenced PR number's state (round-4A
    brief: "closed/merged PRs (via ONE GraphQL query for all referenced PR
    numbers)"), via one aliased field per PR number."""

    if not numbers:
        return {}
    fields = "\n".join(f'  pr{n}: pullRequest(number: {n}) {{ number state }}' for n in sorted(set(numbers)))
    query = f'query {{ repository(owner: "{owner}", name: "{name}") {{\n{fields}\n}} }}'
    try:
        payload = graphql(query, token)
    except GitHubApiError as exc:
        raise CacheApiError(f"fetching PR states: {exc}") from exc
    body = _as_dict(payload)
    errors = _as_list(body.get("errors"))
    if errors:
        raise CacheApiError(f"fetching PR states: GraphQL returned errors: {errors}")
    repository = _as_dict(_as_dict(body.get("data")).get("repository"))
    result: dict[int, PrState] = {}
    for n in numbers:
        raw = repository.get(f"pr{n}")
        if raw is None:
            result[n] = PrState(number=n, state="", found=False)
            continue
        d = _as_dict(raw)
        state = d.get("state")
        result[n] = PrState(number=n, state=state if isinstance(state, str) else "", found=True)
    return result
