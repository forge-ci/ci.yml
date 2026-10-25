"""Derive Cargo workspace test binaries from manifests + the filesystem.

Round-1A brief, section B group 7: "derive test binaries from Cargo
manifests (tomllib + filesystem; NO cargo)". This is a deliberately
tomllib-only reimplementation of enough of Cargo's default target-discovery
rules (autobins/autotests, `src/main.rs`, `src/bin/*.rs`, `tests/*.rs`,
`tests/*/main.rs`, explicit `[[bin]]`/`[[test]]`) to name every test binary
the way ci.toml's `[rust.tests].binaries` does: `<crate>:lib`,
`<crate>:bin:<name>`, `<crate>:test:<name>`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.repo_files import list_repo_files
from ci_lint.toml_cursor import TomlValue
from ci_lint.rust_lexer import find_attribute_and_macro_spans, strip_comments_and_strings

# Round-6B: match `cfg(feature`/`cfg_attr(feature` allowing whitespace
# (including newlines) between the paren and `feature`, e.g.
# `#[cfg(\n    feature = "json"\n)]`, matched against a whole
# `find_attribute_and_macro_spans` span rather than a raw source line.
_CFG_FEATURE_RE = re.compile(r"cfg(_attr)?\(\s*feature")


@dataclass(frozen=True)
class CargoBinTarget:
    name: str
    path: str
    test: bool


@dataclass(frozen=True)
class CargoTestTarget:
    name: str
    path: str


@dataclass(frozen=True)
class CargoCrate:
    name: str
    dir: str  # repo-relative
    manifest_path: str  # repo-relative
    publish: bool
    has_lib: bool
    lib_test: bool
    bin_targets: tuple[CargoBinTarget, ...]
    test_targets: tuple[CargoTestTarget, ...]
    features: dict[str, tuple[str, ...]]
    optional_dep_names: tuple[str, ...]


def _load_toml(path: Path) -> dict[str, TomlValue] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _expand_members(repo_root: Path, patterns: list[str]) -> set[Path]:
    out: set[Path] = set()
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        candidates = repo_root.glob(pattern) if "*" in pattern else [repo_root / pattern]
        for p in candidates:
            if (p / "Cargo.toml").is_file():
                out.add(p)
    return out


def _resolve_publish(pkg: dict[str, TomlValue]) -> bool:
    raw = pkg.get("publish", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, list):
        return len(raw) > 0  # a registry allowlist still means "publishable"
    return True


def _resolve_lib(crate_dir: Path, manifest: dict[str, TomlValue]) -> tuple[bool, bool]:
    lib_section = manifest.get("lib")
    default_path = crate_dir / "src" / "lib.rs"
    if isinstance(lib_section, dict):
        rel_path = lib_section.get("path", "src/lib.rs")
        has_lib = "path" in lib_section or (crate_dir / str(rel_path)).is_file()
        test_val = lib_section.get("test", True)
        return has_lib, bool(test_val) if isinstance(test_val, bool) else True
    return default_path.is_file(), True


def _resolve_bins(  # noqa: C901
    crate_dir: Path, manifest: dict[str, TomlValue], pkg: dict[str, TomlValue], crate_name: str
) -> tuple[CargoBinTarget, ...]:
    bins: dict[str, CargoBinTarget] = {}
    explicit = manifest.get("bin")
    if isinstance(explicit, list):
        for b in explicit:
            if isinstance(b, dict) and isinstance(b.get("name"), str):
                bname = b["name"]
                bpath = b.get("path", f"src/bin/{bname}.rs")
                test_val = b.get("test", True)
                bins[bname] = CargoBinTarget(
                    name=bname,
                    path=str(bpath),
                    test=bool(test_val) if isinstance(test_val, bool) else True,
                )
    main_rs = crate_dir / "src" / "main.rs"
    if main_rs.is_file() and crate_name not in bins:
        bins[crate_name] = CargoBinTarget(name=crate_name, path="src/main.rs", test=True)
    autobins = pkg.get("autobins", True)
    if autobins is not False:
        bin_dir = crate_dir / "src" / "bin"
        if bin_dir.is_dir():
            for f in sorted(bin_dir.glob("*.rs")):
                if f.stem not in bins:
                    bins[f.stem] = CargoBinTarget(name=f.stem, path=f"src/bin/{f.stem}.rs", test=True)
            for sub in sorted(bin_dir.iterdir()):
                if sub.is_dir() and (sub / "main.rs").is_file() and sub.name not in bins:
                    bins[sub.name] = CargoBinTarget(
                        name=sub.name, path=f"src/bin/{sub.name}/main.rs", test=True
                    )
    return tuple(bins.values())


def _resolve_tests(
    crate_dir: Path, manifest: dict[str, TomlValue], pkg: dict[str, TomlValue]
) -> tuple[CargoTestTarget, ...]:
    tests: dict[str, CargoTestTarget] = {}
    explicit = manifest.get("test")
    if isinstance(explicit, list):
        for t in explicit:
            if isinstance(t, dict) and isinstance(t.get("name"), str):
                tname = t["name"]
                tpath = t.get("path", f"tests/{tname}.rs")
                tests[tname] = CargoTestTarget(name=tname, path=str(tpath))
    autotests = pkg.get("autotests", True)
    if autotests is not False:
        tests_dir = crate_dir / "tests"
        if tests_dir.is_dir():
            for f in sorted(tests_dir.glob("*.rs")):
                if f.stem not in tests:
                    tests[f.stem] = CargoTestTarget(name=f.stem, path=f"tests/{f.stem}.rs")
            for sub in sorted(tests_dir.iterdir()):
                if sub.is_dir() and (sub / "main.rs").is_file() and sub.name not in tests:
                    tests[sub.name] = CargoTestTarget(name=sub.name, path=f"tests/{sub.name}/main.rs")
    return tuple(tests.values())


def _resolve_features_and_deps(
    manifest: dict[str, TomlValue],
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    features_raw = manifest.get("features")
    features: dict[str, tuple[str, ...]] = {}
    if isinstance(features_raw, dict):
        for key, val in features_raw.items():
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                features[key] = tuple(val)
    optional_deps: list[str] = []
    for dep_table_name in ("dependencies", "dev-dependencies", "build-dependencies"):
        deps = manifest.get(dep_table_name)
        if isinstance(deps, dict):
            for dep_name, dep_val in deps.items():
                if isinstance(dep_val, dict) and dep_val.get("optional") is True:
                    optional_deps.append(dep_name)
    return features, tuple(optional_deps)


def discover_workspace(repo_root: Path) -> list[CargoCrate]:
    root_manifest_path = repo_root / "Cargo.toml"
    root = _load_toml(root_manifest_path)
    if root is None:
        return []
    workspace = root.get("workspace")
    members: list[str] = []
    exclude: list[str] = []
    if isinstance(workspace, dict):
        raw_members = workspace.get("members")
        if isinstance(raw_members, list):
            members = [m for m in raw_members if isinstance(m, str)]
        raw_exclude = workspace.get("exclude")
        if isinstance(raw_exclude, list):
            exclude = [m for m in raw_exclude if isinstance(m, str)]

    crate_dirs = _expand_members(repo_root, members) - _expand_members(repo_root, exclude)
    # A workspace root manifest can itself carry [package] (a root crate).
    if isinstance(root.get("package"), dict):
        crate_dirs.add(repo_root)

    crates: list[CargoCrate] = []
    for crate_dir in sorted(crate_dirs):
        manifest_path = crate_dir / "Cargo.toml"
        manifest = root if crate_dir == repo_root else _load_toml(manifest_path)
        if manifest is None:
            continue
        pkg = manifest.get("package")
        if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
            continue
        name = pkg["name"]
        has_lib, lib_test = _resolve_lib(crate_dir, manifest)
        bins = _resolve_bins(crate_dir, manifest, pkg, name)
        tests = _resolve_tests(crate_dir, manifest, pkg)
        features, optional_deps = _resolve_features_and_deps(manifest)
        crates.append(
            CargoCrate(
                name=name,
                dir=crate_dir.relative_to(repo_root).as_posix() if crate_dir != repo_root else ".",
                manifest_path=manifest_path.relative_to(repo_root).as_posix(),
                publish=_resolve_publish(pkg),
                has_lib=has_lib,
                lib_test=lib_test,
                bin_targets=bins,
                test_targets=tests,
                features=features,
                optional_dep_names=optional_deps,
            )
        )
    return crates


def expected_target_names(crate: CargoCrate) -> dict[str, str]:
    """name -> kind ("lib" | "bin" | "test"), for targets that get a
    harness by default (lib/bin with test != false; every integration
    test target)."""

    out: dict[str, str] = {}
    if crate.has_lib and crate.lib_test:
        out[f"{crate.name}:lib"] = "lib"
    for b in crate.bin_targets:
        if b.test:
            out[f"{crate.name}:bin:{b.name}"] = "bin"
    for t in crate.test_targets:
        out[f"{crate.name}:test:{t.name}"] = "test"
    return out


def has_any_rust_test_attr(crate_src_dir: Path) -> bool:
    if not crate_src_dir.is_dir():
        return False
    for f in crate_src_dir.rglob("*.rs"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "#[test]" in text or "#[tokio::test]" in text:
            return True
    return False


def has_cfg_feature(repo_root: Path, crate_dir_rel: str) -> bool:
    """True if any git-tracked `.rs` file under `<crate_dir_rel>/src` uses
    `cfg(feature = ...)` / `cfg_attr(feature = ...)` in real code -- not
    merely mentioned inside a `//`/`///`/`//!`/`/* */` doc comment or a
    string literal (round-2A brief, defect 2: a doc comment mentioning
    `cfg(feature = "json")` was flagged as though it were real code), and
    not evaded by splitting the attribute/macro across lines (round-6B:
    `#[cfg(\n    feature = "json"\n)]` used to evade the old
    `"cfg(feature" in code` line-blind substring check)."""

    prefix = f"{crate_dir_rel}/src/" if crate_dir_rel != "." else "src/"
    for rel in list_repo_files(repo_root):
        if not (rel.startswith(prefix) and rel.endswith(".rs")):
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = strip_comments_and_strings(text)
        for span in find_attribute_and_macro_spans(code):
            if _CFG_FEATURE_RE.search(span.text):
                return True
    return False


def effective_features(
    features: tuple[str, ...] | list[str], declared: dict[str, tuple[str, ...]]
) -> frozenset[str]:
    """zackees/ci.yml#89: `default` names no graph of its own -- it is
    exactly its members. Replace it with the feature names it lists (an
    empty `default = []`, or no `default` at all, contributes nothing), so
    `[]` and `["default"]` compare equal when default is empty. Members that
    are `dep:`/`crate/feature` wiring are not feature names and are dropped
    (Cargo's own artifact record already lists the features they enable)."""

    out = set(features)
    if "default" in out:
        out.discard("default")
        out.update(m for m in declared.get("default", ()) if m in declared)
    return frozenset(out)
