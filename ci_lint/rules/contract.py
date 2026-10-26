"""Group 1: contract rules.

CT-001..CT-005 are produced while `ci_lint.schema.load_ci_toml` parses and
cross-references ci.toml, and while `ci_lint.exceptions` matches findings
against declared `[[exceptions]]`. This module covers what is left in group
1: the PR-title tag checks (TAG-001, TAG-002) and the Cargo.toml/[platforms]
cross-check (CT-006).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.resolve import derive_tag_registry, is_reserved_token
from ci_lint.schema import CiToml, profile_requires_cargo

BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")


def extract_bracket_tokens(title: str) -> list[str]:
    return BRACKET_RE.findall(title)


def check_tag_001(ci: CiToml, title: str) -> list[Finding]:
    """Unknown reserved tag in the PR title."""

    registry = derive_tag_registry(ci)
    findings: list[Finding] = []
    for token in extract_bracket_tokens(title):
        if is_reserved_token(token) and token not in registry:
            findings.append(
                Finding(
                    rule="TAG-001",
                    message=f"PR title tag '[{token}]' looks reserved (prefix ci-/no-test/release) "
                    "but is not a known tag",
                    fix=(
                        "use a declared tag: a platform id (ci-<platform-id>), a platform group "
                        "(ci-<group>), a suite (ci-test-<suite> / no-test-<suite>), or one of the "
                        f"[tags] entries in ci.toml ({', '.join(sorted(ci.tags)) or 'none declared'}); "
                        "fix the spelling in the PR title, or declare the tag in ci.toml's [tags] table"
                    ),
                )
            )
    return findings


def check_tag_002(title: str) -> list[Finding]:
    """[release] combined with any [no-test*]."""

    tokens = extract_bracket_tokens(title)
    has_release = "release" in tokens
    no_test_tokens = [t for t in tokens if t.startswith("no-test")]
    if has_release and no_test_tokens:
        return [
            Finding(
                rule="TAG-002",
                message=f"PR title combines [release] with {', '.join(f'[{t}]' for t in no_test_tokens)}",
                fix="a release run must keep its tests: remove the [no-test*] tag, or remove [release] "
                "and re-tag once the run is ready to ship",
            )
        ]
    return []


def check_ct_006(ci: CiToml, repo_root: Path) -> list[Finding]:
    """[platforms] targets must match Cargo.toml's
    [workspace.metadata.soldr].targets.

    Only Rust profiles: a known non-Rust profile (`cpp`, `github-action`)
    has no Cargo workspace to cross-check, so CT-006 is silent for it.
    """

    if not profile_requires_cargo(ci.profile):
        return []
    cargo_path = repo_root / "Cargo.toml"
    declared_targets = sorted({p.target for p in ci.platforms.values()})
    if not cargo_path.is_file():
        return [
            Finding(
                rule="CT-006",
                path="Cargo.toml",
                message="no Cargo.toml at the repo root to cross-check [platforms] targets against",
                fix="add a workspace Cargo.toml with "
                "[workspace.metadata.soldr] targets = [" + ", ".join(repr(t) for t in declared_targets) + "]",
            )
        ]
    try:
        with cargo_path.open("rb") as fh:
            cargo = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        return [
            Finding(
                rule="CT-006",
                path="Cargo.toml",
                message=f"Cargo.toml is not valid TOML: {exc}",
                fix="fix the TOML syntax error in Cargo.toml",
            )
        ]
    soldr_meta = (
        cargo.get("workspace", {}).get("metadata", {}).get("soldr", {})
        if isinstance(cargo.get("workspace"), dict)
        else {}
    )
    if not isinstance(soldr_meta, dict) or "targets" not in soldr_meta:
        return [
            Finding(
                rule="CT-006",
                path="Cargo.toml",
                message="Cargo.toml has no [workspace.metadata.soldr] targets table",
                fix="add [workspace.metadata.soldr] targets = ["
                + ", ".join(repr(t) for t in declared_targets)
                + "] to Cargo.toml, matching ci.toml's [platforms]",
            )
        ]
    cargo_targets_raw = soldr_meta["targets"]
    if not isinstance(cargo_targets_raw, list) or not all(isinstance(t, str) for t in cargo_targets_raw):
        return [
            Finding(
                rule="CT-006",
                path="Cargo.toml",
                message="[workspace.metadata.soldr].targets must be an array of strings",
                fix="set [workspace.metadata.soldr] targets to an array of target-triple strings",
            )
        ]
    cargo_targets = sorted(set(cargo_targets_raw))
    if cargo_targets != declared_targets:
        missing_in_cargo = sorted(set(declared_targets) - set(cargo_targets))
        missing_in_ci = sorted(set(cargo_targets) - set(declared_targets))
        parts = []
        if missing_in_cargo:
            parts.append(f"missing from Cargo.toml: {missing_in_cargo}")
        if missing_in_ci:
            parts.append(f"missing from ci.toml [platforms]: {missing_in_ci}")
        return [
            Finding(
                rule="CT-006",
                path="Cargo.toml",
                message="[platforms] targets differ from [workspace.metadata.soldr].targets ("
                + "; ".join(parts)
                + ")",
                fix="make ci.toml's [platforms].*.target set and Cargo.toml's "
                "[workspace.metadata.soldr].targets set identical",
            )
        ]
    return []
