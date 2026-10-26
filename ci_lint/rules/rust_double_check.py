"""RUST-023: a CI path type-checks the same code twice (zackees/ci.yml#115).

Evidence: FastLED/fbuild#1574 -- `check --workspace --all-targets` then
`clippy --workspace --all-targets` took 194.5 s against 106.4 s for clippy
alone (-45% per lint lane), and a separate MSRV `cargo check` lane on the
pinned toolchain cost ~5m37s per PR push.

Static signal (workflow `run:` lines, plus one level into a `ci/*.py` script
such a line names -- GEN-004's convention). "Same path" is one job, read in
step order:

- (a) violation: `cargo check` / `soldr cargo check` / `soldr check` whose
  normalized package selection, target selection, features, profile and
  `--target` equal, or are a subset of, a LATER `cargo clippy` in the same
  job. A check over a different target set (RUST-008 cross-target checks),
  different features or a different profile is not a finding. An argument
  carrying a `${{ }}`/`$VAR` expression, or an unrecognized flag, makes the
  comparison unprovable: `needs_review`.
- (b) violation: an MSRV lane -- a job whose id or `name:` contains the word
  `msrv` -- runs `cargo check` on a toolchain equal to the
  `rust-toolchain.toml` channel while the root `Cargo.toml`'s
  `rust-version` equals that channel. The lane's toolchain is the literal
  `+<tc>` override, a `rustup toolchain install`/`rustup default`/
  `rustup override set` argument, or a `dtolnay/rust-toolchain@<tc>` /
  `toolchain:` input; with none, it is the pinned channel. A non-literal or
  named-channel (`stable`, `nightly`) toolchain is `needs_review`.

The `cargo build` -> `cargo test` and `cargo doc` cases the policy names as
unmeasured are not checked. A same-line `# ci-lint: allow RUST-023 <reason>`
excuses that one line; a repository-level exception goes in ci.toml's
`[[exceptions]]`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

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
from ci_lint.workflow_scan import as_dict, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus

RULE = "RUST-023"
_MSRV_RE = re.compile(r"(?<![a-z0-9])msrv(?![a-z0-9])", re.IGNORECASE)
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
_FIX_A = (
    "drop the `cargo check` -- the later clippy over the same selection already type-checks it "
    "(clippy is a superset of check); or excuse the line with '# ci-lint: allow RUST-023 <reason>'"
)
_FIX_B = (
    "remove the MSRV lane (every lane already compiles on the pinned channel), or lower "
    "`rust-version` below the pin and run the lane on that older toolchain outside the quick gate"
)
# Flags taking a value, and which selection dimension they belong to.
_VALUE_FLAGS = {
    "-p": "pkg",
    "--package": "pkg",
    "--exclude": "pkg",
    "-F": "feat",
    "--features": "feat",
    "--profile": "profile",
    "--target": "target",
    "--bin": "tsel",
    "--example": "tsel",
    "--test": "tsel",
    "--bench": "tsel",
    "--manifest-path": "manifest",
    "-j": None,
    "--jobs": None,
    "--target-dir": None,
    "--color": None,
    "--message-format": None,
    "--config": None,
    "-Z": None,
}
_SWITCHES = {
    "--workspace": "pkg",
    "--all": "pkg",
    "--all-features": "feat",
    "--no-default-features": "feat",
    "--release": "profile",
    "-r": "profile",
    "--lib": "tsel",
    "--bins": "tsel",
    "--examples": "tsel",
    "--tests": "tsel",
    "--benches": "tsel",
    "--all-targets": "tsel",
    "--locked": None,
    "--frozen": None,
    "--offline": None,
    "--quiet": None,
    "-q": None,
    "--verbose": None,
    "-v": None,
    "-vv": None,
    "--keep-going": None,
}
_ALL_TSEL = frozenset({"lib", "bins", "examples", "tests", "benches"})


@dataclass(frozen=True)
class Selection:
    """A normalized cargo check/clippy selection; `None` when unprovable."""

    packages: frozenset[str]  # {"*"} = workspace
    tsel: frozenset[str]
    features: frozenset[str]
    profile: str
    targets: frozenset[str]  # {"<host>"} when no --target
    manifest: str


@dataclass(frozen=True)
class Invocation:
    kind: str  # "check" or "clippy"
    selection: Selection | None
    toolchain: str | None  # literal `+<tc>` override
    path: str
    line: int | None
    loc: str
    raw: str


@dataclass(frozen=True)
class Subcommand:
    kind: str
    toolchain: str | None
    args: tuple[str, ...]


@dataclass(frozen=True)
class FlagValue:
    flag: str
    value: str | None


def _subcommand(tokens: tuple[str, ...]) -> Subcommand | None:
    """(kind, +toolchain, args) for a cargo check/clippy or `soldr check`."""

    for i, tok in enumerate(tokens):
        base = tok.rsplit("/", 1)[-1]
        if base in ("soldr", "soldr.exe") and i + 1 < len(tokens) and tokens[i + 1] == "check":
            return Subcommand("check", None, tokens[i + 2 :])
        if not is_cargo_token(tok):
            continue
        j = i + 1
        toolchain = None
        while j < len(tokens) and tokens[j].startswith("+"):
            toolchain = tokens[j][1:]
            j += 1
        if j < len(tokens) and tokens[j] in ("check", "clippy"):
            return Subcommand(tokens[j], toolchain, tokens[j + 1 :])
        return None
    return None


def _split_flag(tok: str) -> FlagValue:
    if tok.startswith("--") and "=" in tok:
        flag, value = tok.split("=", 1)
        return FlagValue(flag, value)
    return FlagValue(tok, None)


def normalize(args: tuple[str, ...]) -> Selection | None:
    if "--" in args:
        args = args[: args.index("--")]
    dims: dict[str, list[str]] = {"pkg": [], "feat": [], "profile": [], "target": [], "tsel": [], "manifest": []}
    i = 0
    while i < len(args):
        tok = args[i]
        if "$" in tok:
            return None
        fv = _split_flag(tok)
        flag, value = fv.flag, fv.value
        if flag in _SWITCHES:
            dim = _SWITCHES[flag]
            if dim is not None:
                dims[dim].append(flag)
            i += 1
            continue
        if flag not in _VALUE_FLAGS:
            return None
        if value is None:
            if i + 1 >= len(args) or "$" in args[i + 1]:
                return None
            value = args[i + 1]
            i += 1
        dim = _VALUE_FLAGS[flag]
        if dim is not None:
            dims[dim].append(f"{flag}={value}")
        i += 1
    return _selection(dims)


def _selection(dims: dict[str, list[str]]) -> Selection:
    pkg_flags = dims["pkg"]
    if any(f in ("--workspace", "--all") for f in pkg_flags):
        excluded = sorted(f for f in pkg_flags if f.startswith("--exclude="))
        packages = frozenset({"*", *excluded})
    else:
        packages = frozenset(f.split("=", 1)[1] for f in pkg_flags if "=" in f) or frozenset({"<default>"})
    tsel: set[str] = set()
    for f in dims["tsel"]:
        if f == "--all-targets":
            tsel |= _ALL_TSEL
        elif "=" in f:
            tsel.add(f)
        else:
            tsel.add(f.lstrip("-"))
    features: set[str] = set()
    for f in dims["feat"]:
        if "=" in f:
            features |= {x for x in re.split(r"[ ,]+", f.split("=", 1)[1]) if x}
        else:
            features.add(f)
    profile = "release" if any(f in ("--release", "-r") for f in dims["profile"]) else "dev"
    for f in dims["profile"]:
        if f.startswith("--profile="):
            profile = f.split("=", 1)[1]
    targets = frozenset(f.split("=", 1)[1] for f in dims["target"]) or frozenset({"<host>"})
    manifest = dims["manifest"][-1] if dims["manifest"] else ""
    return Selection(
        packages=packages,
        tsel=frozenset(tsel) or frozenset({"lib", "bins"}),
        features=frozenset(features),
        profile="dev" if profile == "test" else profile,
        targets=targets,
        manifest=manifest,
    )


def _tsel_covered(check: frozenset[str], clippy: frozenset[str]) -> bool:
    for item in check:
        if item in clippy:
            continue
        kind = item.split("=", 1)[0]
        plural = {"--bin": "bins", "--example": "examples", "--test": "tests", "--bench": "benches"}.get(kind)
        if plural is None or plural not in clippy:
            return False
    return True


def covered(check: Selection, clippy: Selection) -> bool:
    if check.manifest != clippy.manifest or check.profile != clippy.profile or check.features != clippy.features:
        return False
    if not check.targets <= clippy.targets:
        return False
    if "*" in clippy.packages:
        if clippy.packages != frozenset({"*"}) and check.packages != clippy.packages:
            return False
    elif not check.packages <= clippy.packages:
        return False
    return _tsel_covered(check.tsel, clippy.tsel)


def _invocation(tokens: tuple[str, ...], path: str, line: int | None, loc: str, raw: str) -> Invocation | None:
    found = _subcommand(tokens)
    if found is None:
        return None
    return Invocation(found.kind, normalize(found.args), found.toolchain, path, line, loc, raw)


def _job_invocations(repo_root: Path, wf_path: str, raw_lines: list[str], job_id: str, job: dict) -> list[Invocation]:
    out: list[Invocation] = []
    for i, step in enumerate(steps_of(job)):
        run_text = step.get("run")
        if not isinstance(run_text, str):
            continue
        loc = f"jobs.{job_id}.steps[{i}]"
        for ln in logical_lines(run_text):
            raw = with_yaml_comment(ln, raw_lines)
            for tokens in split_commands(ln):
                inv = _invocation(tuple(tokens), wf_path, None, loc, raw)
                if inv is not None:
                    out.append(inv)
                for tok in tokens:
                    if CI_SCRIPT_RE.match(tok) and (repo_root / tok).is_file():
                        out.extend(_script_invocations(repo_root, tok, loc))
    return out


def _script_invocations(repo_root: Path, rel: str, loc: str) -> list[Invocation]:
    script = repo_root / rel
    out: list[Invocation] = []
    for call in script_calls(script):
        inv = _invocation(
            call.tokens, rel, call.line, f"{loc} -> {rel} line {call.line}", source_line(script, call.line)
        )
        if inv is not None:
            out.append(inv)
    return out


def _clause_a(job_id: str, invs: list[Invocation]) -> list[Finding]:
    out: list[Finding] = []
    for idx, chk in enumerate(invs):
        if chk.kind != "check" or allowed(chk.raw, RULE):
            continue
        later = [c for c in invs[idx + 1 :] if c.kind == "clippy" and c.toolchain == chk.toolchain]
        if not later:
            continue
        sel = chk.selection
        if sel is not None and any(c.selection is not None and covered(sel, c.selection) for c in later):
            what = (
                f"job '{job_id}' runs `cargo check` over a selection a later `cargo clippy` in the same job "
                "already covers -- the code is type-checked twice"
            )
            out.append(_finding(chk, what, Status.VIOLATION))
        elif sel is None or any(c.selection is None for c in later):
            what = (
                f"job '{job_id}' runs `cargo check` before a `cargo clippy` but the selections cannot be "
                "compared statically (expression or unrecognized flag)"
            )
            out.append(_finding(chk, what, Status.NEEDS_REVIEW))
    return out


def _finding(inv: Invocation, what: str, status: Status, fix: str = _FIX_A) -> Finding:
    return Finding(rule=RULE, path=inv.path, line=inv.line, message=f"{inv.loc}: {what}", fix=fix, status=status)


# ---- clause (b): MSRV lane on the pinned toolchain ----


def _norm_version(v: str) -> str:
    parts = v.strip().split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts)


def pinned_channel(repo_root: Path) -> str | None:
    toml_path = repo_root / "rust-toolchain.toml"
    if toml_path.is_file():
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return None
        tc = data.get("toolchain")
        channel = tc.get("channel") if isinstance(tc, dict) else None
        return channel if isinstance(channel, str) else None
    plain = repo_root / "rust-toolchain"
    if plain.is_file():
        try:
            text = plain.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None
    return None


def rust_version(repo_root: Path) -> str | None:
    try:
        data = tomllib.loads((repo_root / "Cargo.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for table in (data.get("package"), as_dict(data.get("workspace")).get("package")):
        value = table.get("rust-version") if isinstance(table, dict) else None
        if isinstance(value, str):
            return value
    return None


def _step_toolchains(job: dict) -> list[str]:
    found: list[str] = []
    for step in steps_of(job):
        uses = step.get("uses")
        if isinstance(uses, str) and uses.split("@", 1)[0].endswith("rust-toolchain"):
            with_ = as_dict(step.get("with"))
            tc = with_.get("toolchain")
            if isinstance(tc, (str, int, float)):
                found.append(str(tc))
            elif "@" in uses:
                found.append(uses.split("@", 1)[1])
        run_text = step.get("run")
        if isinstance(run_text, str):
            for ln in logical_lines(run_text):
                for tokens in split_commands(ln):
                    found.extend(_rustup_toolchain(tokens))
    return found


def _rustup_toolchain(tokens: list[str]) -> list[str]:
    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] != "rustup":
            continue
        rest = [t for t in tokens[i + 1 :] if not t.startswith("-")]
        if rest[:2] == ["toolchain", "install"] and len(rest) > 2:
            return [rest[2]]
        if rest[:1] == ["default"] and len(rest) > 1:
            return [rest[1]]
        if rest[:2] == ["override", "set"] and len(rest) > 2:
            return [rest[2]]
    return []


def _clause_b(repo_root: Path, job_id: str, job: dict, invs: list[Invocation]) -> list[Finding]:
    name = job.get("name")
    if not any(_MSRV_RE.search(t) for t in (job_id, name if isinstance(name, str) else "") if t):
        return []
    checks = [c for c in invs if c.kind == "check" and not allowed(c.raw, RULE)]
    channel = pinned_channel(repo_root)
    msrv = rust_version(repo_root)
    if not checks or channel is None or msrv is None or not _VERSION_RE.match(channel.strip()):
        return []
    if _norm_version(msrv) != _norm_version(channel):
        return []
    out: list[Finding] = []
    for chk in checks:
        candidates = [chk.toolchain] if chk.toolchain else _step_toolchains(job)
        toolchain = candidates[-1] if candidates else channel
        if "$" in toolchain or not _VERSION_RE.match(toolchain.strip()):
            out.append(
                _finding(
                    chk,
                    f"MSRV lane '{job_id}' runs `cargo check` on toolchain '{toolchain}', which cannot be compared "
                    f"statically with the pinned channel {channel} (= rust-version)",
                    Status.NEEDS_REVIEW,
                    _FIX_B,
                )
            )
        elif _norm_version(toolchain) == _norm_version(channel):
            out.append(
                _finding(
                    chk,
                    f"MSRV lane '{job_id}' runs `cargo check` on {toolchain}, the pinned rust-toolchain channel, "
                    f"while rust-version is {msrv} -- every lane already compiles on that toolchain",
                    Status.VIOLATION,
                    _FIX_B,
                )
            )
    return out


def check_rust_023(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        raw_lines = raw_lines_of(repo_root / wf.path)
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            invs = _job_invocations(repo_root, wf.path, raw_lines, job_id, job)
            if not invs:
                continue
            findings.extend(_clause_a(job_id, invs))
            findings.extend(_clause_b(repo_root, job_id, job, invs))
    return findings
