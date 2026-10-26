"""PKG-008 static half (zackees/ci.yml#119): the abi3 cp310 floor.

A `rust-pypi-app` whose Cargo workspace depends on `pyo3` must enable
exactly one `abi3-py3XX` feature on the Cargo dependency, and it must be
`abi3-py310` (a higher floor needs an `[[exceptions]]` entry); a
`pyo3/abi3-*` feature set only frontend-side (`[tool.maturin].features` or
`[tool.soldr.pep517].features`) while the Cargo dependency lacks it is a
violation; and `pyproject.toml`'s `requires-python` must be `>=3.10`.

A repository with no `pyo3` Cargo dependency at all ships no PyO3 native
extension and is out of scope. The runtime half lives in
`ci_lint.runtime.wheel.check_wheel` and `ci_lint.release`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.repo_files import list_repo_files
from ci_lint.toml_cursor import TomlValue

REQUIRED_FEATURE = "abi3-py310"
_ABI3_PY_RE = re.compile(r"^abi3-py3\d+$")
_DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


@dataclass(frozen=True)
class Pyo3Dep:
    manifest: str
    features: tuple[str, ...]


def _load_toml(path: Path) -> dict[str, TomlValue] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _pyo3_features(table: TomlValue) -> tuple[str, ...] | None:
    if not isinstance(table, dict):
        return None
    dep = table.get("pyo3")
    if dep is None:
        return None
    if isinstance(dep, dict):
        feats = dep.get("features")
        if isinstance(feats, list):
            return tuple(f for f in feats if isinstance(f, str))
    return ()


def _dep_tables(manifest: dict[str, TomlValue]) -> list[TomlValue]:
    tables: list[TomlValue] = [manifest.get(name) for name in _DEP_TABLES]
    workspace = manifest.get("workspace")
    if isinstance(workspace, dict):
        tables.append(workspace.get("dependencies"))
    target = manifest.get("target")
    if isinstance(target, dict):
        for spec in target.values():
            if isinstance(spec, dict):
                tables.extend(spec.get(name) for name in _DEP_TABLES)
    return tables


def find_pyo3_deps(repo_root: Path) -> list[Pyo3Dep]:
    deps: list[Pyo3Dep] = []
    for rel in list_repo_files(repo_root):
        if Path(rel).name != "Cargo.toml":
            continue
        manifest = _load_toml(repo_root / rel)
        if manifest is None:
            continue
        for table in _dep_tables(manifest):
            feats = _pyo3_features(table)
            if feats is not None:
                deps.append(Pyo3Dep(manifest=rel, features=feats))
    return deps


def _frontend_abi3_features(pyproject: dict[str, TomlValue]) -> list[str]:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return []
    out: list[str] = []
    soldr = tool.get("soldr")
    sections: dict[str, TomlValue] = {
        "tool.maturin": tool.get("maturin"),
        "tool.soldr.pep517": soldr.get("pep517") if isinstance(soldr, dict) else None,
    }
    for name, section in sections.items():
        if not isinstance(section, dict):
            continue
        feats = section.get("features")
        for feat in feats if isinstance(feats, list) else []:
            if isinstance(feat, str) and feat.startswith("pyo3/abi3"):
                out.append(f"[{name}].features: {feat!r}")
    return out


def _requires_python_ok(spec: str) -> bool:
    clauses = [c.replace(" ", "") for c in spec.split(",") if c.strip()]
    floors = [c for c in clauses if c.startswith(">=")]
    return floors == [">=3.10"]


def check_pkg_008(repo_root: Path) -> list[Finding]:
    deps = find_pyo3_deps(repo_root)
    if not deps:
        return []
    findings: list[Finding] = []
    union = sorted({f for d in deps for f in d.features})
    abi3_floors = [f for f in union if _ABI3_PY_RE.match(f)]
    where = ", ".join(sorted({d.manifest for d in deps}))
    if abi3_floors != [REQUIRED_FEATURE]:
        findings.append(
            Finding(
                rule="PKG-008",
                path=deps[0].manifest,
                message=f"pyo3 Cargo dependency ({where}) enables abi3 floor feature(s) "
                f"{abi3_floors or 'none'}; expected exactly ['{REQUIRED_FEATURE}']",
                fix=f'set pyo3 = {{ ..., features = ["{REQUIRED_FEATURE}"] }} on the workspace/extension-crate '
                'Cargo dependency (a higher floor needs an [[exceptions]] entry, rule = "PKG-008")',
            )
        )
    pyproject = _load_toml(repo_root / "pyproject.toml")
    if pyproject is None:
        return findings
    if REQUIRED_FEATURE not in abi3_floors:
        for entry in _frontend_abi3_features(pyproject):
            findings.append(
                Finding(
                    rule="PKG-008",
                    path="pyproject.toml",
                    message=f"{entry} sets the pyo3 abi3 feature frontend-side only, not on the Cargo "
                    "dependency -- the wheel build and soldr cargo build/test compile pyo3 twice",
                    fix=f'move it to the pyo3 Cargo dependency (features = ["{REQUIRED_FEATURE}"]); only '
                    "extension-module stays frontend-side",
                )
            )
    project = pyproject.get("project")
    spec = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(spec, str) or not _requires_python_ok(spec):
        findings.append(
            Finding(
                rule="PKG-008",
                path="pyproject.toml",
                message=f"[project].requires-python is {spec!r}; expected '>=3.10' to match abi3-py310",
                fix='set requires-python = ">=3.10" in pyproject.toml\'s [project] table',
            )
        )
    return findings
