"""ci.toml schema 3: frozen dataclasses + strict tomllib loader.

Section A of the round-1A brief. Unknown keys anywhere are `CT-001` (with
the key path); wrong types are `CT-002`; `schema` must equal exactly 3
(`CT-003`). Cross-reference validation (flows' platforms/suites exist, tags
reference existing suites/platforms, `extends` resolves without cycles,
exception fields well-formed) is also reported as `CT-002`, since the brief
does not allocate a separate rule ID for referential-integrity errors and
CT-002 ("wrong types") is the closest existing bucket for "a value that does
not resolve to a valid thing" -- documented as a decision in docs/ci-toml.md.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cache.families import ALLOWED_VIA_VALUES
from ci_lint.finding import Finding
from ci_lint.local_gate import GateConfig, parse_gate_table
from ci_lint.toml_cursor import Cursor, TomlValue

LINTER_RE = re.compile(r"^zackees/ci\.yml@([0-9a-fA-F]{40})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https?://")
SIZE_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(B|KB|MB|GB)\s*$", re.IGNORECASE)

# [cache.family.<id>].per's cardinality multiplier (ci_lint.rules.cache_static
# ._cardinality): "none" is the default (cardinality 1, matching an absent
# `per`); any other value is a CT-002 schema error rather than a silent
# fallback to cardinality 1 -- round-2A amendment 2. An unrecognized `per`
# (e.g. the template's "target") previously undercounted CACHE-004's
# arithmetic without any indication anything was wrong.
CACHE_FAMILY_PER_VALUES: frozenset[str] = frozenset({"none", "platform", "cross-platform", "os"})


# ── Platforms ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Platform:
    id: str
    target: str
    runs_on: str
    group: str
    wheel: str | None = None


# ── Python packaging ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CliBinary:
    name: str
    crate: str
    native: bool


@dataclass(frozen=True)
class PythonConfig:
    backend: str
    cli: CliBinary
    abi3: str
    pythons: tuple[str, ...]


# ── Rust workspace discipline ────────────────────────────────────────────


@dataclass(frozen=True)
class RustTests:
    max_binary: str
    max_total: str
    binaries: tuple[str, ...]


@dataclass(frozen=True)
class RustConfig:
    public: str
    private: str
    ship: tuple[tuple[str, ...], ...]
    tests: RustTests | None


# ── Lint lanes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DylintBudget:
    warm: str
    cold: str


@dataclass(frozen=True)
class DylintConfig:
    targets: str
    shape: str
    budget: DylintBudget


# ── Suites ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Suite:
    id: str
    run: str
    required: bool = False
    cache: str | None = None
    kind: str | None = None
    gating: bool = True


# ── Flows ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FlowBudget:
    critical_path: str | None = None


@dataclass(frozen=True)
class Flow:
    id: str
    extends: str | None = None
    platforms: tuple[str, ...] | str | None = None
    suites: tuple[str, ...] | str | None = None
    dylint: str | None = None
    budget: FlowBudget | None = None
    cache: str | None = None
    schedule: str | None = None
    wheels: str | None = None
    publish: str | None = None
    janitor: bool = False
    pre_prune: bool = False


# ── Tags ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TagAdd:
    platforms: tuple[str, ...] | str | None = None
    suites: tuple[str, ...] | str | None = None


@dataclass(frozen=True)
class TagRemove:
    suites: tuple[str, ...] | str | None = None


@dataclass(frozen=True)
class TagRule:
    id: str
    add: TagAdd | None = None
    remove: TagRemove | None = None
    cache: str | None = None
    flow: str | None = None
    publish: str | None = None


# ── Caches ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PrCache:
    mode: str
    families: tuple[str, ...]
    max_per_pr: str
    budget: str
    trim: str
    expected_open_prs: int | None = None
    measured_largest_delta: str | None = None


@dataclass(frozen=True)
class CacheShape:
    """One named shape of a family, with its OWN ceiling.

    `max` is one number for the whole family, so a family whose entries
    differ wildly in size cannot be declared truthfully: CACHE-004
    multiplies that one ceiling by the platform count and over-counts a
    repository whose big shapes are few. Measured on zackees/running-process
    (zackees/ci.yml#1321): `compile` holds two ~1.25 GiB shapes and six at
    <=529 MB, so `max x 6 platforms` models 7.8 GB for a family that
    actually occupies 5.0 GB -- and the only `per` values that would fit
    under-count, which is the dangerous direction, because
    `ci-lint cache janitor` LRU-evicts to `max x cardinality` and would
    delete live entries.

    `shapes` declares each shape and its own ceiling instead. The family's
    steady footprint is then the SUM, which is both honest and checkable.
    """

    scope: str
    max: str


@dataclass(frozen=True)
class CacheFamily:
    id: str
    via: str
    max: str
    lockfile: bool = False
    per: str | None = None
    min: str | None = None
    key: tuple[str, ...] | None = None
    # zackees/ci.yml#23 §4.2: "lru" makes the family evictable -- the cache
    # janitor LRU-evicts its entries down to its declared footprint
    # (max x cardinality). None (the default) keeps the newest entry per key
    # prefix instead (CACHE-006 superseded-entry cleanup).
    evict: str | None = None
    # CACHE-031: this family's saved keys carry the ancestor-defining lineage
    # label (ci_lint.cache_lineage: `m<n>[-c<k>]-<sha10>[-pr-<N>]`), which is
    # what makes a nearest-ancestor promotion search decidable from the keys
    # alone. `None` (the default) means the family keys on content alone --
    # a content hash, which discards the git-DAG ancestry a promotion needs.
    promote: str | None = None
    # An explicit literal key PREFIX, for a cache no `via` describes: a
    # repository's own action cache, or a sharded path cache like
    # `sccache/<a>/<b>/<c>/<hash>` (zackees/ci.yml#347). Mutually
    # exclusive with `via` -- a shape the allowlist knows is cited against
    # its producer's source, and a shape it does not is declared by the
    # repository that writes it, which is where the knowledge lives.
    prefix: str | None = None
    # Per-shape ceilings. Empty (the default) means the family is uniformly
    # sized and `max x cardinality` is the right bound. When declared, the
    # family's footprint is the sum of these instead.
    shapes: tuple[CacheShape, ...] = ()


CACHE_FAMILY_PROMOTE_VALUES: frozenset[str] = frozenset({"ancestor"})
CACHE_FAMILY_EVICT_VALUES: frozenset[str] = frozenset({"lru"})


CACHE_PROMOTE_MODES: tuple[str, ...] = ("off", "ancestor")
CACHE_SHARE_SCOPES: tuple[str, ...] = ("branch", "repo")


# Indexes of dataclasses and flat scalar maps -- shapes AGENTS.md's PY-002
# directive allows ("flat scalar maps, indexes of dataclasses and single-level
# parsed documents are fine"). Named so each field says what it keys.
PlatformIndex = dict[str, Platform]
SuiteIndex = dict[str, Suite]
FlowIndex = dict[str, Flow]
TagRuleIndex = dict[str, TagRule]
CacheFamilyIndex = dict[str, CacheFamily]


@dataclass(frozen=True)
class StrEntry:
    """One `key = "value"` pair of a string -> string table."""

    key: str
    value: str


@dataclass(frozen=True)
class StrMap:
    """A string -> string table (`[allow].setup-*.require`), kept as typed
    entries in declaration order rather than a dict field (PY-002, #193)."""

    entries: tuple[StrEntry, ...] = ()

    @classmethod
    def of(cls, table: Mapping[str, str]) -> StrMap:
        return cls(tuple(StrEntry(k, v) for k, v in table.items()))

    def get(self, key: str) -> str | None:
        return next((e.value for e in self.entries if e.key == key), None)

    def __contains__(self, key: object) -> bool:
        return any(e.key == key for e in self.entries)

    def __iter__(self) -> Iterator[str]:
        return (e.key for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class StrTupleEntry:
    """One `key = ["a", "b"]` pair of a string -> string-array table."""

    key: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class StrTupleMap:
    """A string -> string-array table (`[allow].workflows`,
    `[allow].permissions`), as typed entries (PY-002, #193)."""

    entries: tuple[StrTupleEntry, ...] = ()

    @classmethod
    def of(cls, table: Mapping[str, tuple[str, ...]]) -> StrTupleMap:
        return cls(tuple(StrTupleEntry(k, tuple(v)) for k, v in table.items()))

    def get(self, key: str) -> tuple[str, ...] | None:
        return next((e.values for e in self.entries if e.key == key), None)

    def __contains__(self, key: object) -> bool:
        return any(e.key == key for e in self.entries)

    def __iter__(self) -> Iterator[str]:
        return (e.key for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class CachePromote:
    """`[cache.promote]` -- how a pull request's cache entry becomes the
    default branch's, rather than the default branch rewriting it on every
    push.

    CACHE-024 / zackees/ci.yml#185, mechanism zackees/setup-soldr#552. The
    `mode = "ancestor"` declaration is a POLICY statement until that lands:
    setup-soldr's `action.yml` declares an `auto-key` input, but that input
    appears nowhere in its source or in its built bundle, so enabling it
    today would be a promise nothing implements. CACHE-030 reports it as
    needs_review rather than pretending otherwise."""

    mode: str = "off"
    max_age_hours: float = 168.0


@dataclass(frozen=True)
class CacheShare:
    """`[cache.share.<family-id>]` -- how far a family's entries are shared.

    "branch" (the default, and what an unqualified family already does) keeps
    a family within its own repository, restored by any branch that can
    prove the same lockfile/shape. "repo" declares that every branch reads
    and writes one shared entry, which is only coherent for a family whose
    `lockfile = true` key makes every branch's entry interchangeable."""

    family: str
    scope: str = "branch"


@dataclass(frozen=True)
class CacheConfig:
    budget: str
    write_on: tuple[str, ...]
    never: tuple[str, ...]
    retired: tuple[str, ...]
    pr: PrCache
    family: CacheFamilyIndex
    promote: CachePromote = CachePromote()
    # A tuple of records, not a map: `[cache.share.<id>]` is a declaration
    # list, and the owner directive prefers a record sequence to a map even
    # when the map's values are dataclasses (PY-002).
    share: tuple[CacheShare, ...] = ()


# ── Local (bosn -> act) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalConfig:
    runner: str
    lanes: tuple[str, ...]
    cache: str
    # `[local.gate]` (GATE-001..004, zackees/ci.yml#166): the declared local
    # gate, or None when the repository has not opted in.
    gate: GateConfig | None = None


# ── Fleet (central scanner opt-in) ────────────────────────────────────────


@dataclass(frozen=True)
class FleetConfig:
    """`[fleet]` -- round M2-42 (zackees/ci.yml#98): `sync-issues = true` is
    this repository's explicit opt-in for `ci-lint sync-issues --apply` to
    file/update/close fingerprinted `ci-lint` findings as GitHub issues here.
    Absent table or `sync-issues` key both default to `False` -- no fleet
    repo gets automated issues without this owner consent, and a ci.toml
    predating this key still loads unchanged."""

    sync_issues: bool = False


# ── Reuse (GEN-021 default-branch reuse, phase 2) ────────────────────────

_REUSE_MODES: tuple[str, ...] = ("off", "shadow", "enforce")
_MAX_AGE_HOURS_MAX = 168.0


@dataclass(frozen=True)
class ReuseConfig:
    """`[reuse.default-branch]` (zackees/ci.yml#158, design
    `docs/designs/default-branch-verified-reuse.md` section 7): how a
    default-branch push may reuse the proof a pull_request run already
    gave for a tree-identical tree.

    An absent table -- or an absent `default-branch` sub-table -- is
    `mode = "off"`: nothing is skipped, which is exactly the pre-#158
    behavior, so every ci.toml that predates this table loads unchanged.

    `required_jobs is None` is the table's `required-jobs = "plan"`: the
    proving job list comes from the planner's main-flow lane digests
    rather than a hand-maintained array. The two are deliberately
    different types so `mode = "shadow"` on a hand-written list cannot be
    silently reinterpreted as the plan-derived one.
    """

    mode: str = "off"
    workflows: tuple[str, ...] = ("ci.yml",)
    required_jobs: tuple[str, ...] | None = None
    exempt_jobs: tuple[str, ...] = ()
    max_age_hours: float = 24.0
    nightly: bool = True


# ── Allowlists ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SetupSoldrAllow:
    only_in: str
    require: StrMap


@dataclass(frozen=True)
class SetupUvAllow:
    """Round-4B: `[allow].setup-uv.require` -- checked on every
    `astral-sh/setup-uv` use, anywhere (unlike setup-soldr, there is no
    single wrapper location to restrict it to). A `require` value of
    `"plan"` means the input must be an expression derived from the
    precheck plan rather than a literal (`CACHE-003`; see
    `ci_lint.rules.cache_static.check_cache_003_setup_uv`)."""

    require: StrMap


@dataclass(frozen=True)
class CacheActionsAllow:
    """Round-4B: `[allow].cache-actions.only-in` -- the one directory a raw
    `actions/cache`/`actions/cache/save`/`actions/cache/restore` step may
    appear in (a SHA-pinned composite-action wrapper); everywhere else is
    `CACHE-001`. Defaults to `.github/actions/cache` when the table is
    omitted, so a `ci.toml` predating this key still loads."""

    only_in: str = ".github/actions/cache"


@dataclass(frozen=True)
class AllowConfig:
    workflows: StrTupleMap
    actions: tuple[str, ...]
    setup_soldr: SetupSoldrAllow
    tools: tuple[str, ...]
    secrets: tuple[str, ...]
    platform_selector: str
    platform_code: tuple[str, ...]
    # Round-4B additions, all optional/backward-compatible (see _parse_allow).
    permissions: StrTupleMap
    setup_uv: SetupUvAllow
    cache_actions: CacheActionsAllow


# ── Publish ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PypiPublish:
    auth: str
    environment: str
    mode: str


@dataclass(frozen=True)
class PublishConfig:
    pypi: PypiPublish | None


# ── Exceptions ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExceptionEntry:
    rule: str
    path: str
    reason: str
    issue: str
    expires: str


# ── Root ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CiToml:
    schema: int
    profile: str
    linter: str
    linter_sha: str | None
    platforms: PlatformIndex
    python: PythonConfig | None
    rust: RustConfig | None
    lint_dylint: DylintConfig | None
    suites: SuiteIndex
    flows: FlowIndex
    tags: TagRuleIndex
    cache: CacheConfig
    local: LocalConfig
    allow: AllowConfig
    publish: PublishConfig
    exceptions: tuple[ExceptionEntry, ...]
    fleet: FleetConfig
    reuse: ReuseConfig
    source_path: str


def _parse_platforms(root: Cursor) -> PlatformIndex:
    out: PlatformIndex = {}
    for pid, raw in root.raw_table_of_tables("platforms", required=True).items():
        sub = Cursor(raw, f"platforms.{pid}", root.findings, root.source)
        target = sub.str_("target")
        runs_on = sub.str_("runs-on")
        group = sub.str_("group")
        wheel = sub.str_("wheel", required=False)
        sub.finish()
        out[pid] = Platform(
            id=pid, target=target or "", runs_on=runs_on or "", group=group or "", wheel=wheel
        )
    return out


def _parse_python(root: Cursor) -> PythonConfig | None:
    raw = root.table_("python", required=False)
    if raw is None:
        return None
    sub = Cursor(raw, "python", root.findings, root.source)
    backend = sub.str_("backend") or ""
    cli_raw = sub.table_("cli", required=True)
    cli = None
    if cli_raw is not None:
        cli_sub = Cursor(cli_raw, "python.cli", root.findings, root.source)
        name = cli_sub.str_("name") or ""
        crate = cli_sub.str_("crate") or ""
        native = cli_sub.bool_("native", required=False, default=True)
        cli_sub.finish()
        cli = CliBinary(name=name, crate=crate, native=bool(native))
    abi3 = sub.str_("abi3") or ""
    pythons = sub.list_str("pythons")
    sub.finish()
    if cli is None:
        cli = CliBinary(name="", crate="", native=False)
    return PythonConfig(backend=backend, cli=cli, abi3=abi3, pythons=pythons)


def _parse_rust(root: Cursor) -> RustConfig | None:
    raw = root.table_("rust", required=False)
    if raw is None:
        return None
    sub = Cursor(raw, "rust", root.findings, root.source)
    public = sub.str_("public") or ""
    private = sub.str_("private") or ""
    ship = sub.list_str_list("ship", required=False)
    tests_raw = sub.table_("tests", required=False)
    tests = None
    if tests_raw is not None:
        tsub = Cursor(tests_raw, "rust.tests", root.findings, root.source)
        max_binary = tsub.str_("max-binary") or ""
        max_total = tsub.str_("max-total") or ""
        binaries = tsub.list_str("binaries", required=False)
        tsub.finish()
        tests = RustTests(max_binary=max_binary, max_total=max_total, binaries=binaries)
    sub.finish()
    return RustConfig(public=public, private=private, ship=ship, tests=tests)


def _parse_lint_dylint(root: Cursor) -> DylintConfig | None:
    lint_raw = root.table_("lint", required=False)
    if lint_raw is None:
        return None
    lint_sub = Cursor(lint_raw, "lint", root.findings, root.source)
    dylint_raw = lint_sub.table_("dylint", required=True)
    result = None
    if dylint_raw is not None:
        dsub = Cursor(dylint_raw, "lint.dylint", root.findings, root.source)
        targets = dsub.str_("targets") or ""
        shape = dsub.str_("shape") or ""
        budget_raw = dsub.table_("budget", required=True)
        budget = DylintBudget(warm="", cold="")
        if budget_raw is not None:
            bsub = Cursor(budget_raw, "lint.dylint.budget", root.findings, root.source)
            warm = bsub.str_("warm") or ""
            cold = bsub.str_("cold") or ""
            bsub.finish()
            budget = DylintBudget(warm=warm, cold=cold)
        dsub.finish()
        result = DylintConfig(targets=targets, shape=shape, budget=budget)
    lint_sub.finish()
    return result


def _parse_suites(root: Cursor) -> SuiteIndex:
    out: SuiteIndex = {}
    for sid, raw in root.raw_table_of_tables("suites", required=True).items():
        sub = Cursor(raw, f"suites.{sid}", root.findings, root.source)
        run = sub.str_("run") or ""
        required = sub.bool_("required", required=False, default=False)
        cache = sub.str_("cache", required=False)
        kind = sub.str_("kind", required=False)
        gating = sub.bool_("gating", required=False, default=True)
        sub.finish()
        out[sid] = Suite(
            id=sid, run=run, required=bool(required), cache=cache, kind=kind, gating=bool(gating)
        )
    return out


def _parse_flows(root: Cursor) -> FlowIndex:
    out: FlowIndex = {}
    for fid, raw in root.raw_table_of_tables("flow", required=True).items():
        sub = Cursor(raw, f"flow.{fid}", root.findings, root.source)
        extends = sub.str_("extends", required=False)
        platforms = sub.str_or_list("platforms", required=False)
        suites = sub.str_or_list("suites", required=False)
        dylint = sub.str_("dylint", required=False)
        budget_raw = sub.table_("budget", required=False)
        budget = None
        if budget_raw is not None:
            bsub = Cursor(budget_raw, f"flow.{fid}.budget", root.findings, root.source)
            critical_path = bsub.str_("critical-path", required=False)
            bsub.finish()
            budget = FlowBudget(critical_path=critical_path)
        cache = sub.str_("cache", required=False)
        schedule = sub.str_("schedule", required=False)
        wheels = sub.str_("wheels", required=False)
        publish = sub.str_("publish", required=False)
        janitor = sub.bool_("janitor", required=False, default=False)
        pre_prune = sub.bool_("pre-prune", required=False, default=False)
        sub.finish()
        out[fid] = Flow(
            id=fid,
            extends=extends,
            platforms=platforms,
            suites=suites,
            dylint=dylint,
            budget=budget,
            cache=cache,
            schedule=schedule,
            wheels=wheels,
            publish=publish,
            janitor=bool(janitor),
            pre_prune=bool(pre_prune),
        )
    return out


def _parse_tags(root: Cursor) -> TagRuleIndex:
    out: TagRuleIndex = {}
    for tid, raw in root.raw_table_of_tables("tags", required=True).items():
        sub = Cursor(raw, f"tags.{tid}", root.findings, root.source)
        add_raw = sub.table_("add", required=False)
        add = None
        if add_raw is not None:
            asub = Cursor(add_raw, f"tags.{tid}.add", root.findings, root.source)
            add_platforms = asub.str_or_list("platforms", required=False)
            add_suites = asub.str_or_list("suites", required=False)
            asub.finish()
            add = TagAdd(platforms=add_platforms, suites=add_suites)
        remove_raw = sub.table_("remove", required=False)
        remove = None
        if remove_raw is not None:
            rsub = Cursor(remove_raw, f"tags.{tid}.remove", root.findings, root.source)
            remove_suites = rsub.str_or_list("suites", required=False)
            rsub.finish()
            remove = TagRemove(suites=remove_suites)
        cache = sub.str_("cache", required=False)
        flow = sub.str_("flow", required=False)
        publish = sub.str_("publish", required=False)
        sub.finish()
        out[tid] = TagRule(id=tid, add=add, remove=remove, cache=cache, flow=flow, publish=publish)
    return out


def _parse_cache_shapes(root: Cursor, fsub: Cursor, fam_id: str) -> tuple[CacheShape, ...]:
    """`[cache.family.<id>.shapes.<scope>]` -- a per-shape ceiling, so a
    family whose entries differ in size can be declared truthfully instead
    of being modelled as `max x cardinality` (zackees/ci.yml#1321)."""

    raw = fsub.table_("shapes", required=False)
    if raw is None:
        return ()
    # Iterate `raw` directly rather than through a Cursor: the scope keys
    # are the table's own children, consumed here, so a `finish()` over the
    # same mapping would report every one of them as an unknown key.
    shapes: list[CacheShape] = []
    for scope, entry in raw.items():
        if not isinstance(entry, dict):
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.shapes.{scope}' must be a table with a 'max' size",
                    fix=f"write '[cache.family.{fam_id}.shapes.{scope}]' with 'max = \"1500MB\"'",
                )
            )
            continue
        scope_cur = Cursor(entry, f"cache.family.{fam_id}.shapes.{scope}", root.findings, root.source)
        shape_max = scope_cur.str_("max")
        scope_cur.finish()
        if shape_max and SIZE_RE.fullmatch(shape_max) is None:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.shapes.{scope}.max' is not a size "
                    f"({shape_max!r}); a shape with an unusable ceiling would be silently "
                    "dropped from the family's footprint and understate CACHE-004",
                    fix=f"write 'max = \"1500MB\"' (a number and one of B/KB/MB/GB)",
                )
            )
        elif shape_max:
            shapes.append(CacheShape(scope=scope, max=shape_max))
    if not shapes:
        root.findings.append(
            Finding(
                rule="CT-002",
                path=root.source,
                message=f"'cache.family.{fam_id}.shapes' declares no shape with a 'max'",
                fix=f"give at least one '[cache.family.{fam_id}.shapes.<scope>]' a 'max' size, or "
                "drop the 'shapes' table and declare 'max' alone",
            )
        )
    return tuple(shapes)


@dataclass(frozen=True)
class FamilyShapeClaim:
    """A family's key-shape claim, as a record rather than a positional
    tuple (PY-002) -- `via` and `prefix` are easy to transpose."""

    via: str
    prefix: str | None = None


def _parse_family_shape(root: Cursor, fsub: Cursor, fam_id: str) -> FamilyShapeClaim:
    """A family's key-shape claim: a recognized producer (`via`), the literal
    prefix its own producer writes (`prefix`), or neither.

    Exactly one is required. They are different claims about who owns the
    shape -- a shape the allowlist knows is cited against that producer's
    source; a shape it does not is declared by the repository that writes
    it, which is where the knowledge lives (#347).
    """

    via = fsub.str_("via", required=False) or ""
    literal = fsub.str_("prefix", required=False)
    if literal is not None and not literal.strip():
        root.findings.append(
            Finding(
                rule="CT-002",
                path=root.source,
                message=f"'cache.family.{fam_id}.prefix' is empty",
                fix="give the literal key prefix the producer writes, e.g. 'sccache/'",
            )
        )
        literal = None
    if not via and not literal:
        root.findings.append(
            Finding(
                rule="CT-002",
                path=root.source,
                message=f"'cache.family.{fam_id}' declares neither 'via' nor 'prefix', so nothing "
                "identifies the key shape this family owns",
                fix=f"set 'cache.family.{fam_id}.via' to a recognized producer (see "
                "docs/ci-toml.md's cache-family table), or 'prefix' to the literal key prefix "
                "this repository's own producer writes",
            )
        )
    return FamilyShapeClaim(via=via, prefix=literal)


def _parse_cache(root: Cursor) -> CacheConfig:
    raw = root.table_("cache", required=True)
    if raw is None:
        return CacheConfig(
            budget="0GB",
            write_on=(),
            never=(),
            retired=(),
            pr=PrCache(mode="", families=(), max_per_pr="", budget="", trim=""),
            family={},
        )
    sub = Cursor(raw, "cache", root.findings, root.source)
    budget = sub.str_("budget") or "0GB"
    write_on = sub.list_str("write-on", required=False)
    never = sub.list_str("never", required=False)
    retired = sub.list_str("retired", required=False)
    pr_raw = sub.table_("pr", required=True)
    pr = PrCache(mode="", families=(), max_per_pr="", budget="", trim="")
    if pr_raw is not None:
        psub = Cursor(pr_raw, "cache.pr", root.findings, root.source)
        mode = psub.str_("mode") or ""
        families = psub.list_str("families", required=False)
        max_per_pr = psub.str_("max-per-pr") or ""
        pr_budget = psub.str_("budget") or ""
        trim = psub.str_("trim") or ""
        expected_open_prs = psub.int_("expected-open-prs", required=False, default=None)
        measured_largest_delta = psub.str_("measured-largest-delta", required=False, default=None)
        psub.finish()
        pr = PrCache(
            mode=mode,
            families=families,
            max_per_pr=max_per_pr,
            budget=pr_budget,
            trim=trim,
            expected_open_prs=expected_open_prs,
            measured_largest_delta=measured_largest_delta,
        )
    family: CacheFamilyIndex = {}
    for fam_id, fam_raw in sub.raw_table_of_tables("family", required=True).items():
        fsub = Cursor(fam_raw, f"cache.family.{fam_id}", root.findings, root.source)
        # `via` is optional: a family may name the literal prefix its own
        # producer writes instead (zackees/ci.yml#347).
        claim = _parse_family_shape(root, fsub, fam_id)
        via, literal = claim.via, claim.prefix
        if via and literal:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}' sets both 'via' and 'prefix'; they are two "
                    "different claims about who owns the key shape",
                    fix=f"keep 'via = {via!r}' if a known producer owns the shape, else drop it and "
                    f"keep 'prefix = {literal!r}'",
                )
            )
        if literal is not None and not literal.strip():
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.prefix' is empty",
                    fix="give the literal key prefix the producer writes, e.g. 'sccache/'",
                )
            )
            literal = None
        if via and via not in ALLOWED_VIA_VALUES:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.via' = {via!r} is not one of "
                    f"{sorted(ALLOWED_VIA_VALUES)}",
                    fix=f"set 'cache.family.{fam_id}.via' to a recognized value (see docs/ci-toml.md's "
                    "cache-family table) -- a new setup-soldr-owned family needs a new "
                    "ci_lint.cache.families.FamilyShape entry, cited against setup-soldr's source, "
                    "before ci.toml can declare it",
                )
            )
        max_ = fsub.str_("max") or ""
        lockfile = fsub.bool_("lockfile", required=False, default=False)
        per = fsub.str_("per", required=False)
        if per is not None and per not in CACHE_FAMILY_PER_VALUES:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.per' = {per!r} is not one of "
                    f"{sorted(CACHE_FAMILY_PER_VALUES)}",
                    fix=f"set 'cache.family.{fam_id}.per' to one of: "
                    + ", ".join(sorted(CACHE_FAMILY_PER_VALUES))
                    + " (or omit it, which defaults to 'none')",
                )
            )
        min_ = fsub.str_("min", required=False)
        key = fsub.list_str("key", required=False)
        promote = fsub.str_("promote", required=False)
        if promote is not None and promote not in CACHE_FAMILY_PROMOTE_VALUES:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.promote' = {promote!r} is not one of "
                    f"{sorted(CACHE_FAMILY_PROMOTE_VALUES)}",
                    fix=f"set 'cache.family.{fam_id}.promote' to \"ancestor\" (this family's keys "
                    "carry the lineage label), or omit it to declare that they key on content alone",
                )
            )
        evict = fsub.str_("evict", required=False)
        if evict is not None and evict not in CACHE_FAMILY_EVICT_VALUES:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.family.{fam_id}.evict' = {evict!r} is not one of "
                    f"{sorted(CACHE_FAMILY_EVICT_VALUES)}",
                    fix=f"set 'cache.family.{fam_id}.evict' to \"lru\" (an evictable family the janitor "
                    "LRU-trims to its budget) or omit it (keep the newest entry per key prefix)",
                )
            )
        shapes = _parse_cache_shapes(root, fsub, fam_id)
        fsub.finish()
        family[fam_id] = CacheFamily(
            id=fam_id, via=via, max=max_, lockfile=bool(lockfile), per=per, min=min_, key=key or None,
            evict=evict, promote=promote, prefix=literal, shapes=tuple(shapes),
        )
    promote = _parse_cache_promote(root, sub)
    share = _parse_cache_share(root, sub, family)
    sub.finish()
    return CacheConfig(
        budget=budget, write_on=write_on, never=never, retired=retired, pr=pr, family=family,
        promote=promote, share=share,
    )


def _parse_cache_promote(root: Cursor, sub: Cursor) -> CachePromote:
    """`[cache.promote]`. Absent means `mode = "off"` -- today's behavior:
    a default-branch push rewrites its own cache rather than inheriting the
    one a pull request proved."""

    table = sub.table_("promote", required=False)
    if table is None:
        return CachePromote()
    psub = Cursor(table, "cache.promote", root.findings, root.source)
    mode = psub.str_("mode", required=False, default="off") or "off"
    if mode not in CACHE_PROMOTE_MODES:
        root.findings.append(
            Finding(
                rule="CT-002",
                path=root.source,
                message=f"'cache.promote.mode' must be one of {sorted(CACHE_PROMOTE_MODES)}, got {mode!r}",
                fix='set \'cache.promote.mode\' to "off" (the default) or "ancestor"',
            )
        )
        mode = "off"
    hours = psub.number_("max-age-hours", required=False, default=168.0)
    psub.finish()
    return CachePromote(mode=mode, max_age_hours=168.0 if hours is None else hours)


def _parse_cache_share(
    root: Cursor, sub: Cursor, family: CacheFamilyIndex
) -> tuple[CacheShare, ...]:
    """`[cache.share.<family-id>]` -- the scope each named family is shared
    over. A share entry must name a family this ci.toml declares: a scope
    for a family that does not exist describes nothing."""

    out: list[CacheShare] = []
    for fid, table in sub.raw_table_of_tables("share", required=False).items():
        ssub = Cursor(table, f"cache.share.{fid}", root.findings, root.source)
        scope = ssub.str_("scope", required=False, default="branch") or "branch"
        ssub.finish()
        if scope not in CACHE_SHARE_SCOPES:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.share.{fid}.scope' must be one of {sorted(CACHE_SHARE_SCOPES)}, got {scope!r}",
                    fix=f'set \'cache.share.{fid}.scope\' to "branch" (within this repository) or "repo"',
                )
            )
            continue
        if fid not in family:
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'cache.share.{fid}' names a family this ci.toml does not declare "
                    f"({', '.join(sorted(family)) or 'none'})",
                    fix=f"declare '[cache.family.{fid}]', or drop the '[cache.share.{fid}]' table",
                )
            )
            continue
        out.append(CacheShare(family=fid, scope=scope))
    return tuple(out)


def _parse_local(root: Cursor) -> LocalConfig:
    raw = root.table_("local", required=True)
    if raw is None:
        return LocalConfig(runner="", lanes=(), cache="")
    sub = Cursor(raw, "local", root.findings, root.source)
    runner = sub.str_("runner") or ""
    lanes = sub.list_str("lanes")
    cache = sub.str_("cache") or ""
    gate_raw = sub.table_("gate", required=False)
    sub.finish()
    gate = (
        parse_gate_table(gate_raw, path="local.gate", source=root.source, findings=root.findings)
        if gate_raw is not None
        else None
    )
    return LocalConfig(runner=runner, lanes=lanes, cache=cache, gate=gate)


def _parse_allow(root: Cursor) -> AllowConfig:
    raw = root.table_("allow", required=True)
    if raw is None:
        return AllowConfig(
            workflows=StrTupleMap(),
            actions=(),
            setup_soldr=SetupSoldrAllow(only_in="", require=StrMap()),
            tools=(),
            secrets=(),
            platform_selector="",
            platform_code=(),
            permissions=StrTupleMap(),
            setup_uv=SetupUvAllow(require=StrMap()),
            cache_actions=CacheActionsAllow(),
        )
    sub = Cursor(raw, "allow", root.findings, root.source)
    workflows_raw = sub.table_("workflows", required=True)
    workflows_table: dict[str, tuple[str, ...]] = {}
    if workflows_raw is not None:
        for wf_name, triggers in workflows_raw.items():
            if not isinstance(triggers, list) or not all(isinstance(t, str) for t in triggers):
                root.findings.append(
                    Finding(
                        rule="CT-002",
                        path=root.source,
                        message=f"'allow.workflows.{wf_name}' must be an array of strings",
                        fix=f"set 'allow.workflows.\"{wf_name}\"' to an array of trigger strings in {root.source}",
                    )
                )
                continue
            workflows_table[wf_name] = tuple(triggers)
    workflows = StrTupleMap.of(workflows_table)
    actions = sub.list_str("actions")
    ss_raw = sub.table_("setup-soldr", required=True)
    setup_soldr = SetupSoldrAllow(only_in="", require=StrMap())
    if ss_raw is not None:
        ssub = Cursor(ss_raw, "allow.setup-soldr", root.findings, root.source)
        only_in = ssub.str_("only-in") or ""
        require = StrMap.of(ssub.dict_str_str("require", required=False))
        ssub.finish()
        setup_soldr = SetupSoldrAllow(only_in=only_in, require=require)
    tools = sub.list_str("tools")
    secrets = sub.list_str("secrets", required=False)
    platform_selector = sub.str_("platform-selector") or ""
    platform_code = sub.list_str("platform-code")

    # Round-4B additions. Each is optional so a ci.toml predating this
    # round still loads unchanged (an absent table means "nothing extra
    # allowed" for permissions/setup-uv, and the documented default
    # wrapper directory for cache-actions).
    permissions = StrTupleMap.of(sub.dict_str_list_str("permissions", required=False))

    setup_uv_raw = sub.table_("setup-uv", required=False)
    setup_uv = SetupUvAllow(require=StrMap())
    if setup_uv_raw is not None:
        uvsub = Cursor(setup_uv_raw, "allow.setup-uv", root.findings, root.source)
        uv_require = StrMap.of(uvsub.dict_str_str("require", required=False))
        uvsub.finish()
        setup_uv = SetupUvAllow(require=uv_require)

    cache_actions_raw = sub.table_("cache-actions", required=False)
    cache_actions = CacheActionsAllow()
    if cache_actions_raw is not None:
        casub = Cursor(cache_actions_raw, "allow.cache-actions", root.findings, root.source)
        only_in = casub.str_("only-in", required=False, default=CacheActionsAllow().only_in)
        casub.finish()
        cache_actions = CacheActionsAllow(only_in=only_in or CacheActionsAllow().only_in)

    sub.finish()
    return AllowConfig(
        workflows=workflows,
        actions=actions,
        setup_soldr=setup_soldr,
        tools=tools,
        secrets=secrets,
        platform_selector=platform_selector,
        platform_code=platform_code,
        permissions=permissions,
        setup_uv=setup_uv,
        cache_actions=cache_actions,
    )


def _parse_fleet(root: Cursor) -> FleetConfig:
    raw = root.table_("fleet", required=False)
    if raw is None:
        return FleetConfig()
    sub = Cursor(raw, "fleet", root.findings, root.source)
    sync_issues = sub.bool_("sync-issues", required=False, default=False)
    sub.finish()
    return FleetConfig(sync_issues=bool(sync_issues))


def _reuse_finding(root: Cursor, sub: "Cursor", key: str, message: str, fix: str) -> None:
    root.findings.append(
        Finding(
            rule="CT-002",
            path=root.source,
            message=f"'{sub.key_path(key)}': {message}",
            fix=fix,
        )
    )


def _parse_reuse_required_jobs(root: Cursor, sub: Cursor) -> tuple[str, ...] | None:
    """`required-jobs`: "plan" (None) or an array of exact display names."""

    raw = sub.str_or_list("required-jobs", required=False, default="plan")
    if raw is None or raw == "plan":
        return None
    if isinstance(raw, str):
        _reuse_finding(
            root, sub, "required-jobs",
            f"the only accepted string is \"plan\", got {raw!r}",
            "set 'reuse.default-branch.required-jobs' to \"plan\" or to an array of exact "
            "job display names",
        )
        return None
    if not raw or any(not j.strip() for j in raw):
        _reuse_finding(
            root, sub, "required-jobs",
            f"must be \"plan\" or a non-empty array of unique non-empty job display names, "
            f"got {list(raw)!r}",
            "set 'reuse.default-branch.required-jobs' to \"plan\" or to an array of exact "
            "job display names",
        )
        return None
    if len(set(raw)) != len(raw):
        _reuse_finding(
            root, sub, "required-jobs",
            f"has duplicate entries: {list(raw)!r}",
            "list each proving job display name exactly once",
        )
    return tuple(raw)


def _parse_reuse(root: Cursor) -> ReuseConfig:
    raw = root.table_("reuse", required=False)
    if raw is None:
        return ReuseConfig()
    reuse = Cursor(raw, "reuse", root.findings, root.source)
    default_branch = reuse.table_("default-branch", required=False)
    if default_branch is None:
        # A bare `[reuse]` with no sub-table is an explicit opt-out, not an
        # error: it keeps the table around to host `default-branch` later.
        reuse.finish()
        return ReuseConfig()
    reuse.finish()
    sub = Cursor(default_branch, "reuse.default-branch", root.findings, root.source)

    mode = sub.str_("mode", required=False, default="off") or "off"
    if mode not in _REUSE_MODES:
        _reuse_finding(
            root, sub, "mode",
            f"must be one of {', '.join(repr(m) for m in _REUSE_MODES)}, got {mode!r}",
            f"set 'reuse.default-branch.mode' to one of {', '.join(repr(m) for m in _REUSE_MODES)}",
        )
        mode = "off"

    workflows = sub.list_str("workflows", required=False, default=("ci.yml",))
    if not workflows or any(not w.strip() for w in workflows):
        _reuse_finding(
            root, sub, "workflows",
            f"must be a non-empty array of non-empty workflow file names, got {list(workflows)!r}",
            "set 'reuse.default-branch.workflows' to a non-empty array such as [\"ci.yml\"]",
        )
        workflows = ("ci.yml",)

    required_jobs = _parse_reuse_required_jobs(root, sub)

    exempt_jobs = sub.list_str("exempt-jobs", required=False, default=())

    max_age = sub.number_("max-age-hours", required=False, default=24.0)
    max_age_hours = 24.0 if max_age is None else max_age
    if not 0 < max_age_hours <= _MAX_AGE_HOURS_MAX:
        _reuse_finding(
            root, sub, "max-age-hours",
            f"must satisfy 0 < x <= {_MAX_AGE_HOURS_MAX:g}, got {max_age_hours!r}",
            f"set 'reuse.default-branch.max-age-hours' to a number in (0, {_MAX_AGE_HOURS_MAX:g}]",
        )
        max_age_hours = 24.0

    nightly = sub.bool_("nightly", required=False, default=True)
    sub.finish()
    return ReuseConfig(
        mode=mode,
        workflows=workflows,
        required_jobs=required_jobs,
        exempt_jobs=exempt_jobs,
        max_age_hours=max_age_hours,
        nightly=True if nightly is None else bool(nightly),
    )


def _parse_publish(root: Cursor) -> PublishConfig:
    raw = root.table_("publish", required=True)
    if raw is None:
        return PublishConfig(pypi=None)
    sub = Cursor(raw, "publish", root.findings, root.source)
    pypi_raw = sub.table_("pypi", required=False)
    pypi = None
    if pypi_raw is not None:
        psub = Cursor(pypi_raw, "publish.pypi", root.findings, root.source)
        auth = psub.str_("auth") or ""
        environment = psub.str_("environment") or ""
        mode = psub.str_("mode") or ""
        psub.finish()
        pypi = PypiPublish(auth=auth, environment=environment, mode=mode)
    sub.finish()
    return PublishConfig(pypi=pypi)


def _parse_exceptions(root: Cursor) -> tuple[ExceptionEntry, ...]:
    out: list[ExceptionEntry] = []
    for idx, raw in enumerate(root.array_of_tables("exceptions")):
        sub = Cursor(raw, f"exceptions[{idx}]", root.findings, root.source)
        rule = sub.str_("rule") or ""
        path = sub.str_("path") or ""
        reason = sub.str_("reason") or ""
        issue = sub.str_("issue") or ""
        expires = sub.str_("expires") or ""
        sub.finish()
        if issue and not URL_RE.match(issue):
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'exceptions[{idx}].issue' is not a URL: {issue!r}",
                    fix=f"set 'exceptions[{idx}].issue' to a https:// issue URL in {root.source}",
                )
            )
        if expires and not DATE_RE.match(expires):
            root.findings.append(
                Finding(
                    rule="CT-002",
                    path=root.source,
                    message=f"'exceptions[{idx}].expires' is not YYYY-MM-DD: {expires!r}",
                    fix=f"set 'exceptions[{idx}].expires' to a YYYY-MM-DD date in {root.source}",
                )
            )
        out.append(ExceptionEntry(rule=rule, path=path, reason=reason, issue=issue, expires=expires))
    return tuple(out)


@dataclass(frozen=True)
class LoadResult:
    """What `load_ci_toml` returns: the parsed config (None when ci.toml is
    missing or not valid TOML) and every validation finding."""

    ci: CiToml | None
    findings: list[Finding]


def load_ci_toml(repo_root: Path) -> LoadResult:
    """Load and strictly validate ci.toml. `ci` is None on failure to even
    parse; otherwise the CiToml plus findings-from-validation."""

    findings: list[Finding] = []
    path = repo_root / "ci.toml"
    source = "ci.toml"
    if not path.is_file():
        findings.append(
            Finding(
                rule="CT-002",
                path=source,
                message="ci.toml not found at repo root",
                fix="create ci.toml at the repo root implementing schema 3 (see docs/ci-toml.md)",
            )
        )
        return LoadResult(None, findings)

    try:
        with path.open("rb") as fh:
            raw_root = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        findings.append(
            Finding(
                rule="CT-002",
                path=source,
                message=f"ci.toml is not valid TOML: {exc}",
                fix="fix the TOML syntax error reported above",
            )
        )
        return LoadResult(None, findings)

    root = Cursor(raw_root, "", findings, source)

    schema_val = root.int_("schema")
    if schema_val is not None and schema_val != 3:
        findings.append(
            Finding(
                rule="CT-003",
                path=source,
                message=f"schema = {schema_val}, expected 3",
                fix="set 'schema = 3' in ci.toml (this checker implements schema 3 only)",
            )
        )
    profile = root.str_("profile") or ""
    linter = root.str_("linter") or ""
    linter_sha: str | None = None
    m = LINTER_RE.match(linter)
    if linter and m is None:
        findings.append(
            Finding(
                rule="CT-002",
                path=source,
                message=f"'linter' must match 'zackees/ci.yml@<40-hex-sha>', got {linter!r}",
                fix="set 'linter' to \"zackees/ci.yml@<40-char commit SHA>\" in ci.toml",
            )
        )
    elif m is not None:
        linter_sha = m.group(1).lower()

    platforms = _parse_platforms(root)
    python = _parse_python(root)
    rust = _parse_rust(root)
    lint_dylint = _parse_lint_dylint(root)
    suites = _parse_suites(root)
    flows = _parse_flows(root)
    tags = _parse_tags(root)
    cache = _parse_cache(root)
    local = _parse_local(root)
    allow = _parse_allow(root)
    publish = _parse_publish(root)
    exceptions = _parse_exceptions(root)
    fleet = _parse_fleet(root)
    reuse = _parse_reuse(root)

    root.finish()

    ci = CiToml(
        schema=schema_val or 0,
        profile=profile,
        linter=linter,
        linter_sha=linter_sha,
        platforms=platforms,
        python=python,
        rust=rust,
        lint_dylint=lint_dylint,
        suites=suites,
        flows=flows,
        tags=tags,
        cache=cache,
        local=local,
        allow=allow,
        publish=publish,
        exceptions=exceptions,
        fleet=fleet,
        reuse=reuse,
        source_path=source,
    )

    findings.extend(_validate_cross_refs(ci))
    return LoadResult(ci, findings)


def _validate_cross_refs(ci: CiToml) -> list[Finding]:  # noqa: C901
    out: list[Finding] = []
    source = ci.source_path

    def bad(msg: str, fix: str) -> None:
        out.append(Finding(rule="CT-002", path=source, message=msg, fix=fix))

    # extends cycle / resolution
    for fid, flow in ci.flows.items():
        seen = {fid}
        cur = flow
        while cur.extends is not None:
            if cur.extends not in ci.flows:
                bad(
                    f"'flow.{fid}' extends unknown flow '{cur.extends}'",
                    f"set 'flow.{fid}.extends' to an existing [flow.*] id, or remove it",
                )
                break
            if cur.extends in seen:
                bad(
                    f"'flow.{fid}' extends cycle: {' -> '.join([*seen, cur.extends])}",
                    f"break the extends cycle starting at 'flow.{fid}'",
                )
                break
            seen.add(cur.extends)
            cur = ci.flows[cur.extends]

    def check_platform_ref(where: str, value: tuple[str, ...] | str | None) -> None:
        if value is None or value == "all":
            return
        for pid in value:
            if pid not in ci.platforms:
                bad(
                    f"'{where}' references unknown platform '{pid}'",
                    f"declare '[platforms.{pid}]' in ci.toml, or fix the id at '{where}'",
                )

    def check_suite_ref(where: str, value: tuple[str, ...] | str | None) -> None:
        if value is None or value in ("all", "tests"):
            return
        for sid in value:
            if sid not in ci.suites:
                bad(
                    f"'{where}' references unknown suite '{sid}'",
                    f"declare '[suites.{sid}]' in ci.toml, or fix the id at '{where}'",
                )

    for fid, flow in ci.flows.items():
        check_platform_ref(f"flow.{fid}.platforms", flow.platforms)
        check_suite_ref(f"flow.{fid}.suites", flow.suites)

    for tid, tag in ci.tags.items():
        if tag.add is not None:
            check_platform_ref(f"tags.{tid}.add.platforms", tag.add.platforms)
            check_suite_ref(f"tags.{tid}.add.suites", tag.add.suites)
        if tag.remove is not None:
            check_suite_ref(f"tags.{tid}.remove.suites", tag.remove.suites)
        if tag.flow is not None and tag.flow not in ci.flows:
            bad(
                f"'tags.{tid}.flow' references unknown flow '{tag.flow}'",
                f"declare '[flow.{tag.flow}]' in ci.toml, or fix 'tags.{tid}.flow'",
            )

    # profile requirements (section A)
    if ci.profile == "rust-pypi-app":
        if ci.python is None:
            bad(
                "profile 'rust-pypi-app' requires a [python] table",
                "add [python] with backend = \"soldr\" and [python.cli]",
            )
        elif ci.python.backend != "soldr":
            bad(
                f"[python].backend must be \"soldr\" for profile 'rust-pypi-app', got {ci.python.backend!r}",
                "set 'python.backend = \"soldr\"'",
            )
        if ci.rust is None:
            bad(
                "profile 'rust-pypi-app' requires a [rust] table",
                "add [rust] with public/private/ship",
            )
        required_suites = {sid for sid, s in ci.suites.items() if s.required}
        if "unit" not in required_suites:
            bad(
                "profile 'rust-pypi-app' requires suites.unit.required = true",
                "set '[suites.unit] required = true' in ci.toml",
            )
        if "smoke" not in required_suites:
            bad(
                "profile 'rust-pypi-app' requires suites.smoke.required = true",
                "set '[suites.smoke] required = true' in ci.toml",
            )
        if ci.publish.pypi is None or ci.publish.pypi.auth != "oidc":
            bad(
                "profile 'rust-pypi-app' requires [publish.pypi].auth = \"oidc\"",
                "set '[publish.pypi] auth = \"oidc\"' in ci.toml",
            )

    # `[reuse.default-branch]` (GEN-021 phase 2, zackees/ci.yml#158). Every
    # workflow the decision reads must be one the repository actually ships
    # (`[allow].workflows`); a typo there would make the decision read a
    # workflow that does not exist and silently never prove anything.
    reuse = ci.reuse
    if reuse.mode != "off":
        for wf in reuse.workflows:
            if wf not in ci.allow.workflows:
                bad(
                    f"'reuse.default-branch.workflows' names {wf!r}, which is not declared in "
                    f"'[allow].workflows' ({', '.join(sorted(ci.allow.workflows)) or 'none'})",
                    f"add '{wf}' to '[allow.workflows]' in {source}, or drop it from "
                    "'reuse.default-branch.workflows'",
                )

    return out
