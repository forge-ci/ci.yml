"""RUST-028: system packages follow the compiled feature graph (#84).

Static signal, per workflow job: a `run:` line (or one level into a `ci/*.py`
script the job names) installs system dev packages -- `apt-get`/`apt
install` of a `*-dev` or `pkg-config`/`pkgconf` package, `brew install
pkg-config|pkgconf`, `choco install pkgconfiglite` -- in a step with no
`if:` gate, in a job that compiles Rust (`cargo build|test|check|clippy|
nextest|run|doc`).

Whether the compiled graph reaches a `-sys`/`system-deps`/`pkg-config` crate
cannot be decided soundly without `cargo tree` (the reaching crate is
usually transitive, e.g. gtk -> gtk-sys), so the rule never reports a
violation. It stays silent when the install is provably justified and
otherwise reports `needs_review`:

- silent: any cargo invocation in the job passes `--all-features`;
- silent: a workspace member's direct normal/build dependency (including
  `[target.*]` tables) is a `-sys`/`system-deps`/`pkg-config` crate that is
  non-optional, or optional and enabled by the job's feature flags
  (`--features`, default features unless `--no-default-features`, expanded
  through `[features]`);
- silent: the install step carries an `if:` (gated on a mode/job, the
  kernal-api#373 remedy), or the job compiles no Rust at all;
- needs_review: otherwise -- check `cargo tree -e normal,build` under the
  job's flags; the runner image's preinstalled packages need no install.

A same-line `# ci-lint: allow RUST-028 <reason>` excuses that install line.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_scan import discover_workspace
from ci_lint.finding import Finding, Status
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

RULE = "RUST-028"
COMPILE_SUBCOMMANDS: frozenset[str] = frozenset({"build", "test", "check", "clippy", "nextest", "run", "doc"})
PKG_CONFIG_PACKAGES: frozenset[str] = frozenset({"pkg-config", "pkgconf", "libpkgconf-dev", "pkgconfiglite"})
_SYS_CRATE_RE = re.compile(r"(-sys$)|^(system-deps|pkg-config)$")


@dataclass(frozen=True)
class CargoCall:
    args: tuple[str, ...]


@dataclass(frozen=True)
class InstallSite:
    packages: tuple[str, ...]
    path: str
    line: int | None
    loc: str
    raw: str
    gated: bool


@dataclass(frozen=True)
class JobScan:
    installs: tuple[InstallSite, ...]
    cargo_calls: tuple[CargoCall, ...]


@dataclass(frozen=True)
class SysDep:
    name: str  # dependency key in the manifest
    optional: bool


@dataclass(frozen=True)
class FeatureDef:
    name: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class FeatureRequest:
    features: frozenset[str]
    default: bool


@dataclass(frozen=True)
class CrateGraph:
    features: tuple[FeatureDef, ...]
    sys_deps: tuple[SysDep, ...]
    optional_names: frozenset[str]


def install_packages(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Qualifying dev packages a single command installs (empty if none)."""

    for i, tok in enumerate(tokens):
        base = tok.rsplit("/", 1)[-1]
        if base in ("apt-get", "apt", "brew", "choco") and "install" in tokens[i + 1 :]:
            rest = tokens[tokens.index("install", i + 1) + 1 :]
            pkgs = [t.split("=", 1)[0] for t in rest if not t.startswith("-")]
            if base in ("apt-get", "apt"):
                hit = [p for p in pkgs if p.endswith("-dev") or p in PKG_CONFIG_PACKAGES]
            else:
                hit = [p for p in pkgs if p in PKG_CONFIG_PACKAGES]
            return tuple(hit)
    return ()


def cargo_compile_args(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    for i, tok in enumerate(tokens):
        if not is_cargo_token(tok):
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].startswith("+"):
            j += 1
        if j < len(tokens) and tokens[j] in COMPILE_SUBCOMMANDS:
            return tokens[j + 1 :]
        return None
    return None


def _scan_tokens(
    tokens: tuple[str, ...], site: InstallSite, installs: list[InstallSite], calls: list[CargoCall]
) -> None:
    pkgs = install_packages(tokens)
    if pkgs:
        installs.append(InstallSite(pkgs, site.path, site.line, site.loc, site.raw, site.gated))
    args = cargo_compile_args(tokens)
    if args is not None:
        calls.append(CargoCall(args))


def scan_job(repo_root: Path, wf_path: str, job_id: str, job: dict[str, YamlValue]) -> JobScan:
    installs: list[InstallSite] = []
    calls: list[CargoCall] = []
    raw_lines = raw_lines_of(repo_root / wf_path)
    for i, step in enumerate(steps_of(job)):
        run_text = step.get("run")
        if not isinstance(run_text, str):
            continue
        gated = step.get("if") not in (None, "", True, "true")
        loc = f"jobs.{job_id}.steps[{i}]"
        for ln in logical_lines(run_text):
            line = with_yaml_comment(ln, raw_lines)
            here = InstallSite((), wf_path, None, loc, line, gated)
            for tokens in split_commands(line):
                _scan_tokens(tuple(tokens), here, installs, calls)
                for tok in tokens:
                    if CI_SCRIPT_RE.match(tok) and (repo_root / tok).is_file():
                        script = repo_root / tok
                        for call in script_calls(script):
                            site = InstallSite(
                                (), tok, call.line, f"line {call.line}", source_line(script, call.line), gated
                            )
                            _scan_tokens(call.tokens, site, installs, calls)
    return JobScan(tuple(installs), tuple(calls))


def _load(path: Path) -> dict[str, TomlValue] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _dep_tables(manifest: dict[str, TomlValue]) -> list[dict[str, TomlValue]]:
    tables: list[dict[str, TomlValue]] = []
    for key in ("dependencies", "build-dependencies"):
        t = manifest.get(key)
        if isinstance(t, dict):
            tables.append(t)
    targets = manifest.get("target")
    if isinstance(targets, dict):
        for spec in targets.values():
            if isinstance(spec, dict):
                for key in ("dependencies", "build-dependencies"):
                    t = spec.get(key)
                    if isinstance(t, dict):
                        tables.append(t)
    return tables


def crate_graphs(repo_root: Path) -> list[CrateGraph]:
    out: list[CrateGraph] = []
    for crate in discover_workspace(repo_root):
        manifest = _load(repo_root / crate.manifest_path) or {}
        sys_deps: list[SysDep] = []
        optional: set[str] = set()
        for table in _dep_tables(manifest):
            for name, val in table.items():
                is_opt = isinstance(val, dict) and val.get("optional") is True
                if is_opt:
                    optional.add(name)
                pkg = val.get("package", name) if isinstance(val, dict) else name
                if _SYS_CRATE_RE.search(str(pkg)):
                    sys_deps.append(SysDep(name, is_opt))
        out.append(
            CrateGraph(tuple(FeatureDef(k, v) for k, v in crate.features.items()), tuple(sys_deps), frozenset(optional))
        )
    return out


def _requested_features(args: tuple[str, ...]) -> FeatureRequest:
    feats: set[str] = set()
    for i, arg in enumerate(args):
        value: str | None = None
        if arg in ("-F", "--features") and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith("--features="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-F") and len(arg) > 2:
            value = arg[2:]
        if value is not None:
            feats.update(f for f in re.split(r"[,\s]+", value) if f)
    return FeatureRequest(frozenset(feats), "--no-default-features" not in args)


def _enabled_deps(graph: CrateGraph, request: FeatureRequest) -> set[str]:
    table = {f.name: f.members for f in graph.features}
    pending = list(request.features) + (["default"] if request.default else [])
    seen: set[str] = set()
    deps: set[str] = set()
    while pending:
        feat = pending.pop()
        if feat in seen:
            continue
        seen.add(feat)
        if feat.startswith("dep:"):
            deps.add(feat[4:])
            continue
        if "/" in feat:
            head = feat.split("/", 1)[0]
            if not head.endswith("?"):
                deps.add(head)
            continue
        if feat in graph.optional_names and feat not in table:
            deps.add(feat)
        pending.extend(table.get(feat, ()))
    return deps


def _graph_reaches_sys(graphs: list[CrateGraph], calls: tuple[CargoCall, ...]) -> bool:
    for graph in graphs:
        if any(not d.optional for d in graph.sys_deps):
            return True
        for call in calls:
            enabled = _enabled_deps(graph, _requested_features(call.args))
            if any(d.optional and d.name in enabled for d in graph.sys_deps):
                return True
    return False


def check_rust_028(repo_root: Path) -> list[Finding]:
    if not (repo_root / "Cargo.toml").is_file():
        return []
    graphs: list[CrateGraph] | None = None
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            scan = scan_job(repo_root, wf.path, job_id, job)
            pending = [s for s in scan.installs if not s.gated and not allowed(s.raw, RULE)]
            if not pending or not scan.cargo_calls:
                continue
            if any("--all-features" in c.args for c in scan.cargo_calls):
                continue
            if graphs is None:
                graphs = crate_graphs(repo_root)
            if _graph_reaches_sys(graphs, scan.cargo_calls):
                continue
            for site in pending:
                findings.append(
                    Finding(
                        rule=RULE,
                        path=site.path,
                        line=site.line,
                        status=Status.NEEDS_REVIEW,
                        message=f"{site.loc}: job '{job_id}' installs system dev package(s) "
                        f"{', '.join(site.packages)} unconditionally, but none of its cargo invocations "
                        "enables a feature set that statically reaches a -sys/system-deps/pkg-config "
                        "dependency",
                        fix="run 'cargo tree -e normal,build' under this job's real feature flags; if no "
                        "-sys crate appears, gate the install (an 'if:' on the mode or job that builds the "
                        "needing graph, e.g. kernal-api#373's mode == 'full') or drop it; the runner image's "
                        "preinstalled packages need no install",
                    )
                )
    return findings
