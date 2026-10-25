"""RUST-019: async guard lints are configured and inherited (static, #413).

A synchronous lock or `RefCell` borrow held across `.await` can deadlock an
async runtime: the guard blocks an executor thread while the future waits
on the runtime that would release it. The three clippy lints that catch
this class -- `await_holding_lock`, `await_holding_refcell_ref` and
`await_holding_invalid_type` -- live in the `pedantic` group, so nothing
fires until a repository opts them in; #413's decision is to opt in at
`deny` workspace-wide. Prior art: fbuild#1688 (proposal), soldr#3856
(adopted: deny + custom blocking wrappers registered in `clippy.toml`),
zccache#1979 (adoption, open).

Clauses this static check ENFORCES (settings only -- source-level Clippy
findings remain Clippy's responsibility):

- a Rust repository's workspace (or lone package) sets all three lints to
  `deny` (or `forbid`, which is stronger) in `[workspace.lints.clippy]`
  / `[lints.clippy]`;
- every workspace member inherits them (`[lints] workspace = true`) and
  does not re-declare one of the three at a weaker level;
- `clippy.toml`'s `await-holding-invalid-types`, when present, is a list
  of `{path, reason}` tables with both fields non-empty (well-formedness
  only).

Clauses deliberately NOT enforced here, recorded as Candidate in
policy-rust.md: completeness of a repository's custom-type registrations
(that needs source analysis -- which blocking guard types exist and
whether every one is registered), and `large_futures` thresholds (warn
first, per #413's decision; an unmeasured hard cap is cleanup debt).

The std `std::sync`/`std::cell` guard types need no
`await-holding-invalid-types` entries: `await_holding_lock` and
`await_holding_refcell_ref` already cover them (and parking_lot), which
is why soldr#3856's `clippy.toml` registers only its own wrappers --
#413's draft said to list the std paths; soldr's implementation evidence
and this spike (2026-10-25) show the built-ins are the normative path
for them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.toml_cursor import TomlValue

RUST019_RULE = "RUST-019"
GUARD_LINTS: tuple[str, ...] = ("await_holding_lock", "await_holding_refcell_ref", "await_holding_invalid_type")
_ON_LEVELS = frozenset({"deny", "forbid"})

_FIX = (
    "set all three to deny under [workspace.lints.clippy] (or [lints.clippy] for a lone package): "
    'await_holding_lock = "deny", await_holding_refcell_ref = "deny", await_holding_invalid_type = "deny", '
    "and give every member `[lints] workspace = true` without re-declaring them -- the std guard types are "
    "covered by the two built-in lints; register only your own blocking wrappers (with reasons) under "
    "`await-holding-invalid-types` in clippy.toml (zackees/ci.yml#413, soldr#3856)"
)


def _load_toml(path: Path) -> dict[str, TomlValue] | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _table(doc: dict[str, TomlValue], *keys: str) -> dict[str, TomlValue]:
    node: TomlValue = doc
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _level(value: TomlValue) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        level = value.get("level")
        return level if isinstance(level, str) else None
    return None


def _check_lint_table(lints: dict[str, TomlValue], where: str, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    for lint in GUARD_LINTS:
        level = _level(lints.get(lint))
        if level in _ON_LEVELS:
            continue
        shown = "unset" if level is None else f"`{level}`"
        findings.append(
            Finding(
                rule=RUST019_RULE,
                path=rel,
                message=f"clippy::{lint} is {shown} in `{where}`: a lock or RefCell borrow held across .await "
                "can deadlock the async runtime, and the lint lives in the pedantic group, so nothing fires "
                "until the workspace opts it in at deny",
                fix=_FIX,
            )
        )
    return findings


def _member_dirs(repo_root: Path, workspace: dict[str, TomlValue]) -> list[Path]:
    members = workspace.get("members")
    excluded_raw = workspace.get("exclude")
    excluded = {
        (repo_root / e).resolve()
        for e in (excluded_raw if isinstance(excluded_raw, list) else [])
        if isinstance(e, str)
    }
    dirs: list[Path] = []
    for pattern in members if isinstance(members, list) else []:
        if not isinstance(pattern, str):
            continue
        for path in sorted(repo_root.glob(pattern)):
            if path.resolve() not in excluded and (path / "Cargo.toml").is_file():
                dirs.append(path)
    return dirs


def _check_member(repo_root: Path, manifest: Path) -> list[Finding]:
    doc = _load_toml(manifest)
    if doc is None:
        return []
    rel = manifest.relative_to(repo_root).as_posix()
    lints = doc.get("lints")
    own = _table(doc, "lints", "clippy")
    weakened = [
        lint
        for lint in GUARD_LINTS
        if lint in own and _level(own.get(lint)) not in _ON_LEVELS
    ]
    if weakened:
        return [
            Finding(
                rule=RUST019_RULE,
                path=rel,
                message=f"this crate re-declares {'/'.join(weakened)} below deny, overriding the workspace's "
                "async guard lints -- an await can still hold the guard and deadlock the runtime",
                fix=_FIX,
            )
        ]
    if isinstance(lints, dict) and lints.get("workspace") is True:
        return []
    return [
        Finding(
            rule=RUST019_RULE,
            path=rel,
            message="this crate does not inherit the workspace lints (`[lints] workspace = true`), so the "
            "async guard lints do not reach it",
            fix=_FIX,
        )
    ]


def _check_clippy_toml(repo_root: Path, rel: str) -> list[Finding]:
    """Well-formedness only: registration completeness stays Candidate."""

    doc = _load_toml(repo_root / rel)
    if doc is None:
        return []
    entries = doc.get("await-holding-invalid-types")
    if entries is None:
        return []
    if not isinstance(entries, list):
        return [
            Finding(
                rule=RUST019_RULE,
                path=rel,
                message="await-holding-invalid-types must be a list of {path, reason} tables",
                fix=_FIX,
            )
        ]
    findings: list[Finding] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    rule=RUST019_RULE, path=rel,
                    message=f"await-holding-invalid-types[{index}] is not a table with path and reason",
                    fix=_FIX,
                )
            )
            continue
        path, reason = entry.get("path"), entry.get("reason")
        if not (isinstance(path, str) and path.strip() and isinstance(reason, str) and reason.strip()):
            findings.append(
                Finding(
                    rule=RUST019_RULE, path=rel,
                    message=f"await-holding-invalid-types[{index}] needs a non-empty path and reason (the "
                    "reason is what the next reader sees when clippy fires on a guard they are holding)",
                    fix=_FIX,
                )
            )
    return findings


def check_rust_019(repo_root: Path) -> list[Finding]:
    """Static settings check for #413; see the module docstring for the
    enforced/candidate clause split."""

    if not (repo_root / "Cargo.toml").is_file():
        return []
    doc = _load_toml(repo_root / "Cargo.toml")
    if doc is None:
        return []
    workspace = _table(doc, "workspace")
    if not workspace:
        findings = _check_lint_table(_table(doc, "lints", "clippy"), "[lints.clippy]", "Cargo.toml")
    else:
        findings = _check_lint_table(
            _table(workspace, "lints", "clippy"), "[workspace.lints.clippy]", "Cargo.toml"
        )
        if "package" in doc:
            findings.extend(_check_member(repo_root, repo_root / "Cargo.toml"))
        for member in _member_dirs(repo_root, workspace):
            if member.resolve() != repo_root.resolve():
                findings.extend(_check_member(repo_root, member / "Cargo.toml"))
    for name in ("clippy.toml", ".clippy.toml"):
        if (repo_root / name).is_file():
            findings.extend(_check_clippy_toml(repo_root, name))
    return findings