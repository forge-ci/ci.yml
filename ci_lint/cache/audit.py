"""`ci-lint cache audit`: live, read-only classification of every cache
entry the GitHub Actions cache API reports, against ci.toml's declared
families (round-4A brief, deliverable 4). Round-6E adds RUST-004: a
`setup-soldr:dylint-output`/`setup-soldr:dylint` family with NO live entry
at all on the default branch, while the default branch's latest run has a
successful `dylint` job -- "a successful Dylint run never saved its output
cache" (setup-soldr#538/#540, zackees/ci.yml#1).

RUST-004 cannot literally recompute setup-soldr's own cache-key hash to
match "the current Cargo.lock hash" the way a `via = "ci-lint"` family's
key can be rebuilt (`ci_lint.cache.keys`): `dylintOutputKey`'s hash covers
`cargo_lock` *together with* `source_revision` (the exact commit SHA),
`target_shape`, and several other inputs ci-lint has no access to
(setup-soldr `src/lib/resolve-setup.ts`, read read-only, ~line 1185); it is
fundamentally source-SHA-keyed, not a reproducible function of Cargo.lock
content alone. An earlier design correlated a family's live entries'
`created_at` against the qualifying job's own `started_at` instead --
proven WRONG live against the real template repo (round-6E): an unrelated
commit (workflow/docs-only) legitimately reproduces the IDENTICAL
`dylintOutputHash` and gets a correct "exact hit - skipping save" (the
existing entry is still perfectly valid; nothing was silently lost), so
"no entry created since this job started" false-positives on ordinary warm
reuse. The only signal ci-lint can trust without literally decoding the
hash is coarser but sound: does ANY live entry for the family exist on the
default branch at all -- exactly zackees/clud's observed failure mode
(zero cache layers reused, ever, on either sampled run). See
`_find_default_branch_dylint_run`/`_check_rust_004` below.

Requires `GITHUB_TOKEN` + `GITHUB_REPOSITORY` and `actions: read` (listing
caches, and -- when `fetch` is given to `audit_classified`/`run_audit` --
RUST-004's runs/jobs lookup); the PR-state half of CACHE-008 additionally
needs a `GraphQLFn` (also just a read). Never deletes anything -- see
`ci_lint.cache.ops` for trim/janitor/heal/preprune, which reuse this
module's classification and then act on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ci_lint.cache.families import resolve_prefixes, resolve_retired_prefix
from ci_lint.cache_lineage import parse_ancestor_key
from ci_lint.cargo_messages import JsonValue
from ci_lint.cache.github_cache import (
    CacheApiError,
    CacheEntry,
    PrState,
    fetch_pr_states,
    list_caches,
)
from ci_lint.finding import Finding, Status
from ci_lint.github_api import FetchFn, GitHubApiError, GraphQLFn
from ci_lint.rules.cache_static import cardinality, family_footprint, parse_size
from ci_lint.schema import CiToml

# RUST-004: the two `via` values whose live entries this correlates against
# real Dylint run evidence -- the output-cache family (source-SHA-keyed,
# issue #1's "cold on every new commit" finding) and the driver/foundation
# family (issue #1's qualified-vs-short-nightly identity-bridge gap).
DYLINT_CACHE_VIA_VALUES: frozenset[str] = frozenset({"setup-soldr:dylint-output", "setup-soldr:dylint"})
_RUST_004_RUNS_PER_PAGE = 20
_RUST_004_JOBS_PER_PAGE = 50

# CACHE-005: "poisoned/tiny" -- below the family's declared `min`, else at or
# below 1KB (issue #6 §6: clud's 253-byte entry). A declared `min` replaces
# the floor (#417), so a tiny-by-design family declares `min = "0B"`.
TINY_BYTES = 1024

# The attestation side-entry family `ci-lint attest keys` writes
# (`att1-<gate>-<lineage>`, ~440 B per gate; attest_cli.SIDE_FAMILY) is a
# known ci-lint family (#412): CACHE-005's floor never judges it -- a declared
# family `min` still wins -- and CACHE-001 never calls it undeclared, so the
# janitor cannot delete the lineage `attest resolve --require-gate` needs.
# Measured on bosn (bosn#721): the floor planned 78 of 196 att1-* entries as
# poisoned, and CACHE-005 bypasses the newest-entry guard, so janitor and
# resolver fought each other; on obs-studio the undeclared path deleted the
# lineage outright.
ATTEST_SIDE_PREFIX = "att1-"
BUDGET_WARN_RATIO = 0.90

# `delta-v1-pr-<N>-<family>-<platform>-b<base8>[-g<gen8>]` (#23 §5 delimited
# form; the legacy `delta-v1-pr<N>-` spelling still parses) -- family AND
# platform ids both legitimately contain hyphens (e.g. "linux-x64"), so a
# single blind regex with a `[^-]+` platform group mis-splits them
# (round-4A: caught by ci_lint/tests/test_cache_audit.py against a
# synthetic "compile"/"linux-x64" delta key, which a naive regex parsed as
# family="compile-linux", platform="x64"). Only the PR number and the
# base8 suffix are unambiguous from the string alone; `_split_family_
# platform` below resolves the rest against ci.toml's own declared
# platform ids. The trailing `-g<gen8>` is round-4C's per-generation
# commit-sha suffix (docs/ci-toml.md, template-python-rust-cmd#31): cache
# entries are immutable, so each save needs a distinct key, and the
# previous generation is healed (deleted) once a new one lands -- but
# CLOSED/MERGED-PR trim (CACHE-008) must still recognize a generation-
# suffixed key as a delta at all (round-4A's classifier predates the
# generation suffix and silently misclassified it as CACHE-001
# "undeclared family" instead, so a merged PR's straggler generation
# was never trimmed -- see the round-4C worker report). `gen8` is parsed
# but not otherwise used by classification: the delta's identity for
# staleness/closed-PR purposes is still (pr, family, platform, base8).
DELTA_OUTER_RE = re.compile(
    r"^delta-v1-pr-?(?P<pr>\d+)-(?P<rest>.+)-b(?P<base8>[0-9a-f]{8})(?:-g(?P<gen8>[0-9a-f]{6,40}))?$"
)

# CACHE-006 "superseded": two entries of the same declared family whose
# keys differ only in a trailing lockfile/version hash. Best-effort: strip
# one or more trailing hyphen-hex segments and group by what's left. Each
# segment is either 6-40 hex chars (covers both the 16-hex short hashes and
# 8-hex base tokens observed live) or exactly 64 hex chars (zackees/ci.yml#104:
# astral-sh/setup-uv's own dependency-file hash is a full sha256 hex digest,
# e.g. "setup-uv-2-...-3.14.7-pruned-<64 hex>" -- kernal-api evidence showed
# a superseded setup-uv-2- generation going undetected because the old
# {6,40} bound never matched a 64-char segment, so `_shape` left the full
# key untouched and two generations never grouped). A platform/os component
# earlier in the key (never hex-only, e.g. "linux-x64") is never stripped,
# so per-platform families are never treated as superseding each other.
_TRAILING_HASH_RE = re.compile(r"(?:-(?:[0-9a-f]{64}|[0-9a-f]{6,40}))+$")

# zackees/ci.yml#23 §4.1/§5: a cache belongs to PR N when its KEY contains a
# delimited `pr-<N>` component (any position; delimiters `-`, `_`, `.`, `/`,
# `:` or the key's start/end, so `spr-12` or `pr-12a` never match), or when
# it was saved on ref `refs/pull/<N>/merge`. The legacy delta form
# `delta-v1-pr<N>-...` (no hyphen) is still recognized via DELTA_OUTER_RE.
PR_KEY_COMPONENT_RE = re.compile(r"(?:^|[-_./:])pr-(?P<pr>\d+)(?=$|[-_./:])")
PR_REF_RE = re.compile(r"^refs/pull/(?P<pr>\d+)/merge$")


def pr_from_key(key: str) -> int | None:
    m = PR_KEY_COMPONENT_RE.search(key)
    if m is not None:
        return int(m.group("pr"))
    legacy = DELTA_OUTER_RE.match(key)
    return int(legacy.group("pr")) if legacy is not None else None


def pr_from_ref(ref: str) -> int | None:
    m = PR_REF_RE.match(ref)
    return int(m.group("pr")) if m is not None else None


# zackees/ci.yml#88: setup-soldr's cargo-registry key is
# `setup-soldr-cargoregistry-v<N>-<os>-<arch>-<Cargo.lock hash>-<digest>`
# (setup-soldr src/lib/resolve-setup.ts:1055-1080), where `<digest>` is the
# toolchain-signature digest (resolve-setup.ts:771-789: channel, components,
# TARGETS, ...). Jobs with different targets/toolchains legitimately keep
# one live entry per digest at once (kernal-api: 8 per-job entries), so the
# digest is part of the shape; only the Cargo.lock hash is a generation.
# (An optional `x<suffix>-` validation namespace sits between the two.)
_CARGO_REGISTRY_RE = re.compile(
    r"^(?P<head>setup-soldr-cargoregistry-v\d+-.+?-)[0-9a-f]{16}-(?P<tail>(?:x[^-]+-)?[0-9a-f]{16})$"
)


def _shape(key: str) -> str:
    m = _CARGO_REGISTRY_RE.match(key)
    if m is not None:
        return f"{m.group('head')}<lock>-{m.group('tail')}"
    # setup-soldr's ancestor pilot (#566, zackees/ci.yml#335) carries its sha
    # MID-key, so the trailing-hash strip below leaves the key intact and two
    # saves of one lineage never group -- which silently blinds CACHE-006 to
    # every ancestor entry at the moment the pilot is adopted. Collapse the
    # per-save provenance instead; identity and PR scope still separate.
    ancestor = parse_ancestor_key(key)
    if ancestor is not None:
        return ancestor.family
    stripped = _TRAILING_HASH_RE.sub("", key)
    return stripped if stripped else key


@dataclass(frozen=True)
class DeltaIdentity:
    pr: int
    family: str
    platform: str
    base8: str


@dataclass(frozen=True)
class ClassifiedEntry:
    entry: CacheEntry
    family_id: str | None  # a declared [cache.family.<id>] id, or None
    is_delta: bool
    delta: DeltaIdentity | None
    is_retired: bool  # matches a [cache].retired prefix
    # The PR this entry belongs to (#23 §4.1): from a delimited `pr-<N>` key
    # component / legacy delta key (`pr_in_key`), else from a
    # `refs/pull/<N>/merge` ref. None for main/nightly/branch entries.
    pr: int | None = None
    pr_in_key: bool = False


@dataclass(frozen=True)
class AuditReport:
    classified: tuple[ClassifiedEntry, ...]
    findings: tuple[Finding, ...]
    total_bytes: int
    budget_bytes: int | None
    budget_ratio: float | None
    warning: str | None = None
    # Cache ids of every entry (any family, delta or not) that belongs to a
    # PR the lookup confirmed closed or merged -- the janitor deletes these
    # (#23 §4.1). A failed lookup leaves this empty (fail safe: keep).
    closed_pr_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _FamilyPlatform:
    family: str
    platform: str


@dataclass(frozen=True)
class _Generation:
    """CACHE-006's grouping key: entries sharing a family, a stripped key
    shape and (for PR-keyed entries) a PR number are generations of one
    cache."""

    family_id: str
    shape: str
    pr: int | None


@dataclass(frozen=True)
class _RuleResult:
    findings: tuple[Finding, ...]
    warning: str | None


@dataclass(frozen=True)
class _Cache008Result:
    findings: tuple[Finding, ...]
    warning: str | None
    closed_pr_ids: frozenset[int]


@dataclass(frozen=True)
class _FamilyEntries:
    family_id: str
    members: tuple[ClassifiedEntry, ...]


@dataclass(frozen=True)
class _BaseEntries:
    """Current (non-PR, non-delta, non-retired) entries by family id."""

    families: tuple[_FamilyEntries, ...]

    def get(self, family_id: str) -> tuple[ClassifiedEntry, ...]:
        for fam in self.families:
            if fam.family_id == family_id:
                return fam.members
        return ()


@dataclass
class _FamilyTally:
    count: int = 0
    size: int = 0


def _split_family_platform(rest: str, ci: CiToml) -> _FamilyPlatform | None:
    """`rest` is `"<family>-<platform>"`. Resolve it against ci.toml's own
    declared platform ids (longest first, though ids are expected unique)
    rather than guessing with a hyphen-free regex group."""

    for platform_id in sorted(ci.platforms, key=len, reverse=True):
        suffix = f"-{platform_id}"
        if rest.endswith(suffix) and len(rest) > len(suffix):
            return _FamilyPlatform(rest[: -len(suffix)], platform_id)
    return None


def classify(ci: CiToml, entries: list[CacheEntry]) -> tuple[ClassifiedEntry, ...]:
    return tuple(_with_pr(_classify_one(ci, e)) for e in entries)


def _with_pr(c: ClassifiedEntry) -> ClassifiedEntry:
    key_pr = c.delta.pr if c.delta is not None else pr_from_key(c.entry.key)
    if key_pr is not None:
        return replace(c, pr=key_pr, pr_in_key=True)
    ref_pr = pr_from_ref(c.entry.ref)
    return replace(c, pr=ref_pr) if ref_pr is not None else c


def _classify_one(ci: CiToml, entry: CacheEntry) -> ClassifiedEntry:
    m = DELTA_OUTER_RE.match(entry.key)
    if m is not None:
        split = _split_family_platform(m.group("rest"), ci)
        if split is not None:
            family, platform = split.family, split.platform
            delta = DeltaIdentity(pr=int(m.group("pr")), family=family, platform=platform, base8=m.group("base8"))
            return ClassifiedEntry(
                entry=entry,
                family_id=family if family in ci.cache.family else None,
                is_delta=True,
                delta=delta,
                is_retired=False,
            )
        # A delta-shaped key whose platform segment matches no declared
        # platform id: fall through to the undeclared-family path below
        # (never silently misclassified as a base layer either).

    for retired_name in ci.cache.retired:
        if entry.key.startswith(resolve_retired_prefix(retired_name)):
            return ClassifiedEntry(entry=entry, family_id=None, is_delta=False, delta=None, is_retired=True)

    for fam_id, fam in ci.cache.family.items():
        # A literal `prefix` is the declaring repository's own claim about a
        # shape no `via` describes (zackees/ci.yml#347) -- a path-shaped
        # cache like `sccache/<a>/<b>/<c>/<hash>`, or a repository's own
        # action cache. `startswith` on the declared stem is what makes a
        # sharded path cache classifiable at all.
        prefixes = (fam.prefix,) if fam.prefix else resolve_prefixes(fam.via, fam_id)
        if any(entry.key.startswith(p) for p in prefixes if p):
            return ClassifiedEntry(entry=entry, family_id=fam_id, is_delta=False, delta=None, is_retired=False)

    return ClassifiedEntry(entry=entry, family_id=None, is_delta=False, delta=None, is_retired=False)


def _check_cache_001(classified: tuple[ClassifiedEntry, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for c in classified:
        if c.is_retired or c.family_id is not None:
            continue
        if c.entry.key.startswith(ATTEST_SIDE_PREFIX):
            continue  # a known ci-lint family (#412): never undeclared
        kind = "PR delta for an" if c.is_delta else "cache"
        findings.append(
            Finding(
                rule="CACHE-001",
                path=f"cache:{c.entry.key}",
                cache_id=c.entry.id,
                message=f"{kind} undeclared family: id={c.entry.id} key {c.entry.key!r} matches no "
                f"declared [cache.family] prefix",
                fix="declare a [cache.family.<id>] entry whose 'via' resolves to this key's prefix "
                "(see docs/ci-toml.md's family table), or stop writing it -- ci-lint cache janitor "
                "deletes an undeclared entry once nothing produces it any more",
            )
        )
    return findings


def _check_cache_003(
    classified: tuple[ClassifiedEntry, ...], default_branch: str
) -> list[Finding]:
    default_ref = f"refs/heads/{default_branch}"
    findings: list[Finding] = []
    for c in classified:
        if c.family_id is None or c.is_delta or c.is_retired or c.pr_in_key:
            continue  # a pr-<N>-keyed entry is PR-scoped (#23 §5), deleted when its PR closes
        if c.entry.ref and c.entry.ref != default_ref:
            findings.append(
                Finding(
                    rule="CACHE-003",
                    path=f"cache:{c.entry.key}",
                    cache_id=c.entry.id,
                    message=f"base-layer family '{c.family_id}' cache id={c.entry.id} was saved on "
                    f"ref {c.entry.ref!r}, not the default branch ({default_ref!r})",
                    fix="base cache layers may only be written by a declared writer flow on the "
                    "default branch (issue #6 §6, [cache].write-on) -- delete this entry (ci-lint "
                    "cache janitor) and check the writer job's ref condition / save-ok call site",
                )
            )
    return findings


def _check_cache_005(ci: CiToml, classified: tuple[ClassifiedEntry, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for c in classified:
        entry = c.entry
        reason: str | None = None
        fam = ci.cache.family[c.family_id] if c.family_id is not None else None
        min_bytes = parse_size(fam.min) if fam is not None and fam.min else None
        if fam is not None and min_bytes is not None:
            # A declared `min` replaces the floor (#417): a family whose
            # entries are tiny by design (soldr verdicts, pass markers)
            # declares `min = "0B"`.
            if entry.size_in_bytes < min_bytes:
                reason = f"{entry.size_in_bytes}B is below [cache.family.{c.family_id}].min ({fam.min})"
        elif entry.key.startswith(ATTEST_SIDE_PREFIX):
            # A known ci-lint family (#412): attestation side entries are
            # ~440 B by design, with or without a family declaration, so
            # the floor never judges them (a declared `min`, handled above,
            # still wins). This is the route the janitor and
            # `attest resolve --require-gate` need to stop fighting.
            continue
        elif entry.size_in_bytes <= TINY_BYTES:
            reason = f"{entry.size_in_bytes}B is at or below the {TINY_BYTES}B poison-guard floor"
        if reason is not None:
            findings.append(
                Finding(
                    rule="CACHE-005",
                    path=f"cache:{entry.key}",
                    cache_id=entry.id,
                    message=f"cache id={entry.id} key {entry.key!r} looks poisoned: {reason}",
                    fix="delete the exact key (ci-lint cache heal --key <key>) so the next writer run "
                    "repopulates it with a real payload",
                )
            )
    return findings


def _check_cache_006(ci: CiToml, classified: tuple[ClassifiedEntry, ...]) -> list[Finding]:
    groups: dict[_Generation, list[ClassifiedEntry]] = {}
    for c in classified:
        if c.family_id is None or c.is_delta or c.is_retired:
            continue
        # An `evict = "lru"` family is bounded by its footprint instead
        # (#421): its keys may be content-addressed (soldr verdicts), so
        # entries sharing a stripped shape are all live, not generations.
        fam = ci.cache.family.get(c.family_id)
        if fam is not None and fam.evict == "lru":
            continue
        groups.setdefault(_Generation(c.family_id, _shape(c.entry.key), c.pr if c.pr_in_key else None), []).append(c)

    findings: list[Finding] = []
    for generation, members in groups.items():
        fam_id = generation.family_id
        if len(members) < 2:
            continue
        newest = max(members, key=lambda c: c.entry.last_accessed_at)
        for c in members:
            if c.entry.id == newest.entry.id:
                continue
            findings.append(
                Finding(
                    rule="CACHE-006",
                    path=f"cache:{c.entry.key}",
                    cache_id=c.entry.id,
                    message=f"cache id={c.entry.id} is a superseded '{fam_id}' entry "
                    f"(last_accessed_at={c.entry.last_accessed_at!r}; kept newest "
                    f"id={newest.entry.id} last_accessed_at={newest.entry.last_accessed_at!r})",
                    fix="delete the superseded entry (ci-lint cache janitor); if this family "
                    "legitimately needs more than one live entry at once, disambiguate it with "
                    "[cache.family.<id>].per instead of relying on two same-shape keys",
                )
            )
    return findings


# ── zackees/ci.yml#222: same-lock toolchain generations ─────────────────────
#
# A soldr release changes setup-soldr's toolchain-signature digest and the
# cook-base `soldr` component WITHOUT touching Cargo.lock, so one lock grows
# two full cache sets and every trailing-hash shape keeps them apart
# (zccache#1875: 12.41 GB live, pre-prune blind, GitHub LRU the only thing
# that moved). zccache#1876 adopted the rule; this is its ci-lint form: a
# base entry created BEFORE the generation epoch -- the earliest
# default-branch `soldr-mini` save of the highest present soldr version,
# less 10 minutes of slack -- is dead weight once a replacement of its
# restore family exists AFTER the epoch. Entries created after the epoch are
# never targets, so concurrent job shapes are untouched; a soldr rollback
# re-seeds the epoch on the way back down (documented limitation).

_EPOCH_SLACK_SECONDS = 600.0
_MINI_VERSION_RE = re.compile(r"-v(\d+\.\d+\.\d+)$")
_BUILDCACHE_RE = re.compile(
    r"^setup-soldr-buildcache-v\d+-(?P<os>[^-]+)-(?P<arch>[^-]+)-"
    r"(?P<digest>[0-9a-f]{8,64})(?:-(?P<suffix>.+?))?-(?P<lock>[0-9a-f]{16,64})$"
)
_COOK_RE = re.compile(
    r"^cook-base-v\d+-(?P<plat>.+?)-rustc(?P<rustc>\d+\.\d+\.\d+)-[^-]+-"
    r"(?P<lock>l[0-9a-f]{16,64})-soldrv(?P<soldr>\d+\.\d+\.\d+)$"
)
_DYLINT_PREFIXES = (
    "setup-soldr-dylint-v1-", "setup-soldr-dylint-v2-",
    "setup-soldr-dylint-output-v1-", "setup-soldr-dylint-output-v2-",
)


@dataclass(frozen=True)
class _SameLockFamily:
    """One entry parsed into the restore-family identity #222 compares on."""

    kind: str  # "buildcache" | "dylint" | "registry" | "cook"
    group: str  # restore-family id (kind-specific; cook groups per platform)
    version: tuple[int, ...] | None  # cook only: (soldr..., rustc...)


@dataclass(frozen=True)
class _TimedEntry:
    """A candidate with its parsed creation time -- a typed record (PY-002),
    never a positional tuple, carrying its parsed family for cook verdicts."""

    entry: ClassifiedEntry
    created: float
    family: _SameLockFamily | None = None


@dataclass(frozen=True)
class _ShapeGroup:
    """One restore family's candidates (#222): `family` is `kind:group`."""

    family: str
    members: tuple[_TimedEntry, ...]


@dataclass(frozen=True)
class _SameLockCandidates:
    """The two candidate sets of #222, as a typed record (PY-002: no tuple
    returns, no dict fields -- homogeneous sequences only)."""

    shape_groups: tuple[_ShapeGroup, ...]
    cook: tuple[_TimedEntry, ...]


def _same_lock_family(key: str) -> _SameLockFamily | None:
    """Parse a key into its same-lock generation family, or None when the
    shape is not one of the four #222 knows -- never guessed, never
    evaluated (fail closed to today's behavior)."""

    match = _BUILDCACHE_RE.match(key)
    if match is not None:
        suffix = match.group("suffix") or ""
        return _SameLockFamily(
            "buildcache", f"{match.group('os')}/{match.group('arch')}/{suffix}/{match.group('lock')}", None
        )
    if key.startswith(_DYLINT_PREFIXES):
        # The digest trails (or is the whole tail): stripping it yields the
        # restore family; identical keys still group as their own family.
        return _SameLockFamily("dylint", _TRAILING_HASH_RE.sub("", key) or key, None)
    match = _CARGO_REGISTRY_RE.match(key)
    if match is not None:
        # head + the lock it stops at: the soldr/toolchain tail (the part a
        # release rewrites; #88 keeps it in the CACHE-006 shape) drops out,
        # so same-lock generations group here and cross-lock never do.
        return _SameLockFamily("registry", key[: len(match.group("head")) + 16], None)
    match = _COOK_RE.match(key)
    if match is not None:
        soldr = tuple(int(part) for part in match.group("soldr").split("."))
        rustc = tuple(int(part) for part in match.group("rustc").split("."))
        return _SameLockFamily("cook", match.group("plat"), (*soldr, *rustc))
    return None


def _created_seconds(value: str) -> float | None:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _toolchain_epoch(classified: tuple[ClassifiedEntry, ...], default_ref: str) -> float | None:
    """The generation epoch (#222): earliest default-branch `soldr-mini` save
    of the HIGHEST present soldr version, less slack. No mini entries ->
    no epoch -> the whole check stays silent."""

    created: dict[tuple[int, ...], float] = {}
    for c in classified:
        if c.entry.ref != default_ref or not c.entry.key.startswith("soldr-mini-"):
            continue
        match = _MINI_VERSION_RE.search(c.entry.key)
        if match is None:
            continue
        stamp = _created_seconds(c.entry.created_at)
        if stamp is None:
            continue
        version = tuple(int(part) for part in match.group(1).split("."))
        created[version] = min(created.get(version, stamp), stamp)
    if not created:
        return None
    return created[max(created)] - _EPOCH_SLACK_SECONDS


def _same_lock_finding(c: ClassifiedEntry, kind: str, epoch: float, detail: str) -> Finding:
    when = datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    return Finding(
        rule="CACHE-006",
        path=f"cache:{c.entry.key}",
        cache_id=c.entry.id,
        message=f"cache id={c.entry.id} is a same-lock {kind} generation superseded by the toolchain epoch "
        f"(created_at={c.entry.created_at!r} < epoch {when}: {detail}; zccache#1876)",
        fix="delete the superseded generation (ci-lint cache janitor; preprune must retire these BEFORE its "
        "forecast): current producers restore the post-epoch replacement, so it is dead weight on the "
        "current lock (zackees/ci.yml#222)",
    )


def _same_lock_candidates(
    ci: CiToml, classified: tuple[ClassifiedEntry, ...], default_ref: str
) -> _SameLockCandidates:
    """Collect same-lock candidates: base, default-ref, non-lru entries of
    the four known shapes, grouped by restore family (cook across locks)."""

    groups: dict[str, list[_TimedEntry]] = {}
    cook: list[_TimedEntry] = []
    for c in classified:
        if c.family_id is None or c.is_delta or c.is_retired or c.pr is not None or c.entry.ref != default_ref:
            continue
        fam = ci.cache.family.get(c.family_id)
        if fam is not None and fam.evict == "lru":
            continue
        parsed = _same_lock_family(c.entry.key)
        stamp = _created_seconds(c.entry.created_at) if parsed is not None else None
        if parsed is None or stamp is None:
            continue
        if parsed.kind == "cook":
            cook.append(_TimedEntry(c, stamp, parsed))
        else:
            groups.setdefault(f"{parsed.kind}:{parsed.group}", []).append(_TimedEntry(c, stamp, parsed))
    shape_groups = tuple(_ShapeGroup(key, tuple(members)) for key, members in sorted(groups.items()))
    return _SameLockCandidates(shape_groups, tuple(cook))


def _superseded_shape_findings(epoch: float, shape_groups: tuple[_ShapeGroup, ...]) -> list[Finding]:
    """Pre-epoch members of a family whose replacement exists after the epoch."""

    findings: list[Finding] = []
    for group in shape_groups:
        replacements = [item for item in group.members if item.created > epoch]
        if not replacements:
            continue  # the producer has not re-seeded this family yet
        newest = max(replacements, key=lambda item: item.created)
        for item in group.members:
            if item.created >= epoch:
                continue  # post-epoch entries (incl. concurrent shapes) are never targets
            kind = group.family.partition(":")[0]
            detail = f"replacement id={newest.entry.entry.id} created_at={newest.entry.entry.created_at!r}"
            findings.append(_same_lock_finding(item.entry, kind, epoch, detail))
    return findings


def _superseded_cook_findings(epoch: float, cook: tuple[_TimedEntry, ...]) -> list[Finding]:
    """Cook bases: a strictly newer (soldr, rustc) after the epoch, any lock."""

    findings: list[Finding] = []
    for item in cook:
        version = item.family.version if item.family is not None else None
        if item.created >= epoch or version is None:
            continue
        newer = [
            other for other in cook
            if other.created > epoch
            and other.family is not None and other.family.version is not None
            and other.family.version > version
        ]
        if not newer:
            continue  # no strictly newer version exists after the epoch (rollback-safe)
        best = max(newer, key=lambda other: (other.family.version if other.family else (), other.created))
        detail = (
            f"newer cook base (soldr/rustc {best.family.version if best.family else None}, "
            f"created_at={best.entry.entry.created_at!r})"
        )
        findings.append(_same_lock_finding(item.entry, "cook", epoch, detail))
    return findings


def _check_cache_006_same_lock(
    ci: CiToml, classified: tuple[ClassifiedEntry, ...], default_branch: str
) -> list[Finding]:
    """#222: same-lock toolchain generations, via the zccache#1876 rule."""

    default_ref = f"refs/heads/{default_branch}"
    epoch = _toolchain_epoch(classified, default_ref)
    if epoch is None:
        return []
    candidates = _same_lock_candidates(ci, classified, default_ref)
    return [
        *_superseded_shape_findings(epoch, candidates.shape_groups),
        *_superseded_cook_findings(epoch, candidates.cook),
    ]


# GEN-009 (issue #5): a fleet-level rollup, not a new detection signal.
# policy-general.md already says CACHE-003 (unreachable-ref saves),
# CACHE-004 (declared byte-budget proof), CACHE-006 (superseded
# generations), and CACHE-008 (closed-PR/stale-delta trim) implement its
# mechanics "under those IDs rather than this one" -- so GEN-009 is the
# single "is this repository's live cache state healthy" verdict a human
# or agent reads instead of grepping four rule IDs separately. It fires
# when the current audit already produced >= 1 finding from that rollup
# set; it emits NOTHING new that CACHE-003/004/006/008 didn't already
# find, and it clears the moment none of them fire, so it can never be
# "fixed" independently of its contributors (no allowlist escape hatch).
GEN_009_ROLLUP_RULES: frozenset[str] = frozenset({"CACHE-003", "CACHE-004", "CACHE-006", "CACHE-008"})


def _check_gen_009(
    contributing: list[Finding], *, total_bytes: int, budget_bytes: int | None
) -> Finding | None:
    if not contributing:
        return None
    counts: dict[str, int] = {}
    for f in contributing:
        counts[f.rule] = counts.get(f.rule, 0) + 1
    by_rule = ", ".join(f"{rule}={counts[rule]}" for rule in sorted(counts))
    budget_str = f"{total_bytes}B" + (f" / declared budget {budget_bytes}B" if budget_bytes is not None else "")
    return Finding(
        rule="GEN-009",
        path="cache:fleet-budget",
        message=f"fleet cache budget rollup: {len(contributing)} contributing finding(s) across "
        f"{len(counts)} rule(s) ({by_rule}); live usage {budget_str}",
        fix="this is a rollup, not an independent defect -- fix each listed CACHE-003/004/006/008 "
        "finding individually (ci-lint cache janitor reclaims what it safely can); GEN-009 clears "
        "once none of its contributing rules fire, never by suppressing GEN-009 itself",
    )


def _check_cache_009(classified: tuple[ClassifiedEntry, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for c in classified:
        if c.is_retired:
            findings.append(
                Finding(
                    rule="CACHE-009",
                    path=f"cache:{c.entry.key}",
                    cache_id=c.entry.id,
                    message=f"cache id={c.entry.id} key {c.entry.key!r} matches a retired family "
                    "([cache].retired)",
                    fix="delete it (ci-lint cache janitor); a retired family must never be written -- "
                    "check the writer flow's setup-soldr wrapper inputs for what is still producing it",
                )
            )
    return findings


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _is_dylint_job_name(name: str) -> bool:
    """The template's job id is literally `dylint`, but its DISPLAY name
    carries the lane-digest suffix `ci_lint.plan`'s title-edit reuse relies
    on (e.g. `"dylint [309e89b739f0]"`) -- match the prefix, not equality."""

    return name == "dylint" or name.startswith("dylint ") or name.startswith("dylint[")


@dataclass(frozen=True)
class _DylintRunEvidence:
    run_id: int
    html_url: str


def _find_default_branch_dylint_run(
    fetch: FetchFn, token: str, repo: str, default_branch: str
) -> _DylintRunEvidence | None:
    """The default branch's most recent run (event=push, its own top-level
    conclusion=success) that has a successful `dylint`-named job --
    `None` when no such run/job is found, so RUST-004 has nothing to
    correlate a missing cache entry against yet (never a false positive on
    a repo/branch that has simply never run Dylint successfully). Matches
    `ci_lint.reuse.compute_reuse`'s "no network in tests, inject FetchFn"
    convention; raises `GitHubApiError` on a transport/HTTP failure, same
    as every other live call in this module."""

    payload = fetch(
        f"https://api.github.com/repos/{repo}/actions/runs"
        f"?branch={default_branch}&event=push&status=success&per_page={_RUST_004_RUNS_PER_PAGE}",
        token,
    )
    runs = _as_list(_as_dict(payload).get("workflow_runs"))
    for raw in runs:
        run = _as_dict(raw)
        run_id = run.get("id")
        html_url = run.get("html_url")
        if not isinstance(run_id, int):
            continue
        jobs_payload = fetch(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
            f"?per_page={_RUST_004_JOBS_PER_PAGE}",
            token,
        )
        for raw_job in _as_list(_as_dict(jobs_payload).get("jobs")):
            job = _as_dict(raw_job)
            name = job.get("name")
            conclusion = job.get("conclusion")
            if not isinstance(name, str) or not _is_dylint_job_name(name):
                continue
            if conclusion != "success":
                continue
            return _DylintRunEvidence(run_id=run_id, html_url=html_url if isinstance(html_url, str) else "")
    return None


def _check_rust_004(
    ci: CiToml,
    classified: tuple[ClassifiedEntry, ...],
    *,
    fetch: FetchFn,
    token: str,
    repo: str,
    default_branch: str,
) -> _RuleResult:
    families = sorted(fam_id for fam_id, fam in ci.cache.family.items() if fam.via in DYLINT_CACHE_VIA_VALUES)
    if not families:
        return _RuleResult((), None)

    try:
        run = _find_default_branch_dylint_run(fetch, token, repo, default_branch)
    except GitHubApiError as exc:
        return _RuleResult((), f"RUST-004: could not confirm the default branch's latest successful dylint job: {exc}")
    if run is None:
        return _RuleResult((), None)

    default_ref = f"refs/heads/{default_branch}"
    findings: list[Finding] = []
    for fam_id in families:
        on_default = [
            c
            for c in classified
            if c.family_id == fam_id and not c.is_delta and not c.is_retired and c.entry.ref == default_ref
        ]
        # NOT "created_at >= the job's own started_at": a live run against
        # the real template (round-6E) proved that signal false-positives
        # on an entirely legitimate case -- setup-soldr's dylint-output key
        # is a hash over (among other things) the workspace's manifest/
        # target-shape/compiler identity, NOT the commit SHA alone, so an
        # unrelated commit (e.g. a docs/workflow-only change) reproduces the
        # IDENTICAL key and correctly gets an "exact hit - skipping save"
        # (confirmed in zackees/template-python-rust-cmd run 36514507620's
        # own job log) -- a perfectly healthy warm cache, not a missed
        # save. The only signal ci-lint can trust without literally
        # recomputing that hash is "does ANY live entry for this family
        # exist on the default branch at all" -- exactly the failure mode
        # issue #1 actually observed (zackees/clud: zero cache layers
        # reused on either sampled run).
        if on_default:
            continue
        via = ci.cache.family[fam_id].via
        run_ref = run.html_url or f"run id={run.run_id}"
        findings.append(
            Finding(
                rule="RUST-004",
                path=f"cache:family:{fam_id}",
                message=f"successful Dylint run never saved its output cache (see setup-soldr#538/#540): "
                f"{run_ref}'s 'dylint' job succeeded on {default_ref!r}, but family '{fam_id}' ({via}) "
                "has no live entry at all on the default branch",
                fix=f"inspect {run_ref}'s Dylint job log for its 'Setup soldr' Post-job step -- "
                "setup-soldr's save gate compares a QUALIFIED nightly identity (e.g. "
                "'nightly-2026-05-28-x86_64-unknown-linux-gnu') against a short one and can silently "
                "skip saving on a mismatch (setup-soldr#538, fixed in v0.9.81+); confirm setup-soldr is "
                f"pinned to >= v0.9.81 and that the job actually reports a cache save (not just a hit) "
                f"for '{fam_id}'",
            )
        )
    return _RuleResult(tuple(findings), None)


def _current_base_entries(classified: tuple[ClassifiedEntry, ...]) -> _BaseEntries:
    out: dict[str, list[ClassifiedEntry]] = {}
    for c in classified:
        if c.family_id is not None and not c.is_delta and not c.is_retired and not c.pr_in_key:
            out.setdefault(c.family_id, []).append(c)
    return _BaseEntries(tuple(_FamilyEntries(fam, tuple(members)) for fam, members in out.items()))


def _check_cache_008(  # noqa: C901
    classified: tuple[ClassifiedEntry, ...],
    *,
    graphql: GraphQLFn | None,
    token: str,
    repo: str,
) -> _Cache008Result:
    """Closed/merged-PR entries (every entry whose key carries `pr-<N>`,
    a legacy `delta-v1-pr<N>-` key, or ref `refs/pull/<N>/merge` -- #23
    section 4.1) plus stale-base PR deltas. ONE GraphQL query for every PR
    number. Returns (findings, warning, ids of closed-PR entries)."""

    pr_entries = [c for c in classified if c.pr is not None]
    if not pr_entries:
        return _Cache008Result((), None, frozenset())

    warning: str | None = None
    pr_states: dict[int, PrState] = {}
    if graphql is not None:
        pr_numbers = sorted({c.pr for c in pr_entries if c.pr is not None})
        try:
            owner, name = repo.split("/", 1)
            pr_states = fetch_pr_states(graphql, token, owner, name, pr_numbers)
        except CacheApiError as exc:
            warning = f"CACHE-008 PR-state lookup: {exc} (fail safe: every PR's entries are kept)"

    base_entries = _current_base_entries(classified)
    findings: list[Finding] = []
    closed_ids: set[int] = set()
    for c in pr_entries:
        assert c.pr is not None
        state = pr_states.get(c.pr)
        if state is not None and state.closed_or_merged:
            closed_ids.add(c.entry.id)
            if c.is_delta:
                kind = "a PR delta"
            elif c.pr_in_key:
                kind = "a pr-<N>-keyed entry"
            else:
                kind = "an entry on its merge ref"
            findings.append(
                Finding(
                    rule="CACHE-008",
                    path=f"cache:{c.entry.key}",
                    cache_id=c.entry.id,
                    message=f"cache id={c.entry.id} is {kind} for #{c.pr}, which is {state.state.lower()}",
                    fix="delete it (ci-lint cache janitor or trim); the janitor deletes every entry of a "
                    "closed/merged PR on its next sweep",
                )
            )
            continue

        d = c.delta
        if d is None:
            continue
        candidates = base_entries.get(d.family)
        matching = [b for b in candidates if d.platform in b.entry.key] or candidates
        if not matching:
            continue  # no live base entry to compare against: unknown, not reported
        current_base8 = {hashlib.sha256(b.entry.key.encode("utf-8")).hexdigest()[:8] for b in matching}
        if d.base8 not in current_base8:
            findings.append(
                Finding(
                    rule="CACHE-008",
                    path=f"cache:{c.entry.key}",
                    cache_id=c.entry.id,
                    message=f"cache id={c.entry.id} is a PR #{d.pr} delta for family '{d.family}' "
                    f"whose base hash b{d.base8} matches none of the live base entries' current keys",
                    fix="delete it (ci-lint cache trim); the next PR push rebuilds the delta against "
                    "the current base and saves fresh",
                )
            )
    return _Cache008Result(tuple(findings), warning, frozenset(closed_ids))


def _check_family_live_excess(
    ci: CiToml, classified: tuple[ClassifiedEntry, ...], default_branch: str
) -> list[Finding]:
    """CACHE-004 (live, ci.yml#139): compare each declared family's LIVE
    default-branch entries with the model CACHE-004's static proof sums --
    at most `cardinality(per)` entries (doubled for a `lockfile = true`
    family's lockfile-change peak), each at most `max`. Restore-only
    leftovers from an older writer shape (kernal-api: 3 compile, 7
    toolchain, 3 soldr-mini entries against `per = "none"`, 5.45 GB live
    vs a 2.97 GB proven worst case) otherwise pass every other live rule
    while silently breaking the static proof. PR/delta entries are out of
    scope (CACHE-008 owns them)."""

    default_ref = f"refs/heads/{default_branch}"
    findings: list[Finding] = []
    for fam_id, fam in ci.cache.family.items():
        live = [
            c
            for c in classified
            if c.family_id == fam_id
            and not c.is_delta
            and c.pr is None
            and (not c.entry.ref or c.entry.ref == default_ref)
        ]
        if not live:
            continue
        # A family that declares `shapes` (ci.yml#334) budgets the SUM of
        # the shape ceilings, and CACHE-004's static proof uses that same
        # number. Reading `per`/`max` here instead made the live audit
        # disagree with the static proof in BOTH directions: a shapes family
        # was charged `max x cardinality(per)` entries it never declared
        # (zackees/wild's `cook`: 3 live entries against a modelled 2, a
        # spurious violation), and the "declared worst case" in the message
        # was the wrong number. Same class as the janitor gap fixed in
        # #343: one footprint, one implementation.
        declared_count = cardinality(ci, fam.per, len(fam.shapes)) * (2 if fam.lockfile else 1)
        max_bytes = parse_size(fam.max) if fam.max else None
        # Per-entry ceiling: with `shapes`, the binding number is the largest
        # shape ceiling, not the family's single `max` -- `max` is only the
        # fallback when a family is uniformly sized.
        entry_ceiling = max(
            (parse_size(sh.max) or 0 for sh in fam.shapes), default=max_bytes
        )
        oversized = [
            c for c in live if entry_ceiling is not None and c.entry.size_in_bytes > entry_ceiling
        ]
        live_bytes = sum(c.entry.size_in_bytes for c in live)
        count_excess = len(live) > declared_count
        if count_excess and fam.evict == "lru" and not fam.shapes and max_bytes is not None:
            # An `evict = "lru"` family is bounded in BYTES, not entries: the
            # janitor LRU-evicts it to `max x cardinality` and its keys may be
            # content-addressed (#421, the CACHE-006 exemption). Many small
            # entries inside that footprint keep the static proof intact
            # (kernal-api's soldr-verdict: 225 entries, 106 KB vs 1 MB).
            count_excess = live_bytes > declared_count * max_bytes
        if not count_excess and not oversized:
            continue
        problems: list[str] = []
        if count_excess:
            peak = ", x2 lockfile peak" if fam.lockfile else ""
            problems.append(
                f"{len(live)} live entries on {default_ref} but the model allows {declared_count} "
                f"(per = {fam.per or 'none'!r}{peak})"
            )
        if oversized:
            ids = ", ".join(f"id={c.entry.id} {c.entry.size_in_bytes}B" for c in oversized)
            problems.append(f"{len(oversized)} entrie(s) exceed max = {fam.max!r} ({ids})")
        declared_bytes = (
            family_footprint(ci, fam) * (2 if fam.lockfile else 1)
            if fam.shapes
            else (declared_count * max_bytes if max_bytes is not None else None)
        )
        bound = f" vs a declared worst case of {declared_bytes}B" if declared_bytes is not None else ""
        # The static proof is actually broken only when the family's live
        # bytes exceed what it budgets; a shape mismatch that still fits
        # the bytes is surfaced for review, not failed.
        broken = declared_bytes is not None and live_bytes > declared_bytes
        findings.append(
            Finding(
                rule="CACHE-004",
                path=f"cache:family:{fam_id}",
                status=Status.VIOLATION if broken else Status.NEEDS_REVIEW,
                message=f"[cache.family.{fam_id}] live footprint {live_bytes}B{bound} no longer matches "
                f"its declared model: {'; '.join(problems)}",
                fix="the static CACHE-004 proof undercounts this family: delete the leftover entries "
                "from older writer shapes ('ci-lint cache janitor' once they go unaccessed, or "
                f"'ci-lint cache heal --key <key> --ref {default_ref}'), or correct "
                f"[cache.family.{fam_id}]'s per/max to the shapes the writers really save",
            )
        )
    return findings


def infer_default_branch(fetch: FetchFn | None, token: str, repo: str) -> str | None:
    """zackees/ci.yml#425: the repository knows its own default branch;
    never guess `main`. Order: the Actions event payload
    (`$GITHUB_EVENT_PATH` -> `repository.default_branch`, zero API calls),
    then one read-only `GET /repos/{repo}`. Unresolvable -> `None`, and the
    caller skips the default-branch-relative checks behind one needs_review
    finding instead of emitting a guessed one (obs-studio's `master` drew
    33 false CACHE-003 under the old `main` assumption)."""

    payload_path = os.environ.get("GITHUB_EVENT_PATH")
    if payload_path:
        try:
            with open(payload_path, encoding="utf-8") as fh:
                data: JsonValue = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            repository = data.get("repository")
            if isinstance(repository, dict):
                branch = repository.get("default_branch")
                if isinstance(branch, str) and branch:
                    return branch
    if fetch is not None and token and repo:
        try:
            answer = fetch(f"https://api.github.com/repos/{repo}", token)
        except GitHubApiError:
            return None
        if isinstance(answer, dict):
            branch = answer.get("default_branch")
            if isinstance(branch, str) and branch:
                return branch
    return None


def audit_classified(
    ci: CiToml,
    classified: tuple[ClassifiedEntry, ...],
    *,
    graphql: GraphQLFn | None,
    token: str,
    repo: str,
    default_branch: str | None = "main",
    fetch: FetchFn | None = None,
) -> AuditReport:
    """The pure-ish half: findings from an already-fetched cache listing.
    Split from `run_audit` so `ci_lint.cache.ops` can classify once and
    reuse it for both the findings and the deletions. `fetch` is optional
    and keyword-only, defaulting to `None` (RUST-004 skipped, no runs/jobs
    lookup, identical output to before round-6E) -- `ci_lint.cache.ops`'s
    trim/janitor/preprune classify+audit for their OWN rule subsets (none
    of which include RUST-004, which never deletes anything) and so never
    pass it, keeping their live-call count unchanged; `run_audit` (used by
    `ci-lint cache audit` and `precheck --live`) always has a `fetch` in
    hand already (it just used it to list caches) and passes it through."""

    findings: list[Finding] = []
    if default_branch is None:
        # #425: the CLI default is "infer"; explicit callers (including the
        # legacy `"main"` default above) never reach this line.
        default_branch = infer_default_branch(fetch, token, repo)
    findings.extend(_check_cache_001(classified))
    if default_branch is None:
        findings.append(
            Finding(
                rule="CACHE-003",
                path="cache:default-branch",
                status=Status.NEEDS_REVIEW,
                message="default branch could not be determined; skipped CACHE-003, "
                "CACHE-004's live-excess check and RUST-004 rather than assume 'main' (#425)",
                fix="pass --default-branch, or run where $GITHUB_EVENT_PATH (GitHub Actions) "
                "or one read-only GET /repos/<owner>/<name> can resolve it",
            )
        )
    else:
        findings.extend(_check_cache_003(classified, default_branch))
    findings.extend(_check_cache_005(ci, classified))
    findings.extend(_check_cache_006(ci, classified))
    findings.extend(_check_cache_009(classified))
    if default_branch is not None:
        findings.extend(_check_family_live_excess(ci, classified, default_branch))
        findings.extend(_check_cache_006_same_lock(ci, classified, default_branch))
    cache008 = _check_cache_008(classified, graphql=graphql, token=token, repo=repo)
    findings.extend(cache008.findings)
    warning = cache008.warning
    closed_pr_ids = cache008.closed_pr_ids

    if fetch is not None and default_branch is not None:
        rust004 = _check_rust_004(ci, classified, fetch=fetch, token=token, repo=repo, default_branch=default_branch)
        findings.extend(rust004.findings)
        if rust004.warning and warning is None:
            warning = rust004.warning

    total_bytes = sum(c.entry.size_in_bytes for c in classified)
    budget_bytes = parse_size(ci.cache.budget) if ci.cache.budget else None
    ratio = (total_bytes / budget_bytes) if budget_bytes else None
    if ratio is not None and ratio >= BUDGET_WARN_RATIO:
        findings.append(
            Finding(
                rule="CACHE-004",
                path="cache:budget",
                message=f"live cache usage {total_bytes}B is {ratio:.0%} of [cache].budget "
                f"({ci.cache.budget})",
                fix="run ci-lint cache janitor to reclaim undeclared/superseded/stale entries, or "
                "raise [cache].budget (re-proving CACHE-004's static arithmetic still fits)",
            )
        )

    gen_009 = _check_gen_009(
        [f for f in findings if f.rule in GEN_009_ROLLUP_RULES], total_bytes=total_bytes, budget_bytes=budget_bytes
    )
    if gen_009 is not None:
        findings.append(gen_009)

    return AuditReport(
        classified=classified,
        findings=tuple(findings),
        total_bytes=total_bytes,
        budget_bytes=budget_bytes,
        budget_ratio=ratio,
        warning=warning,
        closed_pr_ids=closed_pr_ids,
    )


class AuditError(Exception):
    """Listing caches itself failed (network/auth) -- distinct from a
    finding, since there is nothing to classify at all. Callers turn this
    into a needs_review, never a crash."""


def run_audit(
    ci: CiToml,
    *,
    fetch: FetchFn,
    graphql: GraphQLFn | None,
    token: str,
    repo: str,
    default_branch: str | None = "main",
) -> AuditReport:
    try:
        entries = list_caches(fetch, token, repo)
    except CacheApiError as exc:
        raise AuditError(str(exc)) from exc
    classified = classify(ci, entries)
    return audit_classified(
        ci, classified, graphql=graphql, token=token, repo=repo, default_branch=default_branch, fetch=fetch
    )


def to_json_dict(report: AuditReport) -> dict[str, JsonValue]:
    return {
        "entries": [
            {
                "id": c.entry.id,
                "key": c.entry.key,
                "ref": c.entry.ref,
                "size_in_bytes": c.entry.size_in_bytes,
                "last_accessed_at": c.entry.last_accessed_at,
                "family": c.family_id,
                "is_delta": c.is_delta,
                "is_retired": c.is_retired,
            }
            for c in report.classified
        ],
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
        "total_bytes": report.total_bytes,
        "budget_bytes": report.budget_bytes,
        "budget_ratio": report.budget_ratio,
        "warning": report.warning,
    }


def render_text(report: AuditReport) -> str:
    lines: list[str] = []
    by_family: dict[str, _FamilyTally] = {}
    for c in report.classified:
        label = c.family_id or ("delta" if c.is_delta else ("retired" if c.is_retired else "undeclared"))
        tally = by_family.setdefault(label, _FamilyTally())
        tally.count += 1
        tally.size += c.entry.size_in_bytes
    lines.append(f"ci-lint cache audit: {len(report.classified)} live cache entrie(s), {report.total_bytes}B total")
    lines.append(f"{'family':<24} {'count':>6} {'bytes':>12}")
    for label in sorted(by_family):
        tally = by_family[label]
        lines.append(f"{label:<24} {tally.count:>6} {tally.size:>12}")
    if report.budget_bytes is not None:
        pct = f"{report.budget_ratio:.1%}" if report.budget_ratio is not None else "?"
        lines.append(f"budget: {report.total_bytes}B / {report.budget_bytes}B ({pct})")
    lines.append("")
    if not report.findings:
        lines.append("ci-lint cache audit: no findings.")
    for f in report.findings:
        lines.append(f.render())
    if report.warning:
        lines.append(f"warning: {report.warning}")
    return "\n".join(lines)
