"""RUST-016: Dylint lint-crate builds must not thrash cargo's fingerprint.

zackees/ci.yml#82 (evidence: zackees/running-process#1235/#1237, run
36524237645). cargo-dylint links lint libraries through `dylint-link`, and
the linker is part of cargo's unit fingerprint (the `config` hash). Two
static shapes made every warm run rebuild ~80-99 shared crates:

1. **Shared target dir.** A workflow runs Dylint (`soldr dylint`,
   `cargo dylint`) and a cargo command inside a lint crate (one whose
   `.cargo/config.toml` sets `linker = "dylint-link"`) against the SAME
   `CARGO_TARGET_DIR`/`--target-dir`. Each side flips the linker, so each
   rebuilds the other's units and the cache holds only whichever ran last
   (running-process#1235: workspace gate 27 s -> 17 s once split).
2. **Crate config silently ignored.** A cargo command uses
   `--manifest-path <dir>/Cargo.toml` while its working directory is not
   `<dir>` (or below it), and `<dir>/.cargo/config.toml` exists. Cargo
   reads `.cargo/config.toml` from the cwd, not the manifest's directory,
   so the crate's own linker/flags are dropped -- a correctness bug as much
   as a cache bug, and not Dylint-specific (running-process#1237: fixture
   steps 34 s -> 3 s and 43 s -> 2 s on a warm rerun).

Both are violations. Scope: workflow / composite-action `run:` lines (with
step/job `working-directory` and workflow/job/step/inline `CARGO_TARGET_DIR`)
and, one level deep, the literal subprocess argv of a referenced `ci/*.py`
script (its `cwd=` keyword when literal; a non-literal `cwd=` is skipped).
Only literal target-dir values are compared. A same-line
`# ci-lint: allow RUST-016 <reason>` excuses one line.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.test_invocations import (
    allowed,
    is_cargo_token,
    normalize_expr,
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

RULE = "RUST-016"
_CONFIG_NAMES = (".cargo/config.toml", ".cargo/config")


@dataclass(frozen=True)
class CargoCall:
    tokens: tuple[str, ...]
    path: str
    line: int | None
    loc: str
    raw: str
    cwd: str  # repo-relative, normalized ("." = repo root)
    target_dir: str | None


def _norm(rel: str) -> str:
    out = posixpath.normpath(rel.strip().removeprefix("./")) if rel.strip() else "."
    return out


def _env_of(node: YamlValue) -> dict[str, str]:
    env = as_dict(node).get("env") if isinstance(node, dict) else None
    if not isinstance(env, dict):
        return {}
    return {str(k): normalize_expr(str(v)) for k, v in env.items() if isinstance(v, (str, int, float))}


def _crate_config(repo_root: Path, crate_dir: str) -> str | None:
    for name in _CONFIG_NAMES:
        cfg = repo_root / crate_dir / name
        if cfg.is_file():
            try:
                return cfg.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return ""
    return None


def _is_lint_crate(repo_root: Path, crate_dir: str) -> bool:
    text = _crate_config(repo_root, crate_dir)
    return text is not None and "dylint-link" in text


def _flag_value(tokens: tuple[str, ...], flag: str) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == "--":
            return None
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def _cargo_index(tokens: tuple[str, ...]) -> int | None:
    for i, tok in enumerate(tokens):
        if is_cargo_token(tok):
            return i
    return None


def _is_dylint(tokens: tuple[str, ...]) -> bool:
    if tokens and tokens[0] in ("cargo-dylint",):
        return True
    for i, tok in enumerate(tokens[:-1]):
        if (tok == "soldr" or is_cargo_token(tok)) and tokens[i + 1] == "dylint":
            return True
    return False


def _manifest_dir(tokens: tuple[str, ...], cwd: str) -> str | None:
    manifest = _flag_value(tokens, "--manifest-path")
    if manifest is None or "$" in manifest:
        return None
    if manifest.startswith("/"):
        return None  # absolute path: cannot map to the repo tree
    return _norm(posixpath.join(cwd, posixpath.dirname(manifest)))


def _within(cwd: str, crate_dir: str) -> bool:
    return cwd == crate_dir or cwd.startswith(crate_dir + "/")


def _collect(repo_root: Path) -> dict[str, list[CargoCall]]:  # noqa: C901
    """Every cargo/dylint call per workflow file."""

    by_file: dict[str, list[CargoCall]] = {}
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        raw = raw_lines_of(repo_root / wf.path)
        calls: list[CargoCall] = []
        wf_env = _env_of(doc)
        for job_id, job in jobs_of(doc).items():
            job_env = {**wf_env, **_env_of(job)}
            defaults_run = as_dict(as_dict(job.get("defaults")).get("run"))
            job_wd = defaults_run.get("working-directory")
            for i, step in enumerate(steps_of(job)):
                run_text = step.get("run")
                if not isinstance(run_text, str):
                    continue
                env = {**job_env, **_env_of(step)}
                wd = step.get("working-directory", job_wd)
                cwd = _norm(wd) if isinstance(wd, str) else "."
                loc = f"jobs.{job_id}.steps[{i}]"
                for ln in logical_lines(run_text):
                    raw_line = with_yaml_comment(ln, raw)
                    for cmd in split_commands_with_env(ln):
                        inline_env, tokens = cmd.env, cmd.tokens
                        t = tuple(tokens)
                        cmd_env = {**env, **inline_env}
                        target = _flag_value(t, "--target-dir") or cmd_env.get("CARGO_TARGET_DIR")
                        if _cargo_index(t) is not None or _is_dylint(t):
                            calls.append(CargoCall(t, wf.path, None, loc, raw_line, cwd, target))
                        for tok in t:
                            if CI_SCRIPT_RE.match(tok) and (repo_root / cwd / tok).is_file():
                                calls.extend(_script_calls(repo_root, _norm(posixpath.join(cwd, tok)), cwd, target))
        by_file[wf.path] = calls
    return by_file


def _script_calls(repo_root: Path, rel: str, step_cwd: str, step_target: str | None) -> list[CargoCall]:
    out: list[CargoCall] = []
    script = repo_root / rel
    for call in script_calls(script):
        if _cargo_index(call.tokens) is None and not _is_dylint(call.tokens):
            continue
        if call.has_cwd and call.cwd_literal is None:
            continue  # non-literal cwd: cannot resolve statically
        cwd = _norm(posixpath.join(step_cwd, call.cwd_literal)) if call.cwd_literal is not None else step_cwd
        target = _flag_value(call.tokens, "--target-dir") or step_target
        out.append(
            CargoCall(call.tokens, rel, call.line, f"line {call.line}", source_line(script, call.line), cwd, target)
        )
    return out


def check_rust_016(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, int | None, str]] = set()
    for _wf_path, calls in _collect(repo_root).items():
        lint_calls: list[tuple[CargoCall, str]] = []
        for call in calls:
            if allowed(call.raw, RULE):
                continue
            mdir = _manifest_dir(call.tokens, call.cwd)
            crate = mdir if mdir is not None else call.cwd
            if (
                mdir is not None
                and mdir != "."
                and _crate_config(repo_root, mdir) is not None
                and not _within(call.cwd, mdir)
                and (call.path, call.line, call.loc) not in seen
            ):
                seen.add((call.path, call.line, call.loc))
                findings.append(
                    Finding(
                        rule=RULE,
                        path=call.path,
                        line=call.line,
                        message=f"{call.loc}: '--manifest-path {mdir}/Cargo.toml' runs from '{call.cwd}', so "
                        f"{mdir}/.cargo/config.toml is silently ignored (cargo reads config from the cwd, "
                        "not the manifest's directory)",
                        fix=f"run the step from the crate itself ('working-directory: {mdir}', or cwd= in the "
                        "ci/*.py subprocess call) and drop --manifest-path, so the crate's linker/flags apply "
                        "and its fingerprint stays stable (running-process#1237: 34 s -> 3 s warm)",
                    )
                )
            if not _is_dylint(call.tokens) and _is_lint_crate(repo_root, crate):
                lint_calls.append((call, crate))
        dylint_calls = [c for c in calls if _is_dylint(c.tokens) and not allowed(c.raw, RULE)]
        for call, crate in lint_calls:
            if call.target_dir is None:
                continue
            clash = next((d for d in dylint_calls if d.target_dir == call.target_dir), None)
            if clash is None:
                continue
            findings.append(
                Finding(
                    rule=RULE,
                    path=call.path,
                    line=call.line,
                    message=f"{call.loc}: a cargo command in lint crate '{crate}' (linker = dylint-link) "
                    f"shares target dir '{call.target_dir}' with the Dylint pass at {clash.path} {clash.loc}; "
                    "the linker is in cargo's fingerprint, so each side rebuilds the other's crates",
                    fix="give the lint-crate build its own target dir (e.g. a '/fixtures' subdirectory "
                    "inside the cached path, running-process#1235) so Dylint and the fixture tests each "
                    "keep a stable fingerprint",
                )
            )
    return findings
