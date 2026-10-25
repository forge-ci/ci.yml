"""CACHE-039: one target dir rebuilt under different fingerprint settings.

zackees/ci.yml#148 (priority 2, the `same target/, same configuration`
family). Cargo hashes RUSTFLAGS, the linker, the feature set, the crate type
and (through PyO3's build script) the Python interpreter seen on PATH /
VIRTUAL_ENV into each unit's fingerprint. When two steps of ONE job build
into the same target dir under different values of any of those, the second
step rebuilds the units the first produced -- and a cache saved at job end
holds only whichever ran last, so the next run rebuilds again.

Static signal (needs_review -- a heuristic; the steps may build disjoint
crates): within one workflow job, two or more cargo build/check/test/
clippy/nextest/rustc/doc, `maturin build|develop`, or `uv run ...` steps
resolving to the same target dir (`--target-dir`, inline/step/job/workflow
`CARGO_TARGET_DIR`, else `<working-directory>/target`), the same `--target`
triple and the same profile, but with a different:

- RUSTFLAGS / CARGO_ENCODED_RUSTFLAGS / RUSTC_WRAPPER / CARGO_TARGET_*_LINKER
  env value,
- feature selection (`--features`, `--no-default-features`, `--all-features`),
- driver: maturin (cdylib, its own flags) vs plain cargo, or -- only when a
  `Cargo.toml` in the repository depends on `pyo3` -- `uv run` (venv
  PATH/VIRTUAL_ENV) vs plain cargo.

Dylint passes are left to RUST-016. Where the shape is the narrower one an
existing rule names, the finding cites it rather than restating it:
RUST-016 (Dylint linker flips), PKG-007 (release wheel built in a test
job), PKG-009 (more than one extension-crate compile per job) and RUST-030
(plain cargo vs `uv run` on a PyO3 crate). Scope: workflow `run:` lines,
plus one level into a referenced `ci/*.py` script. A same-line
`# ci-lint: allow CACHE-039 <reason>` on either step excuses the pair.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.rust_dylint_target import _env_of  # shared env resolver (PY-002 baseline)
from ci_lint.rules.test_invocations import (
    allowed,
    is_cargo_token,
    logical_lines,
    raw_lines_of,
    script_calls,
    source_line,
    split_commands_with_env,
    with_yaml_comment,
)
from ci_lint.rules.tools import CI_SCRIPT_RE
from ci_lint.workflow_scan import as_dict, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "CACHE-039"
_CARGO_SUBCOMMANDS = frozenset({"build", "check", "test", "clippy", "nextest", "rustc", "doc", "run", "bench"})
_FLAG_ENV = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER")


@dataclass(frozen=True)
class Settings:
    """The fingerprint-relevant part of one build command."""

    env: str  # sorted `K=V` of flag/linker env, space-joined
    features: str  # normalized feature selection
    driver: str  # "cargo", "maturin" or "uv-run"


@dataclass(frozen=True)
class BuildCall:
    path: str
    line: int | None
    loc: str
    raw: str
    target_key: str  # target dir + triple + profile
    settings: Settings


def _flag_values(tokens: tuple[str, ...], flag: str) -> list[str]:
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == "--":
            break
        if tok == flag and i + 1 < len(tokens):
            out.append(tokens[i + 1])
        elif tok.startswith(flag + "="):
            out.append(tok.split("=", 1)[1])
    return out


def _driver(tokens: tuple[str, ...]) -> str | None:
    """The build driver, or None when the command builds nothing."""

    uv = any(t == "uv" for t in tokens[:1]) and len(tokens) > 1 and tokens[1] == "run"
    for i, tok in enumerate(tokens):
        base = tok.rsplit("/", 1)[-1]
        if base == "maturin" and i + 1 < len(tokens) and tokens[i + 1] in ("build", "develop"):
            return "maturin"
        if is_cargo_token(tok):
            rest = [t for t in tokens[i + 1 :] if not t.startswith("+")]
            if rest and rest[0] in _CARGO_SUBCOMMANDS:
                return "uv-run" if uv else "cargo"
            return None
    return None


def _features(tokens: tuple[str, ...]) -> str:
    feats: set[str] = set()
    for value in _flag_values(tokens, "--features") + _flag_values(tokens, "-F"):
        feats.update(f for f in value.replace(",", " ").split() if f)
    parts = sorted(feats)
    if "--all-features" in tokens:
        parts.insert(0, "<all>")
    if "--no-default-features" in tokens:
        parts.insert(0, "<no-default>")
    return ",".join(parts)


def _flag_env(env: dict[str, str]) -> str:
    keys = sorted(
        k for k in env if k in _FLAG_ENV or (k.startswith("CARGO_TARGET_") and k.endswith(("_LINKER", "_RUSTFLAGS")))
    )
    return " ".join(f"{k}={env[k]}" for k in keys)


def _target_key(tokens: tuple[str, ...], env: dict[str, str], cwd: str) -> str:
    tdirs = _flag_values(tokens, "--target-dir")
    tdir = tdirs[0] if tdirs else env.get("CARGO_TARGET_DIR", posixpath.join(cwd, "target"))
    triples = _flag_values(tokens, "--target")
    profile = "release" if ("--release" in tokens or "-r" in tokens) else "dev"
    profiles = _flag_values(tokens, "--profile")
    if profiles:
        profile = profiles[0]
    return f"{posixpath.normpath(tdir)} [{triples[0] if triples else 'host'}/{profile}]"


def _call(tokens: tuple[str, ...], env: dict[str, str], cwd: str, site: BuildCall) -> BuildCall | None:
    driver = _driver(tokens)
    if driver is None or "dylint" in tokens:
        return None
    return BuildCall(
        site.path,
        site.line,
        site.loc,
        site.raw,
        _target_key(tokens, env, cwd),
        Settings(_flag_env(env), _features(tokens), driver),
    )


def _uses_pyo3(repo_root: Path) -> bool:
    for manifest in repo_root.rglob("Cargo.toml"):
        if "target" in manifest.parts:
            continue
        try:
            if "pyo3" in manifest.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def _job_calls(repo_root: Path, wf_path: str, job_id: str, job: dict[str, YamlValue], base_env: dict[str, str], raw: list[str]) -> list[BuildCall]:  # noqa: C901
    out: list[BuildCall] = []
    job_env = {**base_env, **_env_of(job)}
    job_wd = as_dict(as_dict(job.get("defaults")).get("run")).get("working-directory")
    for i, step in enumerate(steps_of(job)):
        run_text = step.get("run")
        if not isinstance(run_text, str):
            continue
        env = {**job_env, **_env_of(step)}
        wd = step.get("working-directory", job_wd)
        cwd = posixpath.normpath(wd) if isinstance(wd, str) else "."
        loc = f"jobs.{job_id}.steps[{i}]"
        for ln in logical_lines(run_text):
            raw_line = with_yaml_comment(ln, raw)
            for inline_env, tokens in split_commands_with_env(ln):
                t = tuple(tokens)
                cmd_env = {**env, **inline_env}
                found = _call(t, cmd_env, cwd, BuildCall(wf_path, None, loc, raw_line, "", Settings("", "", "")))
                if found is not None:
                    out.append(found)
                for tok in t:
                    rel = posixpath.normpath(posixpath.join(cwd, tok))
                    if not (CI_SCRIPT_RE.match(tok) and (repo_root / rel).is_file()):
                        continue
                    for call in script_calls(repo_root / rel):
                        if call.has_cwd:
                            continue  # a different cwd may mean a different target dir
                        site = BuildCall(rel, call.line, f"{loc} -> {rel} line {call.line}",
                                         source_line(repo_root / rel, call.line), "", Settings("", "", ""))
                        found = _call(call.tokens, cmd_env, cwd, site)
                        if found is not None:
                            out.append(found)
    return out


def _cite(a: Settings, b: Settings) -> str:
    drivers = {a.driver, b.driver}
    if "maturin" in drivers and "cargo" in drivers:
        return " (the narrower shapes are PKG-009 -- one extension-crate compile per job -- and PKG-007)"
    if "uv-run" in drivers and "cargo" in drivers:
        return " (RUST-030 names this PyO3 PATH/VIRTUAL_ENV shape)"
    return ""


def _diff(a: Settings, b: Settings) -> str:
    parts: list[str] = []
    if a.env != b.env:
        parts.append(f"flag/linker env '{a.env or '<none>'}' vs '{b.env or '<none>'}'")
    if a.features != b.features:
        parts.append(f"features '{a.features or '<default>'}' vs '{b.features or '<default>'}'")
    if a.driver != b.driver:
        parts.append(f"driver {a.driver} vs {b.driver}")
    return "; ".join(parts)


def check_cache_039(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    pyo3 = _uses_pyo3(repo_root)
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        raw = raw_lines_of(repo_root / wf.path)
        wf_env = _env_of(doc)
        for job_id, job in jobs_of(doc).items():
            first_by_key: dict[str, BuildCall] = {}
            reported: set[str] = set()
            for call in _job_calls(repo_root, wf.path, job_id, job, wf_env, raw):
                settings = call.settings
                if not pyo3 and settings.driver == "uv-run":
                    settings = Settings(settings.env, settings.features, "cargo")
                prev = first_by_key.get(call.target_key)
                if prev is None:
                    first_by_key[call.target_key] = BuildCall(
                        call.path, call.line, call.loc, call.raw, call.target_key, settings
                    )
                    continue
                if settings == prev.settings or call.target_key in reported:
                    continue
                if allowed(call.raw, RULE) or allowed(prev.raw, RULE):
                    continue
                reported.add(call.target_key)
                findings.append(
                    Finding(
                        rule=RULE,
                        path=call.path,
                        line=call.line,
                        status=Status.NEEDS_REVIEW,
                        message=f"{call.loc}: builds into {call.target_key} like {prev.loc} in the same job "
                        f"but with {_diff(prev.settings, settings)}; cargo's fingerprint differs, so this "
                        "step rebuilds the earlier step's units and the saved cache keeps only the last"
                        + _cite(prev.settings, settings),
                        fix="give each configuration its own target dir (CARGO_TARGET_DIR per step), or make "
                        "the steps agree on flags/features/driver; if they build disjoint crates, say so with "
                        "a same-line '# ci-lint: allow CACHE-039 <reason>' comment",
                    )
                )
    return findings
