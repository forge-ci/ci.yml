"""Static clauses of RUST-022, CACHE-015 and CACHE-016 (zackees/ci.yml#110).

docs/policy-rust.md "Lint-phase caches" decides that the `lint` phase caches
third-party dependency state only, under a restore prefix no ordinary PR can
change, written by a default-branch run that cannot be cancelled. Most of
that is only observable in a job log or the live cache listing; this module
enforces the parts a workflow + `ci.toml` already show.

Input names are setup-soldr's real ones (action.yml, read 2026-10-25):
`dylint`, `dylint-output-cache` (default "true" inside Dylint mode),
`target-cache-include-incremental`, `target-cache-compress`
(`auto`/`zstd`/`none`) and `target-cache-compress-level` (1-22).

Clauses:

* RUST-022 (a), needs_review: the full-output `dylint-output` mode is in use
  (`[cache.family]` with `via = "setup-soldr:dylint-output"`, or an explicit
  truthy `dylint-output-cache:` on a Dylint setup-soldr step). The policy
  retires it "once the deps layer ships"; setup-soldr has no layered-key
  input yet, so this cannot be a hard violation today.
* RUST-022 (b), needs_review: a lint cache's restore prefix includes the
  lockfile or workspace manifests -- a lint `[cache.family]` whose `key`
  components or literal `prefix` name them, or an `actions/cache` step with a
  lint key whose `restore-keys:` hashes `Cargo.lock`/`Cargo.toml`.
* RUST-022 (c), violation: a Dylint setup-soldr step persists `incremental/`
  (`target-cache-include-incremental` truthy).
* RUST-022 (d), compression on a Dylint setup-soldr step:
  `target-cache-compress: none` or a literal level 1 is a violation; a
  literal level above 12 is needs_review (level 19 is not worth ~100 s).
* RUST-022 (e), payload of an `actions/cache*` step whose key names
  `dylint`/`lint`: a `path:` entry with an `incremental` segment is a
  violation (the deps layer never holds incremental/); a path that is a
  workspace build tree (`target`, `target/<profile-or-triple>`, not the
  lint-library trees under `target/dylint/`) persists workspace units, which
  the policy allows only as the L2 layer under `-Zchecksum-freshness` -- so
  it is needs_review when neither the workflow file nor
  `.cargo/config.toml` enables checksum freshness. Fleet survey 2026-10-25:
  soldr caches only `target/dylint/{libraries,target}` (the lint-library
  foundation, exempt), clud caches only a pass marker, zccache and
  kernal-api use setup-soldr's own Dylint caches -- no current hit.
* CACHE-015, violation: a hand-rolled `dylint-target-v*` cache (an
  `actions/cache` key or a `[cache.family]` prefix) beside setup-soldr's
  Dylint caches.
* CACHE-016: among setup-soldr Dylint steps that save on `push`/`schedule`,
  none can run uncancelled (every one is under a literal
  `cancel-in-progress: true`) -> violation; unknown cancellation ->
  needs_review. No `schedule` writer outside a cancelling group (the nightly
  warm-up) -> needs_review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.cache_static import (
    _NO_SAVE_VALUES,
    _as_text,
    _composite_soldr_steps,
    _is_build_soldr,
    _resolve_input,
)
from ci_lint.rules.tools import _iter_uses_with
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import (
    as_dict,
    get_on_section,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)
from ci_lint.yaml_io import LoadStatus, YamlValue

_TRUTHY = frozenset({"true", "1", "yes", "on"})
LINT_VIAS: frozenset[str] = frozenset({"setup-soldr:dylint-output", "setup-soldr:dylint", "setup-soldr:lint-output"})
_PREFIX_INPUT_RE = re.compile(r"(cargo\.lock|cargo\.toml|lockfile|\block\b|manifest|workspace)", re.IGNORECASE)
_RESTORE_HASH_RE = re.compile(r"hashFiles\([^)]*Cargo\.(lock|toml)", re.IGNORECASE)
_LINT_KEY_RE = re.compile(r"(^|[^A-Za-z0-9])(dy)?lint", re.IGNORECASE)
_FRESHNESS_RE = re.compile(r"checksum-freshness")
_HAND_ROLLED_RE = re.compile(r"(^|[^A-Za-z0-9])dylint-target-v", re.IGNORECASE)
MAX_LINT_ZSTD_LEVEL = 12


@dataclass(frozen=True)
class DylintStep:
    """One setup-soldr build step in Dylint mode, with its job context."""

    workflow: str
    loc: str
    with_: dict[str, YamlValue]
    triggers: frozenset[str]
    cancel: bool | None  # literal cancel-in-progress true / absent-or-false / expression

    @property
    def where(self) -> str:
        return f"{self.workflow} {self.loc}"

    def text(self, name: str) -> str | None:
        raw = _as_text(self.with_.get(name))
        return None if raw is None else raw.strip()

    def truthy(self, name: str) -> bool:
        value = self.text(name)
        return value is not None and value.lower() in _TRUTHY

    @property
    def saves(self) -> bool:
        value = self.text("save-cache")
        return value is None or value.lower() not in _NO_SAVE_VALUES


def _cancel_of(value: YamlValue) -> bool | None:
    if not isinstance(value, dict):
        return False
    raw = _as_text(value.get("cancel-in-progress"))
    if raw is None:
        return False
    if "${{" in raw:
        return None
    return raw.strip().lower() in _TRUTHY


def _combine_cancel(workflow: bool | None, job: bool | None) -> bool | None:
    if workflow is True or job is True:
        return True
    if workflow is None or job is None:
        return None
    return False


def collect_dylint_steps(repo_root: Path) -> list[DylintStep]:
    composites = _composite_soldr_steps(repo_root)
    out: list[DylintStep] = []
    for item in load_workflows(repo_root):
        if item.status != LoadStatus.OK:
            continue
        doc = as_dict(item.document)
        triggers = frozenset(str(k) for k in get_on_section(doc))
        wf_cancel = _cancel_of(doc.get("concurrency"))
        for job_id, job in jobs_of(doc).items():
            cancel = _combine_cancel(wf_cancel, _cancel_of(job.get("concurrency")))
            for i, step in enumerate(steps_of(job)):
                uses = step.get("uses")
                if not isinstance(uses, str):
                    continue
                with_ = as_dict(step.get("with"))
                candidates: list[dict[str, YamlValue]] = []
                if _is_build_soldr(uses):
                    candidates.append(with_)
                else:
                    for comp in (c for c in composites if c.action == uses.rstrip("/")):
                        candidates.append({k: _resolve_input(v, with_, comp.defaults) for k, v in comp.with_.items()})
                for cand in candidates:
                    s = DylintStep(item.path, f"jobs.{job_id}.steps[{i}]", cand, triggers, cancel)
                    if s.truthy("dylint"):
                        out.append(s)
    return out


@dataclass(frozen=True)
class CacheStep:
    """One `actions/cache*` step's key and restore-keys text."""

    path: str
    loc: str
    key: str
    restore_keys: str
    paths: tuple[str, ...] = ()


def _cache_steps(repo_root: Path) -> list[CacheStep]:
    out: list[CacheStep] = []
    docs = [(w, False) for w in load_workflows(repo_root)] + [(c, True) for c in load_composite_actions(repo_root)]
    for item, composite in docs:
        if item.status != LoadStatus.OK:
            continue
        for s in _iter_uses_with(as_dict(item.document), is_composite=composite):
            if not s.uses.startswith("actions/cache"):
                continue
            out.append(
                CacheStep(
                    item.path,
                    s.loc,
                    _as_text(s.with_.get("key")) or "",
                    _as_text(s.with_.get("restore-keys")) or "",
                    tuple(ln.strip() for ln in (_as_text(s.with_.get("path")) or "").splitlines() if ln.strip()),
                )
            )
    return out


def _is_lint_family(fid: str, via: str) -> bool:
    return via in LINT_VIAS or "lint" in fid.lower()


def _rust_022_full_mode(ci: CiToml, steps: list[DylintStep]) -> list[Finding]:
    fix = (
        "move the lint cache to the deps-only layer (docs/policy-rust.md 'Lint-phase caches', #110) "
        "once setup-soldr ships the layered key generation, and list the `setup-soldr-dylint-output-v2-` "
        "prefix in [cache].retired"
    )
    out: list[Finding] = []
    for fid, fam in sorted(ci.cache.family.items()):
        if fam.via == "setup-soldr:dylint-output":
            out.append(
                Finding(
                    rule="RUST-022",
                    status=Status.NEEDS_REVIEW,
                    path="ci.toml",
                    message=(
                        f"[cache.family.{fid}] declares the full-target-dir `setup-soldr:dylint-output` "
                        "cache, the mode #110 retires once the deps layer ships"
                    ),
                    fix=fix,
                )
            )
    for s in steps:
        if s.truthy("dylint-output-cache"):
            out.append(
                Finding(
                    rule="RUST-022",
                    status=Status.NEEDS_REVIEW,
                    path=s.workflow,
                    message=f"{s.where} explicitly enables `dylint-output-cache`, the full-output mode #110 retires",
                    fix=fix,
                )
            )
    return out


def _rust_022_prefix(ci: CiToml, repo_root: Path) -> list[Finding]:
    fix = (
        "keep Cargo.lock only in the EXACT key and leave workspace manifests, versions, source and the "
        "lint-library hash out of the lint deps layer's restore prefix (#110 key shape)"
    )
    out: list[Finding] = []
    for fid, fam in sorted(ci.cache.family.items()):
        if not _is_lint_family(fid, fam.via):
            continue
        parts = list(fam.key or ()) + ([fam.prefix] if fam.prefix else [])
        hits = sorted({p for p in parts if _PREFIX_INPUT_RE.search(p)})
        if hits:
            out.append(
                Finding(
                    rule="RUST-022",
                    status=Status.NEEDS_REVIEW,
                    path="ci.toml",
                    message=(
                        f"lint cache family [cache.family.{fid}] keys on {', '.join(hits)}; if that is part of "
                        "the restore prefix, every lockfile bump misses the deps layer"
                    ),
                    fix=fix,
                )
            )
    for c in _cache_steps(repo_root):
        if "lint" in c.key.lower() and _RESTORE_HASH_RE.search(c.restore_keys):
            out.append(
                Finding(
                    rule="RUST-022",
                    status=Status.NEEDS_REVIEW,
                    path=c.path,
                    message=(
                        f"{c.path} {c.loc}: lint cache `restore-keys:` hashes Cargo.lock/Cargo.toml, so the "
                        "restore prefix changes with the lockfile or workspace manifests"
                    ),
                    fix=fix,
                )
            )
    return out


def _rust_022_contents(steps: list[DylintStep]) -> list[Finding]:
    out: list[Finding] = []
    for s in steps:
        if s.truthy("target-cache-include-incremental"):
            out.append(
                Finding(
                    rule="RUST-022",
                    path=s.workflow,
                    message=f"{s.where} persists `incremental/` in a Dylint job's target cache",
                    fix="drop `target-cache-include-incremental` from the lint job; the deps layer never holds incremental/",
                )
            )
        codec = s.text("target-cache-compress")
        if codec is not None and codec.lower() == "none":
            out.append(
                Finding(
                    rule="RUST-022",
                    path=s.workflow,
                    message=f"{s.where} saves the lint target cache uncompressed (`target-cache-compress: none`)",
                    fix="use zstd (the default); an uncompressed save is the slowest restore (#110: 749 MB vs 133 MB)",
                )
            )
        level = s.text("target-cache-compress-level")
        if level is not None and level.isdigit():
            n = int(level)
            if n <= 1:
                out.append(
                    Finding(
                        rule="RUST-022",
                        path=s.workflow,
                        message=f"{s.where} saves the lint target cache at zstd level {n}",
                        fix="use level 3 (the default); 9-12 only on default-branch saves under CACHE-023",
                    )
                )
            elif n > MAX_LINT_ZSTD_LEVEL:
                out.append(
                    Finding(
                        rule="RUST-022",
                        status=Status.NEEDS_REVIEW,
                        path=s.workflow,
                        message=f"{s.where} saves the lint target cache at zstd level {n}, above the decided 3-12",
                        fix="level 19 is not worth ~100 s of main-branch time; use 3, or 9-12 on default-branch saves",
                    )
                )
    return out


def _segments(path: str) -> list[str]:
    return [seg for seg in path.replace("\\", "/").split("/") if seg not in ("", ".")]


def _is_workspace_build_tree(path: str) -> bool:
    segs = _segments(path)
    if not segs or segs[0] != "target" or "${{" in path:
        return False
    return len(segs) <= 2 and (len(segs) < 2 or segs[1] != "dylint")


def _freshness_enabled(repo_root: Path, workflow: str) -> bool:
    for rel in (workflow, ".cargo/config.toml"):
        try:
            if _FRESHNESS_RE.search((repo_root / rel).read_text(encoding="utf-8")):
                return True
        except OSError:
            continue
    return False


def _rust_022_payload(repo_root: Path) -> list[Finding]:
    out: list[Finding] = []
    for c in _cache_steps(repo_root):
        if not _LINT_KEY_RE.search(c.key):
            continue
        if any("incremental" in _segments(p) for p in c.paths):
            out.append(
                Finding(
                    rule="RUST-022",
                    path=c.path,
                    message=f"{c.path} {c.loc}: lint cache `path:` persists `incremental/`",
                    fix="drop incremental/ from the lint cache; the deps layer never holds it (#110)",
                )
            )
        trees = [p for p in c.paths if _is_workspace_build_tree(p)]
        if trees and not _freshness_enabled(repo_root, c.path):
            out.append(
                Finding(
                    rule="RUST-022",
                    status=Status.NEEDS_REVIEW,
                    path=c.path,
                    message=(
                        f"{c.path} {c.loc}: lint cache saves the workspace build tree ({', '.join(trees)}) "
                        "without `-Zchecksum-freshness`; a fresh checkout's mtimes dirty every workspace unit, "
                        "so those units are dead weight"
                    ),
                    fix=(
                        "cache third-party units only (strip workspace units by dep-info unit hash), or run the "
                        "lint pass with `-Zchecksum-freshness` so the workspace units are a usable L2 layer"
                    ),
                )
            )
    return out


def _cache_015(ci: CiToml, repo_root: Path, steps: list[DylintStep]) -> list[Finding]:
    if not steps:
        return []
    fix = "delete the hand-rolled `dylint-target-v*` cache and rely on setup-soldr's Dylint caches (#110)"
    out: list[Finding] = []
    for fid, fam in sorted(ci.cache.family.items()):
        if fam.prefix and _HAND_ROLLED_RE.search(fam.prefix):
            out.append(
                Finding(
                    rule="CACHE-015",
                    path="ci.toml",
                    message=f"[cache.family.{fid}] declares a hand-rolled `{fam.prefix}` cache beside setup-soldr's Dylint caches",
                    fix=fix,
                )
            )
    for c in _cache_steps(repo_root):
        if _HAND_ROLLED_RE.search(c.key):
            out.append(
                Finding(
                    rule="CACHE-015",
                    path=c.path,
                    message=f"{c.path} {c.loc} saves a hand-rolled `dylint-target-v*` cache beside setup-soldr's Dylint caches",
                    fix=fix,
                )
            )
    return out


def _cache_016(steps: list[DylintStep]) -> list[Finding]:
    writers = [s for s in steps if s.saves and ({"push", "schedule"} & s.triggers)]
    if not writers:
        return []  # nothing saves it at all: CACHE-027's question, not this rule's
    path = writers[0].workflow
    out: list[Finding] = []
    if all(w.cancel is True for w in writers):
        out.append(
            Finding(
                rule="CACHE-016",
                path=path,
                message=(
                    "every default-branch writer of the must-hit Dylint cache runs under "
                    f"`cancel-in-progress: true` ({'; '.join(w.where for w in writers)}); a superseding push "
                    "cancels the save and PRs miss"
                ),
                fix=(
                    "drop `cancel-in-progress` on the writer's push run, or add a `schedule` writer in a "
                    "non-cancelling group such as `cache-janitor`"
                ),
            )
        )
    elif not any(w.cancel is False for w in writers):
        out.append(
            Finding(
                rule="CACHE-016",
                status=Status.NEEDS_REVIEW,
                path=path,
                message="the Dylint cache writers' `cancel-in-progress` is an expression; confirm one writer cannot be cancelled",
                fix="make one default-branch writer's concurrency literally non-cancelling",
            )
        )
    if not any("schedule" in w.triggers and w.cancel is not True for w in writers):
        out.append(
            Finding(
                rule="CACHE-016",
                status=Status.NEEDS_REVIEW,
                path=path,
                message=(
                    "no nightly `schedule` writer restores and re-saves the must-hit Dylint cache; GitHub's "
                    "7-day idle eviction can drop it and a toolchain/driver bump is not re-warmed until the next push"
                ),
                fix="add a scheduled warm-up job running the same setup-soldr Dylint step in a non-cancelling group",
            )
        )
    return out


def check_lint_cache(ci: CiToml, repo_root: Path) -> list[Finding]:
    steps = collect_dylint_steps(repo_root)
    findings: list[Finding] = []
    findings.extend(_rust_022_full_mode(ci, steps))
    findings.extend(_rust_022_prefix(ci, repo_root))
    findings.extend(_rust_022_contents(steps))
    findings.extend(_rust_022_payload(repo_root))
    findings.extend(_cache_015(ci, repo_root, steps))
    findings.extend(_cache_016(steps))
    return findings
