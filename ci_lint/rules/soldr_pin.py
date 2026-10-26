"""RUST-013: float soldr by default (zackees/ci.yml#18).

Fleet direction: track current soldr through `zackees/setup-soldr@v0`
resolving the latest release and a floor-only `soldr>=X` build requirement
in `pyproject.toml`. An exact pin -- `soldr==X.Y.Z` in
`[build-system].requires`, or a literal `version:` input on a
`zackees/setup-soldr` step -- freezes on a version that silently goes
stale, and is only allowed as a recorded `[[exceptions]]` entry (the
generic exception mechanism in `ci_lint.exceptions` turns a matched finding
into `approved_exception`; this rule just needs to emit the finding at the
right `path` for that match to work).
"""

from __future__ import annotations

from ci_lint import repo_identity

import re
import tomllib
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.cache_static import _loaded_files
from ci_lint.rules.tools import _iter_uses_with
from ci_lint.schema import CiToml
from ci_lint.toml_cursor import TomlValue

SOLDR_EXACT_PIN_RE = re.compile(r"^soldr\s*==\s*\S+")
SOLDR_FLOOR_RE = re.compile(r"^soldr\s*(>=|~=|>)\s*\S+")


def _load_toml(path: Path) -> dict[str, TomlValue] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _check_pyproject_pin(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    pyproject = _load_toml(repo_root / "pyproject.toml")
    if pyproject is None:
        return findings  # PKG-004 already reports a missing/invalid pyproject.toml
    build_system = pyproject.get("build-system")
    build_system = build_system if isinstance(build_system, dict) else {}
    requires = build_system.get("requires")
    requires = requires if isinstance(requires, list) else []
    for entry in requires:
        if isinstance(entry, str) and SOLDR_EXACT_PIN_RE.match(entry.strip()):
            findings.append(
                Finding(
                    rule="RUST-013",
                    path="pyproject.toml",
                    message=f"[build-system].requires pins '{entry.strip()}' exactly, instead of "
                    "floating",
                    fix="replace the exact 'soldr==X.Y.Z' pin in [build-system].requires with a "
                    "floor pin 'soldr>=X.Y.Z' (raise the floor only when a newer feature is needed), "
                    "or add a [[exceptions]] entry for rule = \"RUST-013\", path = \"pyproject.toml\" "
                    "recording the reason, owner, and an automated bump path",
                )
            )
    return findings


def _is_literal_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    # A plan-derived or otherwise computed expression is not a literal pin.
    if "${{" in v or "needs." in v or "inputs." in v:
        return False
    return True


def _check_setup_soldr_version_input(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []

    for loaded in _loaded_files(repo_root):
        path, is_composite, doc = loaded.path, loaded.is_composite, loaded.doc
        for use_step in _iter_uses_with(doc, is_composite=is_composite):
            uses = use_step.uses
            loc = use_step.loc
            with_ = use_step.with_
            if repo_identity.setup_soldr_prefix(uses) is None:
                continue
            version = with_.get("version")
            if _is_literal_version(version):
                findings.append(
                    Finding(
                        rule="RUST-013",
                        path=path,
                        message=f"{loc}: zackees/setup-soldr is called with an exact "
                        f"version: {version!r} instead of floating",
                        fix=f"remove the literal 'version:' input at {loc} so setup-soldr resolves "
                        "the latest release (or pass a plan-derived expression), or add a "
                        f"[[exceptions]] entry for rule = \"RUST-013\", path = \"{path}\" recording "
                        "the reason, owner, and an automated bump path",
                    )
                )
    return findings


def check_rust_013(ci: CiToml, repo_root: Path) -> list[Finding]:
    del ci  # not needed; kept for the check_group7 call convention
    findings: list[Finding] = []
    findings.extend(_check_pyproject_pin(repo_root))
    findings.extend(_check_setup_soldr_version_input(repo_root))
    return findings
