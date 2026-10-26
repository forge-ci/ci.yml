"""Group 7: tests & units (RUST-005, RUST-011, RUST-012).

Test binaries are derived from Cargo manifests and the filesystem only
(`ci_lint.cargo_scan`), never by shelling out to cargo.
"""

from __future__ import annotations

from dataclasses import dataclass

import re
from pathlib import Path

from ci_lint.cargo_scan import (
    CargoCrate,
    discover_workspace,
    effective_features,
    expected_target_names,
    has_any_rust_test_attr,
    has_cfg_feature,
)
from ci_lint.finding import Finding
from ci_lint.globs import matches_any
from ci_lint.rules.soldr_pin import check_rust_013
from ci_lint.rules.tools import _discover_python_command_files, _extract_ast_commands, ENV_ASSIGN_RE, _iter_run_steps, find_commands
from ci_lint.schema import CiToml

FEATURE_FLAG_RE = re.compile(r"[,\s]+")


def check_rust_012(ci: CiToml, crates: list[CargoCrate], repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    if ci.rust is None or ci.rust.tests is None:
        return findings
    declared = set(ci.rust.tests.binaries)

    all_expected: dict[str, str] = {}
    for c in crates:
        all_expected.update({t.name: t.kind for t in expected_target_names(c)})

    for name, kind in sorted(all_expected.items()):
        if name not in declared:
            findings.append(
                Finding(
                    rule="RUST-012",
                    message=f"undeclared {kind} test target '{name}'",
                    fix=f'add "{name}" to [rust.tests].binaries in ci.toml, or make the target not '
                    "produce a harness (e.g. 'test = false')",
                )
            )
    for name in sorted(declared):
        if name not in all_expected:
            findings.append(
                Finding(
                    rule="RUST-012",
                    path="ci.toml",
                    message=f"[rust.tests].binaries declares '{name}', which does not correspond to "
                    "any actual Cargo target",
                    fix=f"remove \"{name}\" from [rust.tests].binaries in ci.toml, or add the crate/"
                    "target it should refer to",
                )
            )
    for c in crates:
        key = f"{c.name}:lib"
        if key in declared and c.has_lib and c.lib_test:
            if not has_any_rust_test_attr(repo_root / c.dir / "src"):
                findings.append(
                    Finding(
                        rule="RUST-012",
                        path=f"{c.dir}/src",
                        message=f"'{key}' is declared and enabled, but {c.dir}/src has zero #[test] "
                        "functions",
                        fix=f"add at least one #[test] under {c.dir}/src, or remove '{key}' from "
                        f"[rust.tests].binaries and set '[lib] test = false' in {c.manifest_path}",
                    )
                )
    return findings


def check_rust_005(ci: CiToml, crates: list[CargoCrate]) -> list[Finding]:
    findings: list[Finding] = []
    declared = set(ci.rust.tests.binaries) if ci.rust and ci.rust.tests else set()
    for c in crates:
        actual = {f"{c.name}:test:{t.name}" for t in c.test_targets}
        declared_for_crate = {d for d in declared if d.startswith(f"{c.name}:test:")}
        if len(actual) > len(declared_for_crate):
            extra = sorted(actual - declared_for_crate)
            findings.append(
                Finding(
                    rule="RUST-005",
                    path=f"{c.dir}/tests",
                    message=f"crate '{c.name}' has {len(actual)} integration-test targets but only "
                    f"{len(declared_for_crate)} declared in [rust.tests].binaries (undeclared: {extra})",
                    fix=f"declare each integration-test target in [rust.tests].binaries, or "
                    f"consolidate {c.dir}/tests/*.rs into fewer files to reduce the linked-binary count "
                    "(see docs/case-studies/soldr-ci-cost.md)",
                )
            )
    return findings


@dataclass(frozen=True)
class _CommandSite:
    tokens: list[str]
    path: str
    loc: str


def _iter_all_commands(repo_root: Path) -> list[_CommandSite]:
    out: list[_CommandSite] = []
    for run_step in _iter_run_steps(repo_root):
        run_text = run_step.run_text
        path = run_step.path
        loc = run_step.loc
        for tokens in find_commands(run_text):
            out.append(_CommandSite(tokens, path, loc))
    for path in _discover_python_command_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        for cmd in _extract_ast_commands(path):
            tokens = cmd.tokens
            lineno = cmd.lineno
            i = 0
            while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
                i += 1
            remaining = tokens[i:]
            if remaining:
                out.append(_CommandSite(remaining, rel, f"line {lineno}"))
    return out


def check_rust_011(ci: CiToml, crates: list[CargoCrate], repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    if ci.rust is None:
        return findings

    private_crates = [c for c in crates if matches_any(c.dir, (ci.rust.private,))]
    private_names = {c.name for c in private_crates}

    for c in private_crates:
        if c.publish is not False:
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=c.manifest_path,
                    message=f"private crate '{c.name}' does not set publish = false",
                    fix=f"add 'publish = false' to [package] in {c.manifest_path}",
                )
            )
        if c.features:
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=c.manifest_path,
                    message=f"private crate '{c.name}' declares [features] {sorted(c.features)}",
                    fix=f"remove [features] from {c.manifest_path}; wire it into the public amalgam's "
                    f"'dep:{c.name}' feature instead",
                )
            )
        if c.optional_dep_names:
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=c.manifest_path,
                    message=f"private crate '{c.name}' has optional dependencies "
                    f"{list(c.optional_dep_names)}",
                    fix=f"remove 'optional = true' from {c.manifest_path}'s dependencies; a private "
                    "crate compiles unconditionally",
                )
            )
        if has_cfg_feature(repo_root, c.dir):
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=f"{c.dir}/src",
                    message=f"private crate '{c.name}' uses cfg(feature = ...) in its sources",
                    fix=f"remove cfg(feature) from {c.dir}/src; a private crate has no [features] and "
                    "compiles unconditionally",
                )
            )

    public_crate = next((c for c in crates if c.dir == ci.rust.public), None)
    if public_crate is not None:
        for feat_name, deps in public_crate.features.items():
            for dep in deps:
                dep_name = dep[len("dep:"):] if dep.startswith("dep:") else None
                if dep_name is None or dep_name not in private_names:
                    findings.append(
                        Finding(
                            rule="RUST-011",
                            path=public_crate.manifest_path,
                            message=f"[features].{feat_name} = {list(deps)!r} contains '{dep}', which "
                            "is not 'dep:<private crate>'",
                            fix=f"express {feat_name} in {public_crate.manifest_path} only as "
                            "dep:<private-crate-name> entries wiring an optional private crate",
                        )
                    )

    declared = public_crate.features if public_crate is not None else {}
    ship_sets = [effective_features(s, declared) for s in ci.rust.ship] if ci.rust.ship else [frozenset()]
    for site in _iter_all_commands(repo_root):
        tokens, path, loc = site.tokens, site.path, site.loc
        if not tokens or tokens[0] != "cargo":
            continue
        if "hack" in tokens[1:2]:
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=path,
                    message=f"{loc}: 'cargo hack' is not allowed",
                    fix=f"remove 'cargo hack' from {loc}; only the exact feature sets in [rust].ship "
                    "may be compiled",
                )
            )
        if "--all-features" in tokens:
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=path,
                    message=f"{loc}: '--all-features' is not allowed",
                    fix=f"replace '--all-features' at {loc} with an explicit '--features <set>' drawn "
                    "from ci.toml's [rust].ship",
                )
            )
        if "--feature-powerset" in tokens:
            findings.append(
                Finding(
                    rule="RUST-011",
                    path=path,
                    message=f"{loc}: '--feature-powerset' is not allowed",
                    fix=f"replace '--feature-powerset' at {loc} with the exact feature sets declared "
                    "in ci.toml's [rust].ship",
                )
            )
        if "--features" in tokens:
            idx = tokens.index("--features")
            if idx + 1 < len(tokens):
                value = tokens[idx + 1]
                feats = effective_features([f for f in FEATURE_FLAG_RE.split(value) if f], declared)
                if feats not in ship_sets:
                    findings.append(
                        Finding(
                            rule="RUST-011",
                            path=path,
                            message=f"{loc}: --features {value!r} is not one of [rust].ship "
                            f"{[sorted(s) for s in ship_sets]}",
                            fix="use only a feature combination declared in ci.toml's [rust].ship, or "
                            "add this combination to [rust].ship if it is genuinely needed",
                        )
                    )
    return findings


def check_group7(ci: CiToml, repo_root: Path) -> list[Finding]:
    crates = discover_workspace(repo_root)
    findings: list[Finding] = []
    findings.extend(check_rust_012(ci, crates, repo_root))
    findings.extend(check_rust_005(ci, crates))
    findings.extend(check_rust_011(ci, crates, repo_root))
    findings.extend(check_rust_013(ci, repo_root))
    return findings
