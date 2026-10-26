"""ACT-001 (round M2-20, zackees/ci.yml#44 part A): audit a local `act`
cache-server store against the same remote cache policy `[cache].budget`/
`[cache].retired` govern -- "local caches obey remote policy" (issue #6
§11, rule 4). This is the zccache#1760 fixture (a retired family silently
re-enabled) reproduced against a local act cache instead of the live
GitHub Actions cache API.

Ported (round M2-20) from `template-python-rust-cmd`'s
`ci/localrun/cache_audit.py`, which stays as the template's own copy for
now (the template's `ci/local.py act` calls it directly); this module is
`ci_lint`'s standard-library-only, fleet-wide version so any repository
can run `python3 -m ci_lint act audit` without needing that template
script. Keep both in sync if act's on-disk cache-server format changes.

act's built-in Actions-cache-server (`--cache-server-path`) persists a
bbolt (Go embedded key/value store) index at `<path>/bolt.db`, plus raw
payload blobs under `<path>/cache/**`. bbolt never compresses or encrypts
a value, so every index record round-trips through the store as one
literal JSON object, recoverable with a direct byte-level regex scan of
`bolt.db`. Because bbolt is a copy-on-write B+tree, stale pre-write copies
of a record can still be present in the file; entries are deduplicated by
`id`, preferring any sighting with `"complete":true` (an entry only ever
transitions incomplete -> complete, never back) and otherwise the most
recently `usedAt` copy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ci_lint.cargo_messages import JsonValue

_RECORD_RE = re.compile(
    rb'\{"id":\d+,"key":"[^"]*","version":"[^"]*","cacheSize":\d+,'
    rb'"complete":(?:true|false),"usedAt":\d+,"createdAt":\d+\}'
)

RULE = "ACT-001"

@dataclass(frozen=True)
class _SizeUnit:
    factor: int
    suffix: str


_SIZE_UNITS: tuple[_SizeUnit, ...] = (
    _SizeUnit(1024**3, "GB"),
    _SizeUnit(1024**2, "MB"),
    _SizeUnit(1024, "KB"),
)


def format_size(num_bytes: int) -> str:
    for unit in _SIZE_UNITS:
        if num_bytes >= unit.factor:
            return f"{num_bytes / unit.factor:.2f}{unit.suffix}"
    return f"{num_bytes}B"


@dataclass(frozen=True)
class CacheIndexEntry:
    id: int
    key: str
    cache_size: int
    complete: bool
    used_at: int


@dataclass(frozen=True)
class RetiredHit:
    entry: CacheIndexEntry
    family: str  # the matched retired family


@dataclass(frozen=True)
class ActCacheAuditResult:
    store_path: Path
    disk_bytes: int  # ground truth: real bytes on disk under cache/
    budget_bytes: int
    entries: tuple[CacheIndexEntry, ...]  # deduplicated, complete-only
    retired_hits: tuple[RetiredHit, ...]
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.findings


def _read_index(bolt_db: Path) -> list[CacheIndexEntry]:
    if not bolt_db.is_file():
        return []
    data = bolt_db.read_bytes()
    by_id: dict[int, CacheIndexEntry] = {}
    for m in _RECORD_RE.finditer(data):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        entry = CacheIndexEntry(
            id=obj["id"],
            key=obj["key"],
            cache_size=obj["cacheSize"],
            complete=obj["complete"],
            used_at=obj["usedAt"],
        )
        prev = by_id.get(entry.id)
        if prev is None:
            by_id[entry.id] = entry
        elif entry.complete and not prev.complete:
            by_id[entry.id] = entry
        elif entry.complete == prev.complete and entry.used_at >= prev.used_at:
            by_id[entry.id] = entry
    return [entry for entry in by_id.values() if entry.complete]


def _disk_bytes(cache_dir: Path) -> int:
    total = 0
    blobs = cache_dir / "cache"
    if not blobs.is_dir():
        return 0
    for path in blobs.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def audit(store_path: Path, *, budget_bytes: int, retired_families: tuple[str, ...]) -> ActCacheAuditResult:
    """Audit `store_path` (an act `--cache-server-path` directory).

    Two independent checks, both sourced from `ci.toml [cache]`:
      1. total on-disk bytes <= budget
      2. no complete cache-index entry's key names a retired family
         (substring match, case-insensitive -- the same convention
         `ci_lint`'s static CACHE-009 check uses for the same list)
    """

    entries = tuple(_read_index(store_path / "bolt.db"))
    disk_bytes = _disk_bytes(store_path)

    findings: list[str] = []
    retired_hits: list[RetiredHit] = []
    for entry in entries:
        key_norm = entry.key.lower()
        for family in retired_families:
            if family.lower() in key_norm:
                retired_hits.append(RetiredHit(entry, family))
                findings.append(
                    f"{RULE}: local act cache saved key {entry.key!r} "
                    f"({format_size(entry.cache_size)}), which matches retired family "
                    f"{family!r} (ci.toml [cache].retired). Fix: remove whatever input "
                    f"re-enabled '{family}' (see CACHE-009's setup-soldr wrapper check) "
                    f"and re-run the local act loop with an empty act cache volume, or "
                    f"garbage-collect the machine-scoped act-cache-server volume."
                )

    if disk_bytes > budget_bytes:
        findings.append(
            f"{RULE}: local act cache store at {store_path} is {format_size(disk_bytes)}, "
            f"over ci.toml [cache].budget ({format_size(budget_bytes)}). Fix: this mirrors "
            f"what the remote budget proof (CACHE-004) checks in CI -- shrink a "
            f"[cache.family].max, or garbage-collect the machine-scoped act-cache-server "
            f"volume and start warm caching over."
        )

    return ActCacheAuditResult(
        store_path=store_path,
        disk_bytes=disk_bytes,
        budget_bytes=budget_bytes,
        entries=entries,
        retired_hits=tuple(retired_hits),
        findings=tuple(findings),
    )


def render(result: ActCacheAuditResult) -> str:
    lines = [
        f"ci-lint act audit: {result.store_path}",
        f"  disk usage: {format_size(result.disk_bytes)} / budget {format_size(result.budget_bytes)}",
        f"  complete index entries: {len(result.entries)}",
    ]
    if result.findings:
        lines.append("  FAILED:")
        lines.extend(f"    {f}" for f in result.findings)
    else:
        lines.append("  ok: within budget, no retired-family key observed")
    return "\n".join(lines)


def to_json_dict(result: ActCacheAuditResult) -> dict[str, JsonValue]:
    return {
        "store_path": str(result.store_path),
        "disk_bytes": result.disk_bytes,
        "budget_bytes": result.budget_bytes,
        "entries": [
            {
                "id": e.id,
                "key": e.key,
                "cache_size": e.cache_size,
                "complete": e.complete,
                "used_at": e.used_at,
            }
            for e in result.entries
        ],
        "findings": list(result.findings),
        "ok": result.ok,
    }
