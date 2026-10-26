"""`ci-lint cache trim|janitor|heal|preprune`: live deletions built on
`ci_lint.cache.audit`'s classification (round-4A brief, deliverable 5).

Every delete needs `actions: write`. `--dry-run` (or no `DeleteFn` at all)
never calls `delete` -- the CLI layer always prints exactly what WOULD be
deleted in that case. Nothing here classifies an entry itself; it all reuses
`ci_lint.cache.audit`'s CACHE-001/003/005/006/008/009 findings and deletes
by cache id (or, for `heal`, by literal key -- no id lookup needed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from ci_lint.cache.audit import ClassifiedEntry, _shape, audit_classified, classify, infer_default_branch
from ci_lint.cargo_messages import JsonValue
from ci_lint.cache.github_cache import CacheApiError, delete_cache_by_id, delete_cache_by_key, list_caches
from ci_lint.github_api import DeleteFn, FetchFn, GraphQLFn
from ci_lint.rules.cache_static import cardinality, family_footprint, parse_size
from ci_lint.schema import CiToml

JANITOR_RULES: frozenset[str] = frozenset({"CACHE-001", "CACHE-003", "CACHE-005", "CACHE-006", "CACHE-009"})


def _resolve_branch(default_branch: str | None, fetch: FetchFn, token: str, repo: str) -> str | None:
    """#425: a caller passing None asks for inference (event payload, then
    one GET /repos/{repo}); `None` back means the caller must fail closed.
    Extracted as a helper so each ops entry point stays under the C901
    ceiling instead of growing an inline branch."""

    return infer_default_branch(fetch, token, repo) if default_branch is None else default_branch
DEFAULT_STALE_DAYS = 5
# zackees/ci.yml#23 section 4.3: the janitor never deletes an entry younger
# than this, so a sweep cannot race the producer that just saved it or the
# consumers about to restore it.
JANITOR_GRACE_SECONDS = 10 * 60


@dataclass(frozen=True)
class DeletePlanEntry:
    id: int
    key: str
    size_in_bytes: int
    reason: str


@dataclass(frozen=True)
class OpsResult:
    planned: tuple[DeletePlanEntry, ...]  # every entry this run decided to delete (capped)
    deleted: tuple[DeletePlanEntry, ...]  # actually deleted (empty when dry_run)
    errors: tuple[str, ...]  # per-entry delete failures; never raised
    dry_run: bool
    warning: str | None = None

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "planned": [p.__dict__ for p in self.planned],
            "deleted": [p.__dict__ for p in self.deleted],
            "errors": list(self.errors),
            "dry_run": self.dry_run,
            "warning": self.warning,
        }

    def render(self) -> str:
        lines = [f"ci-lint cache: {'would delete' if self.dry_run else 'deleted'} {len(self.planned)} entrie(s)"]
        for p in self.planned:
            lines.append(f"  id={p.id} key={p.key!r} {p.size_in_bytes}B -- {p.reason}")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        if self.warning:
            lines.append(f"warning: {self.warning}")
        return "\n".join(lines)


@dataclass(frozen=True)
class OpsReport:
    """A janitor/preprune run: the delete result plus its human-readable
    text (janitor's before/after family table, preprune's forecast)."""

    result: OpsResult
    text: str


class _EntryIndex:
    """`classified` indexed by cache id and by key, for resolving findings."""

    def __init__(self, classified: list[ClassifiedEntry]) -> None:
        self.by_id: dict[int, ClassifiedEntry] = {c.entry.id: c for c in classified}
        self.by_key: dict[str, list[ClassifiedEntry]] = {}
        for c in classified:
            self.by_key.setdefault(c.entry.key, []).append(c)


@dataclass(frozen=True)
class _FamilyRow:
    label: str
    count: int
    size: int


@dataclass(frozen=True)
class _ShapeKey:
    family_id: str
    shape: str


@dataclass(frozen=True)
class LruEviction:
    cache_id: int
    reason: str


def _apply(planned: list[DeletePlanEntry], *, delete: DeleteFn | None, token: str, repo: str, dry_run: bool) -> OpsResult:
    if dry_run or delete is None:
        return OpsResult(planned=tuple(planned), deleted=(), errors=(), dry_run=True)
    deleted: list[DeletePlanEntry] = []
    errors: list[str] = []
    for p in planned:
        try:
            delete_cache_by_id(delete, token, repo, p.id)
            deleted.append(p)
        except CacheApiError as exc:
            errors.append(f"id={p.id} key={p.key!r}: {exc}")
    return OpsResult(planned=tuple(planned), deleted=tuple(deleted), errors=tuple(errors), dry_run=False)


def _finding_entry(f, index: _EntryIndex) -> ClassifiedEntry | None:
    """The one entry a live cache finding is about (ci.yml#137). GitHub
    cache keys are unique per ref, not per repository: the same key can sit
    on refs/heads/main and on refs/pull/N/merge at once, so resolve by the
    finding's `cache_id`. A finding without one resolves by key only when
    that key names exactly one entry -- never a guess between refs."""

    if not f.path or not f.path.startswith("cache:"):
        return None
    if f.cache_id is not None:
        return index.by_id.get(f.cache_id)
    matches = index.by_key.get(f.path[len("cache:") :], [])
    return matches[0] if len(matches) == 1 else None


def _findings_to_plan(
    report_findings, classified: list[ClassifiedEntry], rules: frozenset[str] | None
) -> tuple[DeletePlanEntry, ...]:
    index = _EntryIndex(classified)
    out: dict[int, DeletePlanEntry] = {}
    for f in report_findings:
        if rules is not None and f.rule not in rules:
            continue
        c = _finding_entry(f, index)
        if c is None or c.entry.id in out:
            continue
        out[c.entry.id] = DeletePlanEntry(id=c.entry.id, key=c.entry.key, size_in_bytes=c.entry.size_in_bytes, reason=f"{f.rule}: {f.message}")
    return tuple(out.values())


def trim(
    ci: CiToml,
    *,
    fetch: FetchFn,
    graphql: GraphQLFn | None,
    delete: DeleteFn | None,
    token: str,
    repo: str,
    max_deletes: int = 50,
    dry_run: bool = False,
    default_branch: str | None = "main",
) -> OpsResult:
    """Delete CACHE-008 entries only: closed/merged PR deltas (ONE
    GraphQL query for every referenced PR number, inside `audit_classified`)
    and stale-base deltas."""

    try:
        entries = list_caches(fetch, token, repo)
    except CacheApiError as exc:
        return OpsResult(planned=(), deleted=(), errors=(), dry_run=dry_run, warning=str(exc))
    classified = classify(ci, entries)
    default_branch = _resolve_branch(default_branch, fetch, token, repo)  # #425: infer, never guess `main`
    report = audit_classified(ci, classified, graphql=graphql, token=token, repo=repo, default_branch=default_branch)
    plan = {p.id: p for p in _findings_to_plan(report.findings, list(classified), frozenset({"CACHE-008"}))}
    # Same reasoning as the janitor (#357): `trim`'s plan can exceed
    # `max_deletes` too, and truncating in insertion order would spend the whole
    # cap on the smallest entries. Largest-first within the class.
    planned = _prioritise_plan(plan)[:max_deletes]
    result = _apply(planned, delete=delete, token=token, repo=repo, dry_run=dry_run)
    if report.warning and result.warning is None:
        result = OpsResult(**{**result.__dict__, "warning": report.warning})
    return result


def _parse_iso8601(value: str) -> float | None:
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _family_table(classified: list[ClassifiedEntry]) -> tuple[_FamilyRow, ...]:
    table: dict[str, _FamilyRow] = {}
    for c in classified:
        label = c.family_id or ("delta" if c.is_delta else ("retired" if c.is_retired else "undeclared"))
        row = table.get(label, _FamilyRow(label, 0, 0))
        table[label] = _FamilyRow(label, row.count + 1, row.size + c.entry.size_in_bytes)
    return tuple(table.values())


def _before_after_table(before: list[ClassifiedEntry], deleted_ids: set[int]) -> str:
    after = [c for c in before if c.entry.id not in deleted_ids]
    before_table = {row.label: row for row in _family_table(before)}
    after_table = {row.label: row for row in _family_table(after)}
    lines = [f"{'family':<24} {'before(n)':>10} {'before(B)':>12} {'after(n)':>10} {'after(B)':>12}"]
    for label in sorted(set(before_table) | set(after_table)):
        b = before_table.get(label, _FamilyRow(label, 0, 0))
        a = after_table.get(label, _FamilyRow(label, 0, 0))
        lines.append(f"{label:<24} {b.count:>10} {b.size:>12} {a.count:>10} {a.size:>12}")
    return "\n".join(lines)


def _protected_ids(ci: CiToml, classified: list[ClassifiedEntry]) -> set[int]:
    """#23 section 4.3: never delete the newest entry a required job
    restores -- the newest (by last_accessed_at) base entry of each declared
    family and key shape (not a PR entry, delta, or retired family); for an
    `evict = "lru"` family, only its single newest entry."""

    newest: dict[_ShapeKey, ClassifiedEntry] = {}
    for c in classified:
        if c.family_id is None or c.is_delta or c.is_retired or c.pr is not None:
            continue
        fam = ci.cache.family.get(c.family_id)
        k = _ShapeKey(c.family_id, "" if fam is not None and fam.evict == "lru" else _shape(c.entry.key))
        cur = newest.get(k)
        if cur is None or c.entry.last_accessed_at > cur.entry.last_accessed_at:
            newest[k] = c
    return {c.entry.id for c in newest.values()}


def _lru_evictions(ci: CiToml, classified: list[ClassifiedEntry]) -> tuple[LruEviction, ...]:
    """#23 section 4.2: an `evict = "lru"` family keeps its most recently
    used entries up to its declared footprint (max x cardinality) and
    evicts the rest, least recently used first."""

    out: list[LruEviction] = []
    for fam_id, fam in ci.cache.family.items():
        if fam.evict != "lru":
            continue
        if parse_size(fam.max) is None:
            continue
        # MUST be the same number CACHE-004 used. `max x cardinality(per)`
        # ignores `shapes`, so a family that declares shapes -- whose
        # CACHE-004 footprint is their SUM -- would get a SMALLER janitor
        # budget than the one precheck just approved, and the janitor would
        # then evict live entries that check blessed. One implementation,
        # both paths.
        budget = family_footprint(ci, fam)
        members = sorted(
            (c for c in classified if c.family_id == fam_id and not c.is_retired),
            key=lambda c: c.entry.last_accessed_at,
            reverse=True,
        )
        used = 0
        for c in members:
            used += c.entry.size_in_bytes
            if used > budget:
                out.append(LruEviction(c.entry.id, f"lru: family '{fam_id}' over its {budget}B footprint (evict = \"lru\")"))
    return tuple(out)


def janitor(  # noqa: C901
    ci: CiToml,
    *,
    fetch: FetchFn,
    graphql: GraphQLFn | None,
    delete: DeleteFn | None,
    token: str,
    repo: str,
    max_deletes: int = 100,
    dry_run: bool = False,
    stale_days: int = DEFAULT_STALE_DAYS,
    default_branch: str | None = "main",
    now: float | None = None,
    grace_seconds: int = JANITOR_GRACE_SECONDS,
    enforce_budget: bool = True,
) -> OpsReport:
    """zackees/ci.yml#23 section 4. Deletes: every entry of a closed/merged
    PR (key `pr-<N>`/legacy delta, or ref `refs/pull/<N>/merge`; a failed
    PR lookup keeps them); CACHE-001/003/005/006/009 entries (006 keeps the
    newest entry per key prefix); `evict = "lru"` families down to their
    footprint; entries not accessed in `stale_days` days. Never deletes an
    entry created within `grace_seconds`, nor the newest base entry of a
    family/key shape (except when it is poisoned, CACHE-005).
    Returns the result and its before/after table."""

    try:
        entries = list_caches(fetch, token, repo)
    except CacheApiError as exc:
        return OpsReport(OpsResult(planned=(), deleted=(), errors=(), dry_run=dry_run, warning=str(exc)), "")
    classified = classify(ci, entries)
    # #425: infer, never guess `main` -- a wrong guess deletes CACHE-003 wrongly.
    default_branch = _resolve_branch(default_branch, fetch, token, repo)
    report = audit_classified(ci, classified, graphql=graphql, token=token, repo=repo, default_branch=default_branch)
    by_id = {c.entry.id: c for c in classified}
    plan = {p.id: p for p in _findings_to_plan(report.findings, list(classified), JANITOR_RULES)}
    for c in classified:
        if c.entry.id in report.closed_pr_ids and c.entry.id not in plan:
            plan[c.entry.id] = DeletePlanEntry(
                id=c.entry.id, key=c.entry.key, size_in_bytes=c.entry.size_in_bytes,
                reason=f"closed PR: #{c.pr} is closed or merged",
            )

    lru = {e.cache_id: e.reason for e in _lru_evictions(ci, list(classified))}
    now_ts = now if now is not None else time.time()
    cutoff = now_ts - stale_days * 86400
    for c in classified:
        if c.entry.id in plan:
            continue
        if c.entry.id in lru:
            plan[c.entry.id] = DeletePlanEntry(
                id=c.entry.id, key=c.entry.key, size_in_bytes=c.entry.size_in_bytes, reason=lru[c.entry.id]
            )
            continue
        ts = _parse_iso8601(c.entry.last_accessed_at)
        if ts is not None and ts < cutoff:
            plan[c.entry.id] = DeletePlanEntry(
                id=c.entry.id,
                key=c.entry.key,
                size_in_bytes=c.entry.size_in_bytes,
                reason=f"stale: not accessed in >= {stale_days} day(s) (last_accessed_at={c.entry.last_accessed_at})",
            )

    # Safety (#23 section 4.3), applied last so no reason can bypass it.
    protected = _protected_ids(ci, list(classified))
    grace_cutoff = now_ts - grace_seconds
    for cache_id, entry in list(plan.items()):
        c = by_id[cache_id]
        created = _parse_iso8601(c.entry.created_at)
        if created is not None and created > grace_cutoff:
            del plan[cache_id]  # too young: may race its producer and consumers
        elif entry.id in protected and not entry.reason.startswith(("CACHE-005", "CACHE-009", "closed PR")):
            del plan[cache_id]  # the newest entry a required job restores

    budget = BudgetEvictions((), None)
    if enforce_budget:
        budget = _budget_evictions(ci, list(classified), plan, grace_cutoff)
        for cache_id in budget.ids:
            c = by_id[cache_id]
            plan[cache_id] = DeletePlanEntry(
                id=cache_id, key=c.entry.key, size_in_bytes=c.entry.size_in_bytes,
                reason="budget: repo total over [cache].budget (LRU)",
            )
    # Budget evictions keep their least-recently-accessed order, after every
    # provable reason, so a truncated sweep still evicts the oldest first.
    ordered = _prioritise_plan({k: v for k, v in plan.items() if k not in set(budget.ids)})
    planned = (ordered + [plan[i] for i in budget.ids])[:max_deletes]
    result = _apply(planned, delete=delete, token=token, repo=repo, dry_run=dry_run)
    deleted_or_planned_ids = {p.id for p in (result.deleted if not result.dry_run else result.planned)}
    table = _before_after_table(list(classified), deleted_or_planned_ids)
    # #425 (Problem 2): surface CACHE-004 live drift as a warning naming the
    # measured size, right where eviction runs -- before any sweep can apply
    # a stale declared max (obs-studio: a 1.17 GB zccache against a 550 MB
    # max, noticed by nobody because the audit only ran on demand). The
    # findings are never deletable (CACHE-004 is not in JANITOR_RULES);
    # this only makes every janitor run -- scheduled, end-of-queue, or
    # manual -- report the drift. The budget-ratio CACHE-004 (`cache:budget`)
    # is deliberately excluded: it already has its own channel below.
    live_drift = [
        f.message for f in report.findings if f.rule == "CACHE-004" and f.path.startswith("cache:family:")
    ]
    warning = "; ".join([*live_drift, *(w for w in (report.warning, budget.warning) if w)]) or None
    if warning:
        result = OpsResult(**{**result.__dict__, "warning": warning})
    return OpsReport(result, table)


def _family_newest_ids(classified: list[ClassifiedEntry]) -> set[int]:
    """The newest (by last_accessed_at) base entry of each family."""

    newest: dict[str, ClassifiedEntry] = {}
    for c in classified:
        if c.family_id is None or c.is_delta or c.is_retired or c.pr is not None:
            continue
        cur = newest.get(c.family_id)
        if cur is None or c.entry.last_accessed_at > cur.entry.last_accessed_at:
            newest[c.family_id] = c
    return {c.entry.id for c in newest.values()}


@dataclass(frozen=True)
class BudgetEvictions:
    ids: tuple[int, ...]  # least recently accessed first
    warning: str | None


def _budget_evictions(
    ci: CiToml, classified: list[ClassifiedEntry], plan: dict[int, DeletePlanEntry], grace_cutoff: float
) -> BudgetEvictions:
    """zackees/ci.yml#405: evict least recently accessed entries until the
    repository total is within [cache].budget, so GitHub's own eviction never
    picks. Keeps each family's newest base entry and every in-grace entry;
    returns a warning when those alone exceed the budget."""

    budget = parse_size(ci.cache.budget) if ci.cache.budget else None
    if not budget:
        return BudgetEvictions((), None)
    remaining = sum(c.entry.size_in_bytes for c in classified if c.entry.id not in plan)
    if remaining <= budget:
        return BudgetEvictions((), None)
    keep = _family_newest_ids(classified)
    candidates = []
    for c in classified:
        created = _parse_iso8601(c.entry.created_at)
        if c.entry.id in plan or c.entry.id in keep or (created is not None and created > grace_cutoff):
            continue
        candidates.append(c)
    candidates.sort(key=lambda c: (c.entry.last_accessed_at, c.entry.id))
    out = []
    for c in candidates:
        if remaining <= budget:
            break
        out.append(c.entry.id)
        remaining -= c.entry.size_in_bytes
    if remaining > budget:
        return BudgetEvictions(tuple(out), f"repo total {remaining}B still over [cache].budget {budget}B: only protected or in-grace entries remain")
    return BudgetEvictions(tuple(out), None)


def heal(*, delete: DeleteFn | None, token: str, repo: str, key: str, ref: str | None = None, dry_run: bool = False) -> OpsResult:
    """Delete exactly `key` -- no id lookup, no classification. A writer
    flow calls this right after restoring an unusable payload, so the
    save step that follows repopulates it (issue #6 §6's self-heal)."""

    planned = (DeletePlanEntry(id=-1, key=key, size_in_bytes=0, reason="explicit heal target"),)
    if dry_run or delete is None:
        return OpsResult(planned=planned, deleted=(), errors=(), dry_run=True)
    try:
        delete_cache_by_key(delete, token, repo, key, ref)
        return OpsResult(planned=planned, deleted=planned, errors=(), dry_run=False)
    except CacheApiError as exc:
        return OpsResult(planned=planned, deleted=(), errors=(str(exc),), dry_run=False)


def preprune(
    ci: CiToml,
    *,
    fetch: FetchFn,
    graphql: GraphQLFn | None,
    delete: DeleteFn | None,
    token: str,
    repo: str,
    lockfile_changed: bool,
    max_deletes: int = 50,
    dry_run: bool = False,
    default_branch: str | None = "main",
) -> OpsReport:
    """The CACHE-004 arithmetic, using LIVE sizes where a family has a live
    base entry (else its declared `max`), forecasting whether writing a
    changed lockfile's new keys would push the account over budget. Over
    budget: delete the superseded (CACHE-006) lockfile-keyed entries of the
    families about to be written -- the old-lockfile-hash copies the new
    write is about to make redundant."""

    try:
        entries = list_caches(fetch, token, repo)
    except CacheApiError as exc:
        return OpsReport(
            OpsResult(planned=(), deleted=(), errors=(), dry_run=dry_run, warning=str(exc)),
            "preprune: could not list live caches",
        )
    classified = classify(ci, entries)

    live_max: dict[str, int] = {}
    for c in classified:
        if c.family_id is not None and not c.is_delta and not c.is_retired:
            live_max[c.family_id] = max(live_max.get(c.family_id, 0), c.entry.size_in_bytes)

    lines: list[str] = []
    steady_total = 0
    lockfile_total = 0
    for fam_id in sorted(ci.cache.family):
        fam = ci.cache.family[fam_id]
        declared_max = parse_size(fam.max) or 0
        size = max(live_max.get(fam_id, 0), declared_max)
        card = cardinality(ci, fam.per)
        steady = size * card
        steady_total += steady
        if fam.lockfile:
            lockfile_total += steady
        source = "live" if fam_id in live_max and live_max[fam_id] >= declared_max else "declared"
        lines.append(f"  {fam_id}: {source} size={size}B x {card} = {steady}B" + (" (lockfile)" if fam.lockfile else ""))

    pr_budget = parse_size(ci.cache.pr.budget) if ci.cache.pr.budget else 0
    pr_budget = pr_budget or 0
    peak = lockfile_total if lockfile_changed else 0
    forecast = steady_total + peak + pr_budget
    budget_bytes = parse_size(ci.cache.budget) or 0

    summary = (
        "ci-lint cache preprune forecast (live sizes where known):\n"
        + "\n".join(lines)
        + f"\n  steady total          = {steady_total}B"
        + f"\n  + lockfile peak       = {peak}B (lockfile-changed={lockfile_changed})"
        + f"\n  + [cache.pr].budget   = {pr_budget}B"
        + f"\n  = forecast            = {forecast}B"
        + f"\n  budget ([cache].budget) = {budget_bytes}B"
    )

    if not lockfile_changed or forecast <= budget_bytes:
        return OpsReport(
            OpsResult(planned=(), deleted=(), errors=(), dry_run=dry_run), summary + "\n  under budget: nothing to preprune"
        )

    lockfile_families = {fid for fid, fam in ci.cache.family.items() if fam.lockfile}
    # #425: resolved only now -- the under-budget fast path above never needs
    # the branch, so it never pays the lookup.
    default_branch = _resolve_branch(default_branch, fetch, token, repo)
    report = audit_classified(ci, classified, graphql=graphql, token=token, repo=repo, default_branch=default_branch)
    index = _EntryIndex(list(classified))
    planned: list[DeletePlanEntry] = []
    for f in report.findings:
        if f.rule != "CACHE-006":
            continue
        c = _finding_entry(f, index)
        if c is None or c.family_id not in lockfile_families:
            continue
        planned.append(
            DeletePlanEntry(id=c.entry.id, key=c.entry.key, size_in_bytes=c.entry.size_in_bytes, reason=f"preprune ({f.rule}): {f.message}")
        )
    planned = planned[:max_deletes]
    result = _apply(planned, delete=delete, token=token, repo=repo, dry_run=dry_run)
    return OpsReport(result, summary + f"\n  OVER budget: pruning {len(planned)} superseded lockfile-keyed entrie(s)")


# Reasons ordered by how much a bounded run should prefer them. `max_deletes`
# bounds a single run's blast radius, so when the plan is larger than the cap
# the cap must select the entries that reclaim the most bytes -- otherwise the
# bound silently inverts its value.
#
# Measured 2026-10-05 on zackees/bosn (#357): the plan held 256 findings, of
# which 238 were 440-byte attestation entries (CACHE-001/CACHE-008) and 15
# were superseded multi-hundred-MB `compile` entries (CACHE-006). Truncating in
# dict-insertion order put the ENTIRE default cap on the attestation entries,
# so `cache janitor` reclaimed 44 KB of 9.44 GiB available. The selection was
# always correct -- a CACHE-006 entry names the newer entry it keeps -- only
# the ordering before truncation was wrong.
_PLAN_PRIORITY: tuple[str, ...] = (
    "CACHE-006",  # superseded: a newer sibling is kept, so these are pure reclaim
    "CACHE-009",  # retired family
    "CACHE-008",  # entry belongs to a closed/merged PR
    "CACHE-001",  # undeclared family
    "CACHE-003",
    "CACHE-005",
)


def _plan_rank(reason: str) -> int:
    for index, prefix in enumerate(_PLAN_PRIORITY):
        if reason.startswith(prefix):
            return index
    # LRU and stale-age entries carry a prose reason; treat them as the lowest
    # priority since they are the least provable of the six.
    return len(_PLAN_PRIORITY)


def _prioritise_plan(plan: dict[int, DeletePlanEntry]) -> list[DeletePlanEntry]:
    """Order a delete plan so a truncated run reclaims the most it safely can.

    Within a reason class, largest first: at 1 GiB per entry the difference
    between reclaiming the big entries and the small ones is the whole point of
    ordering at all. Ties break on id so the order is deterministic.
    """

    return sorted(
        plan.values(),
        key=lambda e: (_plan_rank(e.reason), -e.size_in_bytes, e.id),
    )
