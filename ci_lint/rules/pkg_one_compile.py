"""PKG-009 static half (zackees/ci.yml#118): one extension-crate compile per job.

A maturin-built `rust-pypi-app` must not compile its PyO3 extension crate in
more than one step of one CI job. maturin builds it as `cargo rustc
--crate-type cdylib`, which overwrites the plain build's `target/` unit
(`TargetConfigurationChanged`), so a plain cargo build/check/test/clippy/
nextest step over the same crate rebuilds it (and the cache keeps only one).

Static signal, within one workflow job (`run:` lines plus one level into a
referenced `ci/*.py` script):

- a maturin step: `maturin build|develop`, or -- only when `pyproject.toml`'s
  build backend is maturin -- a project install that builds the wheel
  (`uv sync`, `uv pip install .|-e .`, `pip install .|-e .`);
- plus a plain cargo build/check/test/clippy/nextest/rustc step that selects
  the extension crate:
  - `-p/--package <ext>`, or `--workspace`/`--all` without
    `--exclude <ext>` -> violation;
  - `--exclude <ext>` whose `--features` (absent `--all-features`) do not
    mirror every `<member>/<feature>` the extension crate enables on its
    workspace-member path dependencies -> violation (nextest recompiles
    those dependencies under the unified feature set);
  - no package selector at all (default members / cwd package decide) ->
    needs_review.

Out of scope: no `pyo3` crate with a `cdylib` lib (or `[tool.maturin]
.manifest-path`) -- nothing to compile twice. A repository on the Soldr PEP
517 backend has no maturin step, so it is clean unless its own workflow
calls maturin. A same-line `# ci-lint: allow PKG-009 <reason>` on either
step excuses the pair. The runtime signal (`Compiling pyo3` in two steps of
a warm job) is CACHE-035's cross-step recompile check.
"""

from __future__ import annotations

import posixpath
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.repo_files import list_repo_files
from ci_lint.rules.test_invocations import (
    allowed,
    is_cargo_token,
    logical_lines,
    raw_lines_of,
    script_calls,
    source_line,
    split_commands,
    with_yaml_comment,
)
from ci_lint.rules.tools import CI_SCRIPT_RE
from ci_lint.toml_cursor import TomlValue
from ci_lint.workflow_scan import as_dict, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "PKG-009"
_CARGO_SUBCOMMANDS = frozenset({"build", "check", "test", "clippy", "nextest", "rustc"})


@dataclass(frozen=True)
class ExtCrate:
    name: str
    required_features: tuple[str, ...]  # `<member>/<feature>` to mirror


@dataclass(frozen=True)
class Site:
    path: str
    line: int | None
    loc: str
    raw: str


@dataclass(frozen=True)
class SiteCmd:
    site: Site
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    status: Status
    why: str


def _load(path: Path) -> dict[str, TomlValue] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _is_cdylib_pyo3(manifest: dict[str, TomlValue]) -> bool:
    deps = manifest.get("dependencies")
    if not (isinstance(deps, dict) and "pyo3" in deps):
        return False
    lib = manifest.get("lib")
    types = lib.get("crate-type") if isinstance(lib, dict) else None
    return isinstance(types, list) and "cdylib" in types


def _feature_list(entry: TomlValue) -> list[str]:
    feats = entry.get("features") if isinstance(entry, dict) else None
    return [f for f in feats if isinstance(f, str)] if isinstance(feats, list) else []


def _required_features(manifest: dict[str, TomlValue], workspace_deps: dict[str, TomlValue]) -> tuple[str, ...]:
    deps = manifest.get("dependencies")
    out: set[str] = set()
    for name, entry in (deps.items() if isinstance(deps, dict) else []):
        if not isinstance(entry, dict):
            continue
        feats = _feature_list(entry)
        is_member = "path" in entry
        if entry.get("workspace") is True:
            ws = workspace_deps.get(name)
            is_member = isinstance(ws, dict) and "path" in ws
            feats += _feature_list(ws)
        if is_member:
            pkg = entry.get("package")
            dep_name = pkg if isinstance(pkg, str) else name
            out.update(f"{dep_name}/{f}" for f in feats)
    return tuple(sorted(out))


def find_extension_crates(repo_root: Path) -> list[ExtCrate]:
    root_manifest = _load(repo_root / "Cargo.toml") or {}
    ws = root_manifest.get("workspace")
    ws_deps_raw = ws.get("dependencies") if isinstance(ws, dict) else None
    ws_deps: dict[str, TomlValue] = ws_deps_raw if isinstance(ws_deps_raw, dict) else {}
    pyproject = _load(repo_root / "pyproject.toml") or {}
    tool = pyproject.get("tool")
    maturin = tool.get("maturin") if isinstance(tool, dict) else None
    mpath = maturin.get("manifest-path") if isinstance(maturin, dict) else None
    out: list[ExtCrate] = []
    for rel in list_repo_files(repo_root):
        if Path(rel).name != "Cargo.toml":
            continue
        manifest = _load(repo_root / rel)
        if manifest is None:
            continue
        named = isinstance(mpath, str) and posixpath.normpath(mpath) == posixpath.normpath(rel)
        if not (named or _is_cdylib_pyo3(manifest)):
            continue
        pkg = manifest.get("package")
        name = pkg.get("name") if isinstance(pkg, dict) else None
        if isinstance(name, str):
            out.append(ExtCrate(name, _required_features(manifest, ws_deps)))
    return out


def _maturin_backend(repo_root: Path) -> bool:
    pyproject = _load(repo_root / "pyproject.toml") or {}
    bs = pyproject.get("build-system")
    backend = bs.get("build-backend") if isinstance(bs, dict) else None
    return isinstance(backend, str) and backend.startswith("maturin")


def _is_maturin_step(tokens: tuple[str, ...], maturin_backend: bool) -> bool:
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok.rsplit("/", 1)[-1] == "maturin" and nxt in ("build", "develop"):
            return True
    if not maturin_backend:
        return False
    if tokens[:2] == ("uv", "sync"):
        return True
    for i, tok in enumerate(tokens):
        if tok in ("pip", "pip3") and i + 1 < len(tokens) and tokens[i + 1] == "install":
            rest = tokens[i + 2 :]
            return "." in rest or any(a.startswith(("./", ".[")) for a in rest)
    return False


def _cargo_args(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    for i, tok in enumerate(tokens):
        if is_cargo_token(tok):
            rest = [t for t in tokens[i + 1 :] if not t.startswith("+")]
            if rest and rest[0] in _CARGO_SUBCOMMANDS and "dylint" not in rest:
                return tuple(rest[1:])
            return None
    return None


def _values(args: tuple[str, ...], *flags: str) -> list[str]:
    out: list[str] = []
    for i, tok in enumerate(args):
        if tok == "--":
            break
        for flag in flags:
            if tok == flag and i + 1 < len(args):
                out.append(args[i + 1])
            elif tok.startswith(flag + "=") and flag.startswith("--"):
                out.append(tok.split("=", 1)[1])
    return out


def _verdict(args: tuple[str, ...], ext: ExtCrate) -> Verdict | None:
    cut = args.index("--") if "--" in args else len(args)
    head = args[:cut]
    if ext.name in _values(head, "-p", "--package"):
        return Verdict(Status.VIOLATION, f"selects the extension crate `{ext.name}` with -p/--package")
    if _values(head, "-p", "--package"):
        return None
    if "--workspace" in head or "--all" in head:
        if ext.name not in _values(head, "--exclude"):
            return Verdict(Status.VIOLATION, f"builds the whole workspace without `--exclude {ext.name}`")
        if "--all-features" in head:
            return None
        given = {f for v in _values(head, "--features", "-F") for f in v.replace(",", " ").split()}
        missing = [f for f in ext.required_features if f not in given]
        if missing:
            return Verdict(
                Status.VIOLATION,
                f"excludes `{ext.name}` but does not mirror its dependency features "
                f"({', '.join(missing)}), so the test build recompiles them",
            )
        return None
    return Verdict(
        Status.NEEDS_REVIEW,
        "has no package selector, so whether it builds the extension crate depends on default-members/cwd",
    )


def _job_sites(repo_root: Path, job: dict[str, YamlValue], job_id: str, wf_path: str, raw: list[str]) -> list[SiteCmd]:
    out: list[SiteCmd] = []
    for i, step in enumerate(steps_of(job)):
        run_text = step.get("run")
        if not isinstance(run_text, str):
            continue
        loc = f"jobs.{job_id}.steps[{i}]"
        for ln in logical_lines(run_text):
            raw_line = with_yaml_comment(ln, raw)
            for tokens in split_commands(ln):
                t = tuple(tokens)
                out.append(SiteCmd(Site(wf_path, None, loc, raw_line), t))
                for tok in t:
                    if not (CI_SCRIPT_RE.match(tok) and (repo_root / tok).is_file()):
                        continue
                    for call in script_calls(repo_root / tok):
                        site = Site(tok, call.line, f"{loc} -> {tok} line {call.line}",
                                    source_line(repo_root / tok, call.line))
                        out.append(SiteCmd(site, call.tokens))
    return out


def check_pkg_009(repo_root: Path) -> list[Finding]:
    exts = find_extension_crates(repo_root)
    if not exts:
        return []
    backend = _maturin_backend(repo_root)
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        raw = raw_lines_of(repo_root / wf.path)
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            sites = _job_sites(repo_root, job, job_id, wf.path, raw)
            maturin = next((c.site for c in sites if _is_maturin_step(c.tokens, backend)), None)
            if maturin is None or allowed(maturin.raw, RULE):
                continue
            for cmd in sites:
                site = cmd.site
                args = _cargo_args(cmd.tokens)
                if args is None or allowed(site.raw, RULE):
                    continue
                for ext in exts:
                    verdict = _verdict(args, ext)
                    if verdict is None:
                        continue
                    findings.append(
                        Finding(
                            rule=RULE,
                            path=site.path,
                            line=site.line,
                            status=verdict.status,
                            message=f"{site.loc}: plain cargo step {verdict.why}, in the same job as the maturin "
                            f"build at {maturin.loc}; maturin's cdylib overwrites the same target/ unit, so "
                            "the extension crate compiles more than once per job",
                            fix=f"use `--workspace --exclude {ext.name}` and pass `--features` mirroring "
                            "the extension crate's dependency features (end state: the Soldr PEP 517 backend, "
                            "PKG-004); or a same-line '# ci-lint: allow PKG-009 <reason>'",
                        )
                    )
    return findings
