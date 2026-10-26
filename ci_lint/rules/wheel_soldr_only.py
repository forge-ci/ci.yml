"""PKG-010: wheels come from `soldr wheel` only; no zig anywhere in CI (#17).

docs/policy-rust.md's "Wheels come from `soldr wheel`; no zig in CI". For a
`rust-pypi-app` repository (wired through `check_group8`'s
`NATIVE_WHEEL_PROFILES` scope), static signals, all violations:

- a workflow / composite-action `run:` line, or one level into a referenced
  `ci/*.py` script, that builds a wheel through maturin directly
  (`maturin build`/`maturin develop`, wrapped e.g. as `uvx maturin build`,
  `uv run maturin develop`, `python -m maturin build`);
- the same surfaces using zig: `--zig`, `maturin[zig]`, `ziglang`,
  `cargo zigbuild`/`cargo-zigbuild`, `zig cc`/`zig c++`;
- a `uses:` step on `PyO3/maturin-action` or a zig installer action
  (`goto-bus-stop/setup-zig`, `mlugg/setup-zig`, any `*/setup-zig`);
- `pyproject.toml` dependency lists (`[project].dependencies`,
  `[project.optional-dependencies]`, `[dependency-groups]`,
  `[tool.uv].dev-dependencies`) naming `ziglang` or `maturin[zig]`.

Maturin as the PEP 517 backend driven by `soldr wheel` is allowed: nothing
here reads `[build-system]` (that is PKG-004's business).

No double reporting: a command whose first token is a bare `maturin`,
`cargo` or `cargo-*` already fires TOOL-001 (`ci_lint.rules.tools.
_tool_findings_for_commands`) on the same line, so PKG-010 skips it; a
`maturin ... --zig` in a job PKG-007 already flags for its release
`*-linux-gnu` build (`ci_lint.rules.packaging.check_pkg_007`) is skipped
too. PKG-004 only reads `[build-system]`, which PKG-010 never reads.

A same-line `# ci-lint: allow PKG-010 <reason>` excuses that line.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.packaging import check_pkg_007
from ci_lint.rules.test_invocations import allowed, iter_command_sites
from ci_lint.toml_cursor import TomlValue
from ci_lint.rules.tools import UsesStep, _is_bare_banned, _iter_uses_with
from ci_lint.workflow_scan import as_dict, load_composite_actions, load_workflows
from ci_lint.yaml_io import LoadStatus

RULE = "PKG-010"

@dataclass(frozen=True)
class _ZigPattern:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class _MaturinCall:
    index: int  # position of the `maturin` token
    subcommand: str


_FIX_WHEEL = (
    "build the wheel with 'soldr wheel' ('soldr wheel --release --target <triple>' for a release "
    "wheel); maturin may only run as the PEP 517 backend soldr drives, and install + test the "
    "built wheel instead of 'maturin develop'"
)
_FIX_ZIG = (
    "remove zig from CI: build the wheel with 'soldr wheel --target <triple>', whose catalogue "
    "sysroot/toolchain replaces zig's cross linker (soldr removed its zig route, zackees/soldr#2519)"
)

_ZIG_PATTERNS: tuple[_ZigPattern, ...] = (
    _ZigPattern("maturin[zig]", re.compile(r"maturin\[[^\]]*\bzig\b[^\]]*\]", re.IGNORECASE)),
    _ZigPattern("--zig", re.compile(r"(?<![\w-])--zig\b")),
    _ZigPattern("ziglang", re.compile(r"\bziglang\b", re.IGNORECASE)),
    _ZigPattern("cargo zigbuild", re.compile(r"\bcargo[-\s]+zigbuild\b")),
    _ZigPattern("zig cc", re.compile(r"\bzig\s+c(?:c|\+\+)\b")),
)
_MATURIN_ACTION_RE = re.compile(r"^pyo3/maturin-action(@|$)", re.IGNORECASE)
_SETUP_ZIG_RE = re.compile(r"^[^/]+/setup-zig(@|$)", re.IGNORECASE)
_DEP_ZIG_RE = re.compile(r"^\s*ziglang\b|maturin\s*\[[^\]]*\bzig\b", re.IGNORECASE)


@dataclass(frozen=True)
class _SiteKey:
    path: str
    loc: str
    what: str


def _maturin_subcommand(tokens: tuple[str, ...]) -> _MaturinCall | None:
    """(index of the `maturin` token, subcommand) for a build/develop call."""

    for i, tok in enumerate(tokens):
        if tok == "maturin" and i + 1 < len(tokens) and tokens[i + 1] in ("build", "develop"):
            return _MaturinCall(i, tokens[i + 1])
    return None


def _pkg_007_zig_jobs(repo_root: Path) -> frozenset[str]:
    return frozenset(
        f.path for f in check_pkg_007(repo_root) if f.path and "--zig" in f.message
    )


def _job_key(path: str, loc: str) -> str:
    return f"{path}#{loc.split('.steps')[0]}"


def _check_command_sites(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[_SiteKey] = set()
    pkg_007_jobs = _pkg_007_zig_jobs(repo_root)

    def emit(key: _SiteKey, line: int | None, message: str, fix: str) -> None:
        if key in seen:
            return
        seen.add(key)
        findings.append(Finding(rule=RULE, path=key.path, line=line, message=message, fix=fix))

    for site in iter_command_sites(repo_root):
        if not site.tokens or allowed(site.raw, RULE):
            continue
        tool_001_fires = _is_bare_banned(site.tokens[0])
        hit = _maturin_subcommand(site.tokens)
        if hit is not None and not (tool_001_fires and hit.index == 0):
            emit(
                _SiteKey(site.path, site.loc, f"maturin {hit.subcommand}"),
                site.line,
                f"{site.loc}: 'maturin {hit.subcommand}' builds a wheel directly instead of 'soldr wheel'",
                _FIX_WHEEL,
            )
        # site.raw too: leading env assignments (CC="zig cc") are not in tokens.
        text = " ".join(site.tokens) + "\n" + site.raw
        for zig in _ZIG_PATTERNS:
            name = zig.name
            if not zig.pattern.search(text):
                continue
            if tool_001_fires and site.tokens[0] in ("maturin", "cargo", "cargo-zigbuild"):
                continue  # TOOL-001 already reports this bare-tool line
            if name == "--zig" and site.line is None and _job_key(site.path, site.loc) in pkg_007_jobs:
                continue  # PKG-007 already reports this release maturin --zig job
            emit(
                _SiteKey(site.path, site.loc, name),
                site.line,
                f"{site.loc}: uses zig in CI ('{name}')",
                _FIX_ZIG,
            )
    return findings


def _uses_finding(path: str, step: UsesStep) -> Finding | None:
    if _MATURIN_ACTION_RE.match(step.uses):
        return Finding(
            rule=RULE,
            path=path,
            message=f"{step.loc}: 'uses: {step.uses}' builds a wheel outside 'soldr wheel'",
            fix=_FIX_WHEEL,
        )
    if _SETUP_ZIG_RE.match(step.uses):
        return Finding(
            rule=RULE,
            path=path,
            message=f"{step.loc}: 'uses: {step.uses}' installs zig in CI",
            fix=_FIX_ZIG,
        )
    return None


def _check_uses(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for files, composite in ((load_workflows(repo_root), False), (load_composite_actions(repo_root), True)):
        for parsed in files:
            if parsed.status != LoadStatus.OK or parsed.document is None:
                continue
            for step in _iter_uses_with(as_dict(parsed.document), is_composite=composite):
                finding = _uses_finding(parsed.path, step)
                if finding is not None:
                    findings.append(finding)
    return findings


@dataclass(frozen=True)
class _DepList:
    where: str
    entries: tuple[str, ...]


def _str_entries(value: TomlValue) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _dep_lists(doc: dict[str, TomlValue]) -> list[_DepList]:
    out: list[_DepList] = []
    project = doc.get("project")
    if isinstance(project, dict):
        out.append(_DepList("[project].dependencies", _str_entries(project.get("dependencies"))))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for name, deps in optional.items():
                out.append(_DepList(f"[project.optional-dependencies].{name}", _str_entries(deps)))
    groups = doc.get("dependency-groups")
    if isinstance(groups, dict):
        for name, deps in groups.items():
            out.append(_DepList(f"[dependency-groups].{name}", _str_entries(deps)))
    tool = doc.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if isinstance(uv, dict):
        out.append(_DepList("[tool.uv].dev-dependencies", _str_entries(uv.get("dev-dependencies"))))
    return out


def _check_pyproject(repo_root: Path) -> list[Finding]:
    try:
        with (repo_root / "pyproject.toml").open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [
        Finding(
            rule=RULE,
            path="pyproject.toml",
            message=f"{deps.where} pulls zig into the toolchain: {entry!r}",
            fix=_FIX_ZIG,
        )
        for deps in _dep_lists(doc)
        for entry in deps.entries
        if _DEP_ZIG_RE.search(entry)
    ]


def check_pkg_010(repo_root: Path) -> list[Finding]:
    return _check_command_sites(repo_root) + _check_uses(repo_root) + _check_pyproject(repo_root)
