"""`ci-lint fbuild coverage-check`: the `fbuild-coverage-preserved` acceptance
fixture (zackees/ci.yml#39, sub-issue of #4/#52; proposal.md's "Explicit
acceptance fixture: fbuild-coverage-preserved" section).

Proves, mechanically, that a proposed/faster CI shape for a repository built
like `FastLED/fbuild` still requires at least everything a pinned real
snapshot of that repository required:

    existing required coverage ⊆ declared coverage

Deleting any required case -- a board build, a macOS architecture, the
ignored Python facade suite, a full-graph gate, or a standalone required
check -- must turn this fixture red (AGENTS.md: "Confirm coverage from
commands and runs, not from a runner label").

Like `ci_lint.example_drift`, this module reads its `ci.toml` inputs over
raw `tomllib` (never through `ci_lint.schema`'s typed schema-3 loader):
its job is comparing a `[coverage]` declaration against a pinned snapshot,
not validating a repository's whole CI configuration. The board registry
(`ci/board_families.json` in the real repository) is itself the primary
evidence: `board_registry` in `[coverage]` points at a copy of it, and
loading it is what turns 80 registry entries (4 duplicate `workflow`
aliases sharing one `env_name`) into 76 distinct required board build
cases -- see `ci_lint/tests/fixtures/fbuild/SOURCE.md`.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.toml_cursor import TomlValue


class FbuildCoverageError(Exception):
    """A `ci.toml` or board registry input could not be read or parsed --
    exit 2, never silently treated as "no coverage required"."""


@dataclass(frozen=True)
class BoardCase:
    workflow: str
    env_name: str
    family: str
    firmware_ext: str


def load_board_registry(path: Path) -> tuple[BoardCase, ...]:
    """Load every entry of a `board_families.json`-shaped registry (the
    real fbuild file has 80 entries; some share `env_name` under a
    different `workflow` alias)."""
    try:
        with open(path, "rb") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise FbuildCoverageError(f"cannot read board registry {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FbuildCoverageError(f"cannot parse board registry {path}: {exc}") from exc

    boards = raw.get("boards")
    if not isinstance(boards, list):
        raise FbuildCoverageError(f"board registry {path} has no 'boards' array")

    out: list[BoardCase] = []
    for entry in boards:
        if not isinstance(entry, dict):
            raise FbuildCoverageError(f"board registry {path} has a non-object board entry")
        out.append(
            BoardCase(
                workflow=str(entry["workflow"]),
                env_name=str(entry["env_name"]),
                family=str(entry["family"]),
                firmware_ext=str(entry["firmware_ext"]),
            )
        )
    return tuple(out)


def distinct_board_env_names(cases: tuple[BoardCase, ...]) -> frozenset[str]:
    """Deduplicate by `env_name`: two `workflow` aliases building the same
    board (e.g. `build-nano-every.yml` / `build-nano_every.yml`, both
    `env_name: nano_every`) are one required coverage case, not two."""
    return frozenset(c.env_name for c in cases)


@dataclass(frozen=True)
class CoverageSet:
    """Required (or declared) coverage for the fbuild-shaped profile. Every
    field is a frozenset of stable case IDs, never a raw dict/list, per
    AGENTS.md's typed-boundary rule."""

    board_env_names: frozenset[str]
    macos_archs: frozenset[str]
    python_facades_ignored: frozenset[str]
    full_graph_gates: frozenset[str]
    standalone_required_checks: frozenset[str]


def _load_toml(path: Path, label: str) -> dict[str, TomlValue]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise FbuildCoverageError(f"cannot read {label} ci.toml {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise FbuildCoverageError(f"cannot parse {label} ci.toml {path}: {exc}") from exc


def load_coverage_set(ci_toml_path: Path, *, label: str) -> CoverageSet:
    """Load a `CoverageSet` from a `ci.toml`'s `[coverage]` table. The
    `board_registry` path inside `[coverage]` is resolved relative to
    `ci_toml_path`'s own directory."""
    doc = _load_toml(ci_toml_path, label)
    coverage = doc.get("coverage")
    if not isinstance(coverage, dict):
        raise FbuildCoverageError(f"{label} ci.toml {ci_toml_path} has no [coverage] table")

    registry_rel = coverage.get("board_registry")
    if not isinstance(registry_rel, str):
        raise FbuildCoverageError(
            f"{label} ci.toml {ci_toml_path} [coverage] is missing 'board_registry'"
        )
    registry_path = ci_toml_path.parent / registry_rel
    board_cases = load_board_registry(registry_path)

    def _str_list(key: str) -> frozenset[str]:
        value = coverage.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise FbuildCoverageError(
                f"{label} ci.toml {ci_toml_path} [coverage].{key} must be a list of strings"
            )
        return frozenset(value)

    return CoverageSet(
        board_env_names=distinct_board_env_names(board_cases),
        macos_archs=_str_list("macos_archs"),
        python_facades_ignored=_str_list("python_facades_ignored"),
        full_graph_gates=_str_list("full_graph_gates"),
        standalone_required_checks=_str_list("standalone_required_checks"),
    )


@dataclass(frozen=True)
class CoverageFinding:
    category: str
    missing_id: str


# CoverageSet field -> human label, in report order.
_CATEGORIES: dict[str, str] = {
    "board_env_names": "board build case",
    "macos_archs": "macOS architecture",
    "python_facades_ignored": "ignored Python facade suite",
    "full_graph_gates": "full-graph gate",
    "standalone_required_checks": "standalone required check",
}


def check_coverage_preserved(existing: CoverageSet, declared: CoverageSet) -> tuple[CoverageFinding, ...]:
    """`existing required coverage ⊆ declared coverage`. Returns one
    `CoverageFinding` per case present in `existing` but absent from
    `declared`, across every category -- empty means the subset relation
    holds (GREEN)."""
    out: list[CoverageFinding] = []
    for field in _CATEGORIES:
        existing_ids: frozenset[str] = getattr(existing, field)
        declared_ids: frozenset[str] = getattr(declared, field)
        for missing in sorted(existing_ids - declared_ids):
            out.append(CoverageFinding(category=field, missing_id=missing))
    return tuple(out)


def render_text(
    findings: tuple[CoverageFinding, ...], *, existing_path: Path, declared_path: Path
) -> str:
    labels = _CATEGORIES
    lines = [f"ci-lint fbuild coverage-check: {existing_path} (existing) vs {declared_path} (declared)"]
    if not findings:
        lines.append("existing required coverage is a subset of declared coverage (GREEN)")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"MISSING COVERAGE ({len(findings)}) -- declared coverage dropped a required case:")
    for f in findings:
        lines.append(f"  [{labels.get(f.category, f.category)}] {f.missing_id}")
    lines.append("")
    lines.append(
        "fix: restore each listed case to the declared ci.toml's [coverage] table "
        "(or its board_registry), or get an explicit, reviewed policy exception -- "
        "never widen this check to pass instead"
    )
    return "\n".join(lines)


def to_json_dict(findings: tuple[CoverageFinding, ...]) -> dict[str, JsonValue]:
    return {
        "findings": [{"category": f.category, "missing_id": f.missing_id} for f in findings],
        "missing_count": len(findings),
    }
