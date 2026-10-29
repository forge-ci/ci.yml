"""Local gate first (GATE-001..005, zackees/ci.yml#166, #168).

A pull request should pass remote CI on its first push. The cheapest way to
get there is to make the remote quick gate a *subset* of one local command,
and to refuse remote runner time for a head commit that never passed that
command. This module is the whole mechanism, in four pieces:

- **Declaration.** `[local.gate]` in `ci.toml`, or `[gate]` in a repo-root
  `local-gate.toml` for a repository that has not adopted `ci.toml` (the
  same table either way, parsed by `parse_gate_table`). `run` is the gate's
  argv; `mirrors` names the remote jobs whose `run:` steps must invoke that
  same argv and nothing else; `verify` names the PR-entry job that checks
  the attestation before any other job starts.
- **Attestation.** `ci-lint local-gate run` requires a clean tracked tree,
  runs the gate, refuses if the gate rewrote a tracked file, and on success
  amends HEAD's message with a `Local-Gate: v1 tree=<tree sha> secs=<n>`
  trailer. The trailer is bound to the commit's *tree*, so any amend or
  rebase that changes content invalidates it, while amending only the
  message (which this command itself does) keeps it valid.
- **Enforcement.** `ci-lint local-gate verify` (GATE-003) runs first in the
  PR-entry workflow and fails in seconds when the PR head is not attested
  for its exact tree; every other job `needs:` it (GATE-002), so an
  unattested push costs one small job, not a fan-out. `ci-lint local-gate
  check-push` is the matching git pre-push hook (`local-gate hook install`),
  which stops the push before it reaches GitHub at all.
- **Measurement.** `ci-lint local-gate first-pass` (GATE-004, live, GET-only)
  reports the share of merged PRs whose first pushed head went green on its
  first attempt with no further pushes -- the number this whole mechanism
  exists to move.

The trailer is an honesty contract, not a cryptographic proof: anyone can
type it. What keeps it honest is that the mirrored remote jobs still run
the identical command (GATE-001), so a forged or stale attestation turns
into a visible "attested but red remotely" run in `first-pass`, never into
a silent pass. A repository that opts into GATE-008 (`ci_lint.gate_trust`)
skips those jobs on attested in-policy PR heads; there the default-branch
push run and a 1-in-N audit sample play that role.
"""

from __future__ import annotations

from ci_lint import repo_identity

import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO

from ci_lint.attestations import load_definition as load_attestation_definition
from ci_lint.attestations import make as make_attestation
from ci_lint.attestations import strip_trailers as strip_attestation_trailers
from ci_lint.attestations import MISSING, VALID, verify_commit as verify_gate_attestations
from ci_lint.finding import Finding, Status
from ci_lint.gate_run_lock import hold as hold_gate_run_lock
from ci_lint.lane_log_retention import KEEP_RUNS, new_run_log_dir, prune_run_log_dirs
from ci_lint.workflow_replay_config import ReplayConfig, parse_replay
from ci_lint.workflow_replay_runtime import CheckedCommand, bind_execution_pins, run_checked_command
from ci_lint.workflow_replay_static import check_replay_static
from ci_lint.full_run_receipt import FullRunConfig, load_receipt, parse_full_run, validate_evidence
from ci_lint.gate_isolation import IsolationConfig, check_isolation, parse_isolation
from ci_lint.gate_trust import TrustConfig, WorkflowFacts, WorkflowJob, check_trust_static, parse_trust
from ci_lint.lane_cache import (
    NOT_APPLICABLE_EXIT,
    LaneConfig,
    ToolVersions,
    check_lanes_static,
    lane_key,
    lane_log_dir,
    lookup,
    run_lock_path,
    parse_lanes,
    record,
    tree_entries,
    tree_file,
)
from ci_lint.proc import run_captured
from ci_lint.toml_cursor import Cursor, TomlValue
from ci_lint.yaml_io import YamlValue

GATE_FILE = "local-gate.toml"
TRAILER_KEY = "Local-Gate"
TRAILER_VERSION = "v1"
MODES: tuple[str, ...] = ("enforce", "shadow")
# Bot authors cannot run a local gate; their PRs get the full remote run.
DEFAULT_EXEMPT_AUTHORS: tuple[str, ...] = ("dependabot[bot]", "renovate[bot]", "github-actions[bot]")
# `uses:` actions that only prepare a mirrored job's environment. Any other
# `uses:` step in a mirrored job could run a remote-only check, so it must
# be named in `setup-steps` or it is reported (GATE-001).
SETUP_ACTIONS: frozenset[str] = frozenset(
    {
        "actions/checkout",
        "actions/setup-python",
        "actions/setup-node",
        "actions/setup-go",
        "actions/cache",
        "actions/cache/restore",
        "astral-sh/setup-uv",
        "dtolnay/rust-toolchain",
        *repo_identity.SETUP_SOLDR_REPOS,
    }
)
VERIFY_TOKEN = "local-gate verify"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_TRAILER_RE = re.compile(rf"^{TRAILER_KEY}:\s*(?P<body>.+?)\s*$", re.MULTILINE)
_SHELL_BOILERPLATE = re.compile(r"^(set\s+-[a-zA-Z]+(\s+\S+)*|shopt\s.*|cd\s+\S+)$")


@dataclass(frozen=True)
class JobRef:
    """`<workflow file>:<job id>`, e.g. `ci.yml:lint`."""

    workflow: str
    job: str

    @staticmethod
    def parse(text: str) -> JobRef | None:
        workflow, sep, job = text.partition(":")
        if not sep or not workflow or not job or "/" in workflow:
            return None
        return JobRef(workflow=workflow, job=job)

    def __str__(self) -> str:
        return f"{self.workflow}:{self.job}"


@dataclass(frozen=True)
class GateConfig:
    run: tuple[str, ...]
    mirrors: tuple[JobRef, ...]
    setup_steps: frozenset[str]
    verify: JobRef | None
    verify_exempt: frozenset[str]
    exempt_authors: frozenset[str]
    mode: str
    source: str
    # GATE-005 (zackees/ci.yml#168): how a self-hosted tool's suite runs isolated.
    isolation: IsolationConfig | None = None
    # GATE-007 (zackees/ci.yml#177): the gate split into cacheable lanes, in
    # declared order. Empty: `run` executes as one opaque command.
    lanes: tuple[LaneConfig, ...] = ()
    # One full command may seed individual lane passes only with an exact
    # tree-bound receipt from the command (GATE-007).
    full_run: FullRunConfig | None = None
    # GATE-008 (zackees/ci.yml#190): when an attested head may stand in for
    # the remote quick-gate jobs. None: never.
    trust: TrustConfig | None = None
    replay: ReplayConfig | None = None

    @property
    def command(self) -> str:
        return shlex.join(self.run)


def _valid_replay_lanes(replay: ReplayConfig | None, lanes: tuple[str, ...],
                        source: str, findings: list[Finding]) -> bool:
    if replay is None:
        return True
    if all(scope in lanes for job in replay.jobs for scope in job.lanes) and (
        not lanes or all(job.lanes for job in replay.jobs)
    ):
        return True
    findings.append(Finding(rule="GATE-001", path=source,
                            message="replay lane mappings must use declared lanes and cover every replay job",
                            fix="map replay jobs to existing gate lanes"))
    return False


def parse_gate_table(raw: dict[str, TomlValue], *, path: str, source: str, findings: list[Finding]) -> GateConfig | None:
    """Strictly parse one gate table. Unknown keys are CT-001 and type
    errors CT-002 (the shared `Cursor` contract); semantic errors are
    GATE-001, since an unusable declaration means remote ⊆ local is
    unproven."""

    sub = Cursor(raw, path, findings, source)
    run = sub.list_str("run")
    mirrors_raw = sub.list_str("mirrors", required=False)
    setup_steps = sub.list_str("setup-steps", required=False)
    verify_raw = sub.str_("verify", required=False)
    verify_exempt = sub.list_str("verify-exempt", required=False)
    exempt_authors = sub.list_str("exempt-authors", required=False, default=DEFAULT_EXEMPT_AUTHORS)
    mode = sub.str_("mode", required=False, default="shadow") or "shadow"
    isolation_raw = sub.table_("isolation", required=False)
    lanes_raw = sub.table_("lanes", required=False)
    full_run_raw = sub.table_("full-run", required=False)
    trust_raw = sub.table_("trust", required=False)
    replay_raw = sub.table_("replay", required=False)
    sub.finish()
    replay = (parse_replay(replay_raw, source=source, path=f"{path}.replay", findings=findings)
              if replay_raw is not None else None)
    trust = (
        parse_trust(trust_raw, path=f"{path}.trust", source=source, findings=findings) if trust_raw is not None else None
    )
    lanes = (
        parse_lanes(lanes_raw, path=f"{path}.lanes", source=source, findings=findings) if lanes_raw is not None else ()
    )
    if ("replay" in raw and replay is None) or not _valid_replay_lanes(
        replay, tuple(lane.id for lane in lanes), source, findings
    ):
        return None
    full_run = (
        parse_full_run(full_run_raw, path=f"{path}.full-run", source=source, findings=findings)
        if full_run_raw is not None else None
    )
    isolation = (
        parse_isolation(isolation_raw, path=f"{path}.isolation", source=source, findings=findings)
        if isolation_raw is not None
        else None
    )

    def bad(message: str, fix: str) -> None:
        findings.append(Finding(rule="GATE-001", path=source, message=f"{path}: {message}", fix=fix))

    if not run:
        bad("'run' is empty", "set 'run' to the gate's argv, e.g. [\"python3\", \"ci/local_gate.py\"]")
        return None
    if mode not in MODES:
        bad(f"'mode' is {mode!r}", f"set 'mode' to one of {', '.join(MODES)}")
        mode = "shadow"
    if full_run is not None and sum(not lane.optional for lane in lanes) < 2:
        bad("full-run receipts require at least two non-optional lanes",
            "declare two or more required lanes, or remove [gate.full-run]")
        full_run = None
    mirrors: list[JobRef] = []
    for text in mirrors_raw:
        ref = JobRef.parse(text)
        if ref is None:
            bad(f"mirror {text!r} is not '<workflow file>:<job id>'", "write it as e.g. 'ci.yml:lint'")
        else:
            mirrors.append(ref)
    verify: JobRef | None = None
    if verify_raw is not None:
        verify = JobRef.parse(verify_raw)
        if verify is None:
            bad(f"verify {verify_raw!r} is not '<workflow file>:<job id>'", "write it as e.g. 'ci.yml:ci-mode'")
    return GateConfig(
        run=run,
        mirrors=tuple(mirrors),
        setup_steps=frozenset(setup_steps),
        verify=verify,
        verify_exempt=frozenset(verify_exempt),
        exempt_authors=frozenset(exempt_authors),
        mode=mode,
        source=source,
        isolation=isolation,
        lanes=lanes,
        full_run=full_run,
        trust=trust,
        replay=replay,
    )


@dataclass(frozen=True)
class GateLoad:
    """`config` is None when the repository has not opted in (or the
    declaration is unusable); `findings` are the declaration's own problems."""

    config: GateConfig | None
    findings: list[Finding]


@dataclass(frozen=True)
class _GateTable:
    raw: dict[str, TomlValue]  # the TOML wire boundary, parsed by parse_gate_table
    path: str
    source: str


def load_gate_config(repo_root: Path) -> GateLoad:
    """`ci.toml`'s `[local.gate]` wins; else `local-gate.toml`'s `[gate]`;
    else no config -- the repository has not opted in. Declaring both is a
    GATE-001 violation: two declarations can drift apart."""

    def read(name: str) -> str | None:
        path = repo_root / name
        return path.read_text(encoding="utf-8") if path.is_file() else None

    return _load_gate_config(read)


def load_gate_config_at(repo: Path, rev: str) -> GateLoad:
    """The declaration as committed at `rev` (GATE-008 reads the PR's base)."""

    findings: list[Finding] = []

    def read(name: str) -> str | None:
        try:
            # Only a successfully read tree can prove a declaration is absent.
            if not _git(repo, "ls-tree", rev, "--", name):
                return None
            return _git(repo, "show", f"{rev}:{name}")
        except GitError as exc:
            findings.append(Finding(
                rule="GATE-001", path=name,
                message=f"cannot read committed gate policy at {rev}: {exc}",
                fix="fetch the base commit, tree and policy blobs before verification",
            ))
            return None

    loaded = _load_gate_config(read)
    return GateLoad(None, findings) if findings else loaded


def _load_gate_config(read: Callable[[str], str | None]) -> GateLoad:
    findings: list[Finding] = []
    tables: list[_GateTable] = []
    ci_text = read("ci.toml")
    if ci_text is not None:
        try:
            doc = tomllib.loads(ci_text)
        except tomllib.TOMLDecodeError as exc:
            findings.append(Finding(rule="GATE-001", path="ci.toml",
                                    message=f"invalid TOML: {exc}", fix="fix the TOML syntax"))
            doc = {}
        local = doc.get("local")
        gate = local.get("gate") if isinstance(local, dict) else None
        if isinstance(gate, dict):
            tables.append(_GateTable(gate, "local.gate", "ci.toml"))
    gate_text = read(GATE_FILE)
    if gate_text is not None:
        try:
            doc = tomllib.loads(gate_text)
        except tomllib.TOMLDecodeError as exc:
            return GateLoad(
                None,
                [Finding(rule="GATE-001", path=GATE_FILE, message=f"invalid TOML: {exc}", fix="fix the TOML syntax")],
            )
        top = Cursor(doc, "", findings, GATE_FILE)
        gate = top.table_("gate")
        top.finish()
        if gate is not None:
            tables.append(_GateTable(gate, "gate", GATE_FILE))
    if not tables:
        return GateLoad(None, findings)
    if len(tables) > 1:
        findings.append(
            Finding(
                rule="GATE-001",
                path=GATE_FILE,
                message="the local gate is declared in both ci.toml [local.gate] and local-gate.toml",
                fix="keep one declaration: ci.toml's [local.gate] when the repository has a ci.toml",
            )
        )
    first = tables[0]
    config = parse_gate_table(first.raw, path=first.path, source=first.source, findings=findings)
    return GateLoad(config, findings)


# ── git plumbing ─────────────────────────────────────────────────────────────


class GitError(Exception):
    pass


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    proc = run_captured(["git", "-C", str(repo), *args], input_text=stdin)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def tree_of(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}").strip()


def tracked_changes(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return [line for line in out.splitlines() if line.strip()]


@dataclass(frozen=True)
class Attestation:
    tree: str
    secs: int | None
    # GATE-007 provenance: `lint:run,rust:reused@<key12>,...`; None for an
    # unlaned gate.
    lanes: str | None = None

    def trailer(self) -> str:
        secs = f" secs={self.secs}" if self.secs is not None else ""
        lanes = f" lanes={self.lanes}" if self.lanes else ""
        return f"{TRAILER_KEY}: {TRAILER_VERSION} tree={self.tree}{secs}{lanes}"


def parse_attestation(message: str) -> Attestation | None:
    """The last `Local-Gate:` trailer in a commit message, or None."""

    matches = list(_TRAILER_RE.finditer(message))
    if not matches:
        return None
    fields: dict[str, str] = {}
    tokens = matches[-1].group("body").split()
    if not tokens or tokens[0] != TRAILER_VERSION:
        return None
    for token in tokens[1:]:
        key, sep, value = token.partition("=")
        if sep:
            fields[key] = value
    tree = fields.get("tree", "")
    if not _SHA40.match(tree):
        return None
    secs_text = fields.get("secs", "")
    return Attestation(tree=tree, secs=int(secs_text) if secs_text.isdigit() else None, lanes=fields.get("lanes") or None)


@dataclass(frozen=True)
class CommitCheck:
    """`state`: `attested` (trailer tree == commit tree), `attested-parent`
    (an unattested merge commit whose first parent is attested -- GitHub's
    "Update branch" button), `stale` (trailer present, tree differs: content
    changed after the gate ran), or `missing`."""

    sha: str
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.state in ("attested", "attested-parent")


def check_commit(repo: Path, sha: str, *, _depth: int = 0) -> CommitCheck:
    try:
        message = _git(repo, "log", "-1", "--format=%B", sha)
        tree = tree_of(repo, sha)
        parents = _git(repo, "log", "-1", "--format=%P", sha).split()
    except GitError as exc:
        return CommitCheck(sha, "missing", f"cannot read commit {sha}: {exc}")
    att = parse_attestation(message)
    if att is not None:
        if att.tree == tree:
            return CommitCheck(sha, "attested", f"{TRAILER_KEY} trailer matches tree {tree[:12]}")
        return CommitCheck(
            sha, "stale", f"{TRAILER_KEY} trailer names tree {att.tree[:12]} but the commit's tree is {tree[:12]}"
        )
    if len(parents) >= 2 and _depth == 0:
        first = check_commit(repo, parents[0], _depth=1)
        if first.state == "attested":
            return CommitCheck(sha, "attested-parent", f"merge commit; first parent {parents[0][:12]} is attested")
    return CommitCheck(sha, "missing", f"no {TRAILER_KEY} trailer on {sha[:12]}")


def stamp_head(repo: Path, attestation: Attestation, gate_trailers: tuple[str, ...] = ()) -> str:
    """Rewrite HEAD's message with the trailer (replacing any older one) and
    return the new HEAD sha. The tree is untouched; `--no-verify` skips
    commit hooks because nothing they inspect (the tree) has changed.
    `gate_trailers` (GATE-010 `Ci-Attestation:` lines) replace every older
    one."""

    message = strip_attestation_trailers(_git(repo, "log", "-1", "--format=%B", "HEAD"))
    args = ["interpret-trailers", "--if-exists", "replace", "--trailer", attestation.trailer()]
    for trailer in gate_trailers:
        args += ["--if-exists", "add", "--trailer", trailer]
    new_message = _git(repo, *args, stdin=message)
    _git(repo, "commit", "--amend", "--allow-empty", "--no-verify", "--quiet", "-F", "-", stdin=new_message)
    return _git(repo, "rev-parse", "HEAD").strip()


def gate_attestation_trailers(repo: Path, config: GateConfig, head: str, tree: str,
                              passed: tuple[LanePass, ...]) -> tuple[str, ...]:
    """One `Ci-Attestation:` trailer per declared gate whose lane passed
    (GATE-010). Omitted gates were not run; no definition, no trailers."""

    loaded = load_attestation_definition(repo, lanes=tuple(lane.id for lane in config.lanes))
    if loaded is None or loaded.definition is None:
        return ()
    parents = tuple(_git(repo, "log", "-1", "--format=%P", head).split())
    out: list[str] = []
    for lane in passed:
        lane_config = next(item for item in config.lanes if item.id == lane.lane)
        evidence = lookup(repo, lane_config, lane.key)
        if lane.via == "reused" and evidence is None:
            continue  # Evidence expired or disappeared: never renew it by stamping.
        for gate in loaded.definition.gates_of_lane(lane.lane):
            att = make_attestation(gate.path, tree=tree, parents=parents, lane=lane.lane, key=lane.key,
                                   via=lane.via, secs=lane.secs,
                                   at=int(evidence.passed_at) if evidence is not None else None)
            out.append(att.trailer())
    return tuple(out)


# ── local-gate run ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    message: str
    head: str | None = None


def _changed(repo: Path, head: str) -> str | None:
    """None when the tree is untouched and HEAD did not move, else a message."""

    try:
        after = tracked_changes(repo)
        head_after = _git(repo, "rev-parse", "HEAD").strip()
    except GitError as exc:
        return str(exc)
    if after or head_after != head:
        return (
            "the gate passed but changed the repository (a formatter rewrote files, or HEAD moved); "
            "review and commit the result, then run the gate again:\n  " + "\n  ".join(after[:20])
        )
    return None


@dataclass(frozen=True)
class LanePass:
    lane: str
    key: str
    via: str  # "run" | "reused"
    secs: int | None


@dataclass(frozen=True)
class LaneRun:
    """The outcome of `run_lanes`: `provenance` is one `<lane>:run` or
    `<lane>:reused@<key12>` item per lane that passed, in order; `spent_secs`
    counts only lanes that actually ran."""

    exit_code: int
    provenance: list[str]
    spent_secs: int
    # Per passed lane: its key, how it passed and how long it took, for the
    # ci-attestations trailers (GATE-010, #198).
    passed: tuple[LanePass, ...] = ()


@dataclass(frozen=True)
class LaneOutcome:
    lane: LaneConfig
    key: str
    exit_code: int
    secs: int
    log: Path


def _gate_command(repo: Path, argv: tuple[str, ...], config: GateConfig, head: str, tree: str, *,
                  lane: str | None = None, env: dict[str, str] | None = None,
                  stdout: BinaryIO | None = None) -> CheckedCommand:
    if config.replay is not None and (lane is None or any(lane in job.lanes for job in config.replay.jobs)):
        return run_checked_command(repo, argv, config.replay, head=head, tree=tree,
                                   lane=lane, env=env, stdout=stdout)
    process = subprocess.run(list(argv), cwd=repo, env=env,
                             stdin=subprocess.DEVNULL if stdout is not None else None,
                             stdout=stdout, stderr=subprocess.STDOUT if stdout is not None else None, check=False)
    return CheckedCommand(process.returncode)


def _command_error(command: CheckedCommand, output: BinaryIO) -> None:
    if command.error:
        output.write((command.error + "\n").encode("utf-8"))


def _run_lane(repo: Path, lane: LaneConfig, key: str, log_dir: Path,
              config: GateConfig, head: str, tree: str) -> LaneOutcome:
    """Run one lane with its output written to a log file, never a pipe
    (PY-003), so concurrent lanes cannot interleave or block on output."""

    log = log_dir / f"{lane.id}.log"
    start = time.monotonic()
    with open(log, "wb") as fh:
        try:
            proc = _gate_command(repo, lane.run, config, head, tree, lane=lane.id, stdout=fh)
            _command_error(proc, fh)
            returncode = proc.returncode
        except (OSError, subprocess.SubprocessError) as error:
            # A launch failure is a failed lane, not an exception that prevents
            # independent groups from running and recording their results.
            fh.write(f"lane could not start ({type(error).__name__})\n".encode("utf-8"))
            returncode = 127
    return LaneOutcome(lane, key, returncode, int(round(time.monotonic() - start)), log)


@dataclass(frozen=True)
class _Pending:
    lane: LaneConfig
    key: str


def _heavy_chain(repo: Path, chain: list[_Pending], log_dir: Path,
                 config: GateConfig, head: str, tree: str) -> list[LaneOutcome]:
    """One group's heavy lanes one at a time, in declared order; stop at the
    group's first failure."""

    out: list[LaneOutcome] = []
    for item in chain:
        print(f"local-gate: lane {item.lane.id}: started (heavy)", file=sys.stderr, flush=True)
        outcome = _run_lane(repo, item.lane, item.key, log_dir, config, head, tree)
        _report(outcome)
        out.append(outcome)
        if outcome.exit_code != 0 and not _not_applicable(outcome):
            break
    return out


def _serial_heavy_chains(repo: Path, chains: list[list[_Pending]], log_dir: Path,
                         config: GateConfig, head: str, tree: str) -> list[LaneOutcome]:
    """Run groups sequentially while preserving each group's failure boundary."""
    out: list[LaneOutcome] = []
    for chain in chains:
        out.extend(_heavy_chain(repo, chain, log_dir, config, head, tree))
    return out


def _not_applicable(outcome: LaneOutcome) -> bool:
    return outcome.lane.optional and outcome.exit_code == NOT_APPLICABLE_EXIT


def _report(outcome: LaneOutcome) -> None:
    if _not_applicable(outcome):
        tail = [ln for ln in outcome.log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-1:]
        reason = f" -- {tail[0][:160]}" if tail else ""
        print(f"local-gate: lane {outcome.lane.id}: not applicable on this host (optional; not attested){reason}",
              file=sys.stderr, flush=True)
        return
    if outcome.exit_code == 0:
        tail = [ln for ln in outcome.log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-1:]
        summary = f" -- {tail[0][:140]}" if tail else ""
        print(f"local-gate: lane {outcome.lane.id}: passed in {outcome.secs}s{summary}", file=sys.stderr, flush=True)
        return
    lines = outcome.log.read_text(encoding="utf-8", errors="replace").splitlines()
    print(
        f"local-gate: lane {outcome.lane.id}: FAILED after {outcome.secs}s (exit {outcome.exit_code}); "
        f"full log: {outcome.log}",
        file=sys.stderr,
    )
    print("\n".join(lines[-150:]), file=sys.stderr, flush=True)


def _run_full_lanes(repo: Path, config: GateConfig, head: str, tree: str, reused: dict[str, str],
                    pending: list[_Pending], log_dir: Path) -> LaneRun:
    """Seed narrow lane keys from one full run only after its exact receipt."""

    with tempfile.TemporaryDirectory(prefix="full-run-", dir=log_dir) as scratch:
        return _execute_full_lanes(repo, config, head, tree, reused, pending, log_dir, Path(scratch) / "receipt.json")


def _execution_changed(repo: Path, head: str, config: GateConfig) -> str | None:
    if config.replay is not None:
        try:
            bind_execution_pins(repo, config.replay)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return f"execution provider refused: {exc}"
    return _changed(repo, head)


def _execute_full_lanes(repo: Path, config: GateConfig, head: str, tree: str, reused: dict[str, str],
                        pending: list[_Pending], log_dir: Path, receipt_path: Path) -> LaneRun:
    log = log_dir / "full-run.log"
    env = os.environ.copy()
    env["CI_LINT_GATE_RECEIPT"] = str(receipt_path)
    env["CI_LINT_GATE_TREE"] = tree
    env["CI_LINT_GATE_HEAD"] = head
    print(f"local-gate: full run started for {len(pending)} cold lanes", file=sys.stderr, flush=True)
    start = time.monotonic()
    try:
        with open(log, "wb") as fh:
            proc = _gate_command(repo, config.run, config, head, tree, env=env, stdout=fh)
            _command_error(proc, fh)
    except OSError as exc:
        print(f"local-gate: full run could not start: {exc}", file=sys.stderr)
        return LaneRun(2, [], 0)
    secs = int(round(time.monotonic() - start))
    if proc.returncode != 0:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"local-gate: full run FAILED after {secs}s (exit {proc.returncode}); full log: {log}", file=sys.stderr)
        print("\n".join(lines[-150:]), file=sys.stderr, flush=True)
        return LaneRun(proc.returncode or 1, [], secs)
    expected_lanes = tuple(lane.id for lane in config.lanes)
    optional_lanes = tuple(lane.id for lane in config.lanes if lane.optional)
    loaded = (validate_evidence(proc.full_run, expected_lanes=expected_lanes, optional_lanes=optional_lanes)
              if proc.full_run is not None else
              load_receipt(receipt_path, tree=tree, expected_lanes=expected_lanes, optional_lanes=optional_lanes))
    if loaded.evidence is None:
        print(f"local-gate: full run has no usable lane proof: {loaded.error}; full log: {log}", file=sys.stderr)
        return LaneRun(1, [], secs)
    problem = _execution_changed(repo, head, config)
    if problem is not None:
        print(f"local-gate: {problem}", file=sys.stderr)
        return LaneRun(1, [], secs)
    proofs = {item.lane: item for item in loaded.evidence.passes}
    cold = {item.lane.id: item for item in pending}
    for item in pending:
        if item.lane.id in proofs:
            record(repo, item.lane, item.key, secs=proofs[item.lane.id].secs, head=head, tree=tree)
    provenance: list[str] = []
    passed: list[LanePass] = []
    for lane in config.lanes:
        if lane.id in loaded.evidence.not_applicable:
            provenance.append(f"{lane.id}:n/a")
        elif lane.id in reused:
            provenance.append(f"{lane.id}:reused@{reused[lane.id][:12]}")
            passed.append(LanePass(lane.id, reused[lane.id], "reused", None))
        else:
            item = cold[lane.id]
            provenance.append(f"{lane.id}:run")
            passed.append(LanePass(lane.id, item.key, "run", proofs[lane.id].secs))
    print(f"local-gate: full run passed in {secs}s; receipt proved {len(proofs)} lanes", file=sys.stderr, flush=True)
    return LaneRun(0, provenance, secs, tuple(passed))


def run_lanes(  # noqa: C901
    repo: Path, config: GateConfig, head: str, tree: str, *, use_cache: bool, serial_heavy: bool = False,
) -> LaneRun:
    """Run (or reuse) every declared lane (GATE-007). Cache hits are
    resolved first; then `light` lanes run concurrently alongside the
    `heavy` chains: one per heavy-lane `group` (#399), each running its
    lanes one at a time in declared order. Every lane
    that passed is recorded if the tree is still untouched -- even when
    another lane failed, so fixing that one does not rerun these."""

    entries = tree_entries(repo, tree)
    ci_toml = tree_file(repo, tree, "ci.toml")  # #401: semantic pin hashing per lane
    versions = ToolVersions()
    replay = bind_execution_pins(repo, config.replay) if config.replay is not None else None
    config = replace(config, replay=replay)
    log_dir = new_run_log_dir(lane_log_dir(repo))
    reused: dict[str, str] = {}
    pending: list[_Pending] = []
    for lane in config.lanes:
        key = lane_key(entries, lane, gate_run=(*config.run, *(replay.provider_query if replay else ())),
                       gate_source=config.source, versions=versions,
                       provider=replay.execution_pins if replay else None, ci_toml=ci_toml)
        hit = lookup(repo, lane, key.key) if use_cache else None
        if hit is not None:
            print(
                f"local-gate: lane {lane.id}: reused (inputs unchanged since a pass {hit.age_hours(time.time()):.1f} h ago "
                f"that took {hit.secs}s; key {key.key[:12]}, {key.inputs} inputs)",
                file=sys.stderr,
                flush=True,
            )
            reused[lane.id] = key.key
        else:
            pending.append(_Pending(lane, key.key))
    if not serial_heavy and config.full_run is not None and len(pending) >= config.full_run.min_misses:
        return _run_full_lanes(repo, config, head, tree, reused, pending, log_dir)
    light = [p for p in pending if p.lane.weight == "light"]
    groups: dict[str, list[_Pending]] = {}
    for p in pending:
        if p.lane.weight != "light":
            groups.setdefault(p.lane.group, []).append(p)
    outcomes: list[LaneOutcome] = []
    with ThreadPoolExecutor(max_workers=len(light) + max(len(groups), 1)) as pool:
        futures = []
        for p in light:
            print(f"local-gate: lane {p.lane.id}: started (light)", file=sys.stderr, flush=True)
            futures.append(pool.submit(_run_lane, repo, p.lane, p.key, log_dir, config, head, tree))
        if serial_heavy and groups:
            chains = [pool.submit(_serial_heavy_chains, repo, list(groups.values()), log_dir, config, head, tree)]
        else:
            chains = [pool.submit(_heavy_chain, repo, chain, log_dir, config, head, tree) for chain in groups.values()]
        for future in as_completed(futures):
            outcome = future.result()
            _report(outcome)
            outcomes.append(outcome)
        for chain_future in chains:
            outcomes.extend(chain_future.result())
    problem = _execution_changed(repo, head, config)
    if problem is not None:
        print(f"local-gate: {problem}", file=sys.stderr)
    else:
        for outcome in outcomes:
            if outcome.exit_code == 0:
                record(repo, outcome.lane, outcome.key, secs=outcome.secs, head=head, tree=tree)
    by_id = {o.lane.id: o for o in outcomes}
    provenance: list[str] = []
    passed: list[LanePass] = []
    for lane in config.lanes:
        if lane.id in reused:
            provenance.append(f"{lane.id}:reused@{reused[lane.id][:12]}")
            passed.append(LanePass(lane.id, reused[lane.id], "reused", None))
        elif lane.id in by_id and by_id[lane.id].exit_code == 0:
            provenance.append(f"{lane.id}:run")
            passed.append(LanePass(lane.id, by_id[lane.id].key, "run", by_id[lane.id].secs))
        elif lane.id in by_id and _not_applicable(by_id[lane.id]):
            provenance.append(f"{lane.id}:n/a")
    failed = [o for o in outcomes if o.exit_code != 0 and not _not_applicable(o)]
    not_run = [p.lane.id for p in pending if p.lane.id not in by_id]
    if not_run:
        print(f"local-gate: not run after a heavy-lane failure: {', '.join(not_run)}", file=sys.stderr)
    spent = sum(o.secs for o in outcomes)
    if failed:
        return LaneRun(failed[0].exit_code or 1, provenance, spent, tuple(passed))
    if problem is not None:
        return LaneRun(1, provenance, spent, tuple(passed))
    return LaneRun(0, provenance, spent, tuple(passed))


def _replay_coverage_problem(config: GateConfig, repo: Path) -> str | None:
    if config.replay is None:
        return None
    findings = check_replay_static(config.replay, repo)
    if not findings:
        return None
    return "local-gate run: replay coverage is unproven:\n  " + "\n  ".join(item.message for item in findings)


def _retain_lane_logs(repo: Path) -> None:
    """GEN-030: the run is over and the per-worktree run lock is still held,
    so no gate run of this worktree is active; keep the newest KEEP_RUNS."""

    try:
        report = prune_run_log_dirs(lane_log_dir(repo), keep=KEEP_RUNS)
    except (OSError, GitError, subprocess.SubprocessError):
        return
    if report.removed:
        print(
            f"local-gate: lane-log retention kept {len(report.kept)} run(s), removed {len(report.removed)} "
            f"({report.freed_bytes} bytes freed; GEN-030 keeps the last {KEEP_RUNS} per worktree)",
            file=sys.stderr,
        )


def run_gate(
    repo: Path, config: GateConfig, *, stamp: bool = True, force: bool = False,
    use_cache: bool = True, serial_heavy: bool = False,
) -> RunOutcome:
    try:
        with hold_gate_run_lock(run_lock_path(repo)):
            try:
                return _run_gate(repo, config, stamp=stamp, force=force, use_cache=use_cache, serial_heavy=serial_heavy)
            finally:
                _retain_lane_logs(repo)
    except (OSError, GitError, ValueError, subprocess.SubprocessError) as error:
        return RunOutcome(2, f"local-gate run: {error}")


def _run_gate(  # noqa: C901
    repo: Path, config: GateConfig, *, stamp: bool = True, force: bool = False,
    use_cache: bool = True, serial_heavy: bool = False,
) -> RunOutcome:
    try:
        dirty = tracked_changes(repo)
        head = _git(repo, "rev-parse", "HEAD").strip()
    except GitError as exc:
        return RunOutcome(2, f"local-gate run: {exc}")
    if dirty:
        return RunOutcome(
            2,
            "local-gate run: tracked files have uncommitted changes; the gate attests a commit's tree, "
            "so commit (or stash) them first:\n  " + "\n  ".join(dirty[:20]),
        )
    if serial_heavy and not config.lanes:
        return RunOutcome(2, "local-gate run: --serial-heavy requires declared lanes")
    replay_problem = _replay_coverage_problem(config, repo)
    if replay_problem:
        return RunOutcome(1, replay_problem)
    if config.replay is not None:
        config = replace(config, replay=bind_execution_pins(repo, config.replay))
    already_attested = check_commit(repo, head).state == "attested"
    if (not config.lanes and not force and use_cache and already_attested
            and not (config.replay and config.replay.provider_query)):
        return RunOutcome(0, f"local-gate run: HEAD {head[:12]} is already attested for its tree", head)
    tree = tree_of(repo, head)
    if config.lanes:
        start = time.monotonic()
        lane_run = run_lanes(repo, config, head, tree, use_cache=use_cache, serial_heavy=serial_heavy)
        secs = int(round(time.monotonic() - start))
        lanes_field = ",".join(lane_run.provenance)
        if lane_run.exit_code != 0:
            return RunOutcome(lane_run.exit_code, f"local-gate run: FAILED after {secs}s ({lanes_field or 'no lane passed'})")
        if already_attested and not force and all(item.via == "reused" for item in lane_run.passed):
            return RunOutcome(0, f"local-gate run: HEAD {head[:12]} is already attested; lane evidence revalidated", head)
        if not stamp:
            return RunOutcome(0, f"local-gate run: passed in {secs}s [{lanes_field}] (not stamped)", head)
        trailers = gate_attestation_trailers(repo, config, head, tree, lane_run.passed)
        new_head = stamp_head(repo, Attestation(tree=tree, secs=secs, lanes=lanes_field), trailers)
        return RunOutcome(
            0, f"local-gate run: passed in {secs}s [{lanes_field}]; stamped {new_head[:12]} ({TRAILER_KEY} tree={tree[:12]})",
            new_head,
        )
    print(f"local-gate run: {config.command}", file=sys.stderr, flush=True)
    start = time.monotonic()
    proc = _gate_command(repo, config.run, config, head, tree)
    if proc.error:
        print(proc.error, file=sys.stderr)
    secs = int(round(time.monotonic() - start))
    if proc.returncode != 0:
        return RunOutcome(proc.returncode or 1, f"local-gate run: FAILED after {secs}s (exit {proc.returncode})")
    try:
        after = tracked_changes(repo)
        head_after = _git(repo, "rev-parse", "HEAD").strip()
    except GitError as exc:
        return RunOutcome(2, f"local-gate run: {exc}")
    if after or head_after != head:
        return RunOutcome(
            1,
            "local-gate run: the gate passed but changed the repository (a formatter rewrote files, or HEAD "
            "moved); review and commit the result, then run the gate again:\n  " + "\n  ".join(after[:20]),
        )
    provider_problem = _execution_changed(repo, head, config)
    if provider_problem is not None:
        return RunOutcome(1, f"local-gate run: {provider_problem}")
    if not stamp:
        return RunOutcome(0, f"local-gate run: passed in {secs}s (not stamped)", head)
    new_head = stamp_head(repo, Attestation(tree=tree, secs=secs))
    return RunOutcome(0, f"local-gate run: passed in {secs}s; stamped {new_head[:12]} ({TRAILER_KEY} tree={tree[:12]})", new_head)


# ── pre-push hook ────────────────────────────────────────────────────────────

ZERO_SHA = "0" * 40
HOOK_MARKER = "# installed by: ci-lint local-gate hook install (zackees/ci.yml#166)"
# Matches any managed hook, including ones written by an older ci-lint, so a
# header change never strands a hook that `hook install` could refresh.
HOOK_MARKER_PREFIX = "# installed by: ci-lint"
CUSTOM_BACKUP_SUFFIX = ".pre-ci-lint"
WIP_PUSH_ENV = "CI_LINT_GATE_WIP_PUSH"


@dataclass(frozen=True)
class PushCheck:
    exit_code: int
    problems: list[str]
    # Unattested heads admitted by an explicit WIP reason (ci.yml#406).
    wip: list[str] = field(default_factory=list)
    # Unattested heads let through because the pushed policy is `mode = "shadow"`.
    shadow: list[str] = field(default_factory=list)


def _publication_problems(repo: Path, sha: str, config: GateConfig) -> tuple[str, ...]:
    """Enrolled consumers publish fresh gate proof from the outgoing tree."""
    if config.replay is None or not config.replay.qualified or not config.replay.provider_query:
        return ()
    if not config.lanes:
        return ("enrolled publication requires declared gate lanes",)
    definition = load_attestation_definition(repo, rev=sha, lanes=tuple(lane.id for lane in config.lanes))
    if definition is None or definition.definition is None:
        return ("enrolled publication requires ci-attestations.yml in the outgoing commit",)
    if definition.findings:
        return tuple(f"outgoing attestation definition: {finding.message}" for finding in definition.findings)
    required_lanes = {lane.id for lane in config.lanes if not lane.optional}
    required = {gate.path for gate in definition.definition.gates if gate.lane in required_lanes}
    commit = verify_gate_attestations(repo, sha, definition.definition,
                                     max_age_hours={lane.id: lane.max_age_hours for lane in config.lanes})
    problems = tuple(f"malformed gate attestation: {message}" for message in commit.malformed)
    return problems + tuple(f"{status.gate}: {status.state}: {status.detail}" for status in commit.statuses
                            if status.state != VALID and (status.gate in required or status.state != MISSING))


def check_push(repo: Path, stdin_text: str, *, wip_reason: str | None = None) -> PushCheck:
    """git's pre-push stdin: `<local ref> <local sha> <remote ref> <remote sha>`
    per line. Every pushed branch head must be attested; tags and deletes
    pass. A non-blank `wip_reason` admits unattested heads only (a paused
    handoff branch, ci.yml#406); gate-policy and publication problems still
    refuse, and hosted `local-gate verify` still fails the unattested PR.
    Under the pushed policy's `mode = "shadow"` an unattested head is reported
    in `PushCheck.shadow` and the push passes (talos-hq/clud#2265); `enforce`
    refuses it."""

    wip_reason = (wip_reason or "").strip() or None
    problems: list[str] = []
    wip: list[str] = []
    shadow: list[str] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, _remote_sha = parts
        if local_sha == ZERO_SHA or remote_ref.startswith("refs/tags/"):
            continue
        loaded = load_gate_config_at(repo, local_sha)
        if loaded.findings:
            problems.extend(f"{local_ref} -> {remote_ref}: outgoing gate policy: {finding.message}"
                            for finding in loaded.findings)
            continue
        if loaded.config is None:
            continue
        result = check_commit(repo, local_sha)
        if not result.ok:
            detail = f"{local_ref} -> {remote_ref}: {result.detail}"
            if wip_reason:
                wip.append(detail)
            elif loaded.config.mode == "shadow":
                shadow.append(detail)
            else:
                problems.append(detail)
        else:
            problems.extend(f"{local_ref} -> {remote_ref}: {problem}"
                            for problem in _publication_problems(repo, local_sha, loaded.config))
    return PushCheck(exit_code=1 if problems else 0, problems=problems, wip=wip, shadow=shadow)


def hook_script(launcher: str) -> str:
    # The launcher names the interpreter that installed the hook, which under
    # `uvx` is a temporary path inside uv's cache. uv garbage-collects that
    # cache, so the hook silently stops gating every later push once the
    # interpreter is gone -- the worst failure a gate can have. Probe the
    # launcher first and refuse loudly if it died, rather than exec'ing a
    # missing binary and failing opaquely at push time.
    #
    # The probe is `local-gate --help`, not `--version`: `--version` is not a
    # valid ci_lint invocation (argparse rejects it with exit 2), so probing on
    # it would report every healthy hook as broken.
    probe = f"{launcher} local-gate --help >/dev/null 2>&1"
    lines = [
        "#!/bin/sh",
        HOOK_MARKER.rstrip("\n"),
        f"if ! {probe}; then",
        '  echo "ci-lint local-gate: this pre-push hook can no longer run ci-lint." >&2',
        '  echo "  The interpreter that installed it was probably a temporary uv/venv path." >&2',
        '  echo "  Reinstall with a durable launcher, for example:" >&2',
        '  echo "    ci-lint local-gate install-hook --force --launcher \\"env PYTHONPATH=/path/to/ci.yml python3 -m ci_lint\\"" >&2',
        '  echo "  or run the gate by hand before pushing: ci-lint local-gate run" >&2',
        "  exit 1",
        "fi",
        f'exec {launcher} local-gate check-push "$@"',
        "",
    ]
    return "\n".join(lines)


def default_launcher() -> str:
    package_parent = Path(__file__).resolve().parent.parent
    return f"env PYTHONPATH={shlex.quote(str(package_parent))} {shlex.quote(sys.executable)} -m ci_lint"


@dataclass(frozen=True)
class HookInstall:
    exit_code: int
    message: str


def install_hook(repo: Path, launcher: str, *, force: bool = False) -> HookInstall:
    try:
        hooks_dir = Path(_git(repo, "rev-parse", "--git-path", "hooks").strip())
    except GitError as exc:
        return HookInstall(2, f"local-gate hook install: {exc}")
    if not hooks_dir.is_absolute():
        hooks_dir = repo / hooks_dir
    hook = hooks_dir / "pre-push"
    existing = hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else None
    managed = existing is not None and HOOK_MARKER_PREFIX in existing
    if existing is not None and not managed and not force:
        return HookInstall(
            1,
            f"local-gate hook install: {hook} already exists and was not installed by ci-lint; "
            "call `local-gate check-push` from it yourself, or pass --force to replace it "
            f"(the old hook is kept as {hook.name}{CUSTOM_BACKUP_SUFFIX})",
        )
    script = hook_script(launcher)
    if existing == script:
        return HookInstall(0, f"local-gate hook install: {hook} is already current")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if existing is not None and not managed:
        backup = hook.with_name(hook.name + CUSTOM_BACKUP_SUFFIX)
        backup.write_text(existing, encoding="utf-8")
        backup.chmod(0o755)
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)
    verb = "refreshed" if managed else "wrote"
    return HookInstall(0, f"local-gate hook install: {verb} {hook}")


# ── CI-side verify (GATE-003) ────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyOutcome:
    exit_code: int
    attested: bool
    state: str
    message: str


def verify(
    repo: Path, config: GateConfig, *, sha: str, event: str, author: str | None
) -> VerifyOutcome:
    """Only `pull_request`/`pull_request_target` heads are judged: a push to
    the default branch is a merge of an already-verified PR, and schedule or
    dispatch runs are not an author's iteration loop."""

    if event not in ("pull_request", "pull_request_target"):
        return VerifyOutcome(0, False, "not-applicable", f"[GATE-003] event '{event}' is not a pull request; nothing to verify")
    if author is not None and author in config.exempt_authors:
        return VerifyOutcome(0, False, "exempt", f"[GATE-003] author '{author}' is exempt; the full remote run applies")
    result = check_commit(repo, sha)
    if result.ok:
        return VerifyOutcome(0, True, result.state, f"[GATE-003] {sha[:12]} {result.state}: {result.detail}")
    fix = (
        f"run the local gate and push its stamped commit: `ci-lint local-gate run` (runs {config.command}), "
        "then `git push --force-with-lease`"
    )
    message = f"[GATE-003] {sha[:12]} not attested ({result.state}): {result.detail}\n    fix:   {fix}"
    if config.mode == "shadow":
        return VerifyOutcome(
            0,
            False,
            result.state,
            message + "\n    (mode = shadow: not enforced — the full remote suite runs instead of skipping)",
        )
    return VerifyOutcome(1, False, result.state, message)


# ── static rules (GATE-001, GATE-002) ────────────────────────────────────────


def _logical_lines(script: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", script)
    out: list[str] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _SHELL_BOILERPLATE.match(line):
            continue
        out.append(re.sub(r"\s+", " ", line))
    return out


def invokes_gate(line: str, config: GateConfig) -> bool:
    """A line invokes the gate when it *starts* with the gate argv (extra
    trailing arguments such as `--lane lint` are allowed), optionally after
    leading `VAR=value` environment assignments."""

    tokens = line.split()
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    return tuple(tokens[: len(config.run)]) == config.run


@dataclass(frozen=True)
class _ParsedWorkflow:
    name: str  # file name under .github/workflows/
    doc: dict[str, YamlValue]  # the YAML wire boundary


@dataclass
class _Workflows:
    parsed: list[_ParsedWorkflow] = field(default_factory=list)
    unreadable: set[str] = field(default_factory=set)

    def doc(self, name: str) -> dict[str, YamlValue] | None:
        for wf in self.parsed:
            if wf.name == name:
                return wf.doc
        return None


def _load_workflows(repo_root: Path) -> _Workflows:
    from ci_lint.workflow_scan import (  # noqa: PLC0415 -- YAML tooling is optional
        as_dict,
        load_workflows,
    )
    from ci_lint.yaml_io import LoadStatus  # noqa: PLC0415

    out = _Workflows()
    for wf in load_workflows(repo_root):
        name = Path(wf.path).name
        if wf.status != LoadStatus.OK:
            out.unreadable.add(name)
        else:
            out.parsed.append(_ParsedWorkflow(name, as_dict(wf.document)))
    return out


def check_gate_static(config: GateConfig, repo_root: Path) -> list[Finding]:  # noqa: C901
    from ci_lint.workflow_scan import jobs_of, steps_of  # noqa: PLC0415

    findings: list[Finding] = check_isolation(config.isolation, config.run, repo_root, config.source,
                                           replay=config.replay, lanes=config.lanes)
    if config.replay is not None:
        findings.extend(check_replay_static(config.replay, repo_root))
    findings.extend(_check_attestation_definition(config, repo_root))
    findings.extend(check_lanes_static(config.lanes, config.run, config.source, repo_root))
    wfs = _load_workflows(repo_root)
    findings.extend(_check_trust(config, wfs))
    if not wfs.parsed and not wfs.unreadable and not config.mirrors and config.verify is None:
        # No remote CI at all (e.g. zackees/ci.yml itself): nothing to mirror
        # or verify; the pre-push hook is the whole enforcement.
        return findings

    def job_of(ref: JobRef, rule: str) -> dict[str, YamlValue] | None:
        path = f".github/workflows/{ref.workflow}"
        if ref.workflow in wfs.unreadable:
            findings.append(
                Finding(rule=rule, path=path, status=Status.NEEDS_REVIEW, message=f"cannot parse {ref.workflow}",
                        fix="install PyYAML or yq so the workflow can be checked")
            )
            return None
        doc = wfs.doc(ref.workflow)
        job = jobs_of(doc).get(ref.job) if doc is not None else None
        if job is None:
            findings.append(
                Finding(rule=rule, path=config.source, message=f"declared job '{ref}' does not exist",
                        fix=f"point the declaration at a real job, or add job '{ref.job}' to {path}")
            )
        return job

    # GATE-001: every mirrored remote job runs the gate and nothing else.
    if not config.mirrors:
        findings.append(
            Finding(
                rule="GATE-001",
                path=config.source,
                status=Status.NEEDS_REVIEW,
                message="no remote job is declared as a mirror of the local gate, so remote ⊆ local is unproven",
                fix="list the quick-gate job(s) in 'mirrors' and make their run steps invoke the gate command",
            )
        )
    for ref in config.mirrors:
        job = job_of(ref, "GATE-001")
        if job is None:
            continue
        path = f".github/workflows/{ref.workflow}"
        if "uses" in job:
            findings.append(
                Finding(rule="GATE-001", path=path, status=Status.NEEDS_REVIEW,
                        message=f"mirrored job '{ref.job}' is a reusable-workflow call; its steps cannot be checked here",
                        fix="mirror the called workflow's job instead")
            )
            continue
        invoked = False
        for index, step in enumerate(steps_of(job)):
            name = step.get("name")
            label = name if isinstance(name, str) else f"step {index + 1}"
            if isinstance(name, str) and name in config.setup_steps:
                continue
            uses = step.get("uses")
            if isinstance(uses, str):
                slug = uses.split("@", 1)[0]
                if slug not in SETUP_ACTIONS:
                    findings.append(
                        Finding(
                            rule="GATE-001",
                            path=path,
                            message=f"mirrored job '{ref.job}' step '{label}' uses '{slug}', which may run a remote-only check",
                            fix=f"move the check into the gate ({config.command}), or list the step name in 'setup-steps' if it only prepares the environment",
                        )
                    )
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for line in _logical_lines(run):
                if invokes_gate(line, config):
                    invoked = True
                    continue
                findings.append(
                    Finding(
                        rule="GATE-001",
                        path=path,
                        message=f"mirrored job '{ref.job}' step '{label}' runs `{line[:120]}`, which the local gate does not",
                        fix=f"move that command into the gate ({config.command}) and have this step invoke the gate, "
                        "or list the step in 'setup-steps' if it only prepares the environment",
                    )
                )
        if not invoked:
            findings.append(
                Finding(rule="GATE-001", path=path,
                        message=f"mirrored job '{ref.job}' never invokes the gate command `{config.command}`",
                        fix="add a step that runs the gate command (lane arguments may follow it)")
            )

    # GATE-002: the PR entry verifies the attestation before anything else runs.
    if config.verify is None:
        findings.append(
            Finding(rule="GATE-002", path=config.source,
                    message="no 'verify' job is declared, so an unattested PR head still gets the full remote run",
                    fix="declare verify = '<workflow>:<job>' and run `ci-lint local-gate verify` in that job")
        )
        return findings
    ref = config.verify
    job = job_of(ref, "GATE-002")
    if job is None:
        return findings
    path = f".github/workflows/{ref.workflow}"
    runs = [s.get("run") for s in steps_of(job)]
    if not any(isinstance(r, str) and VERIFY_TOKEN in r for r in runs):
        findings.append(
            Finding(rule="GATE-002", path=path,
                    message=f"verify job '{ref.job}' has no step running `ci-lint {VERIFY_TOKEN}`",
                    fix=f"add a step: python3 -m ci_lint {VERIFY_TOKEN} --repo .")
        )
    if "if" in job:
        findings.append(
            Finding(rule="GATE-002", path=path, status=Status.NEEDS_REVIEW,
                    message=f"verify job '{ref.job}' has an 'if:'; confirm it always runs on pull_request",
                    fix="drop the job-level 'if:' (verify itself skips non-PR events)")
        )
    verify_doc = wfs.doc(ref.workflow)
    assert verify_doc is not None  # job_of() returned the verify job from it
    jobs = jobs_of(verify_doc)

    def needs_of(job_id: str) -> list[str]:
        raw = jobs.get(job_id, {}).get("needs")
        if isinstance(raw, str):
            return [raw]
        return [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []

    def reaches(job_id: str) -> bool:
        seen: set[str] = set()
        stack = needs_of(job_id)
        while stack:
            dep = stack.pop()
            if dep == ref.job:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.extend(needs_of(dep))
        return False

    findings.extend(_ungated_pr_workflows(config, wfs))
    for job_id in jobs:
        if job_id == ref.job or job_id in config.verify_exempt:
            continue
        if not reaches(job_id):
            findings.append(
                Finding(
                    rule="GATE-002",
                    path=path,
                    message=f"job '{job_id}' does not (transitively) need verify job '{ref.job}', so it spends runner time on an unattested head",
                    fix=f"add '{ref.job}' to its needs: (directly or through a job it already needs), or list it in 'verify-exempt' "
                    "if it must run first (e.g. ci-pre)",
                )
            )
    return findings


def _pr_triggered(doc: dict[str, YamlValue]) -> bool:
    from ci_lint.workflow_scan import get_on_section  # noqa: PLC0415

    on = get_on_section(doc)
    return "pull_request" in on or "pull_request_target" in on


def _workflow_gated(doc: dict[str, YamlValue], exempt: frozenset[str]) -> bool:
    """Every non-exempt job runs `local-gate verify` itself or (transitively)
    needs a job that does."""

    from ci_lint.workflow_scan import jobs_of, steps_of  # noqa: PLC0415

    jobs = jobs_of(doc)
    verifiers = {
        job_id for job_id, job in jobs.items()
        if any(isinstance(s.get("run"), str) and VERIFY_TOKEN in str(s.get("run")) for s in steps_of(job))
    }
    if not verifiers:
        return False

    def needs_of(job_id: str) -> list[str]:
        raw = jobs.get(job_id, {}).get("needs")
        if isinstance(raw, str):
            return [raw]
        return [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []

    def reaches(job_id: str) -> bool:
        seen: set[str] = set()
        stack = needs_of(job_id)
        while stack:
            dep = stack.pop()
            if dep in verifiers:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.extend(needs_of(dep))
        return False

    return all(j in verifiers or j in exempt or reaches(j) for j in jobs)


def _ungated_pr_workflows(config: GateConfig, wfs: _Workflows) -> list[Finding]:
    """GATE-002 cross-workflow half: the verify job gates only its own
    workflow. Every other PR-triggered workflow whose jobs do not sit behind
    a `local-gate verify` job of their own still spends runner time on an
    unattested head (mimalloc-pprof: ~15 PR workflows, one gated)."""

    assert config.verify is not None
    exempt = config.verify_exempt | {
        e.split(":", 1)[1] for e in config.verify_exempt if ":" in e
    }
    ungated = sorted(
        wf.name for wf in wfs.parsed
        if wf.name != config.verify.workflow
        and wf.name not in config.verify_exempt
        and _pr_triggered(wf.doc)
        and not _workflow_gated(wf.doc, exempt)
    )
    if not ungated:
        return []
    return [Finding(
        rule="GATE-002", path=config.source, status=Status.NEEDS_REVIEW,
        message=f"verify job '{config.verify}' gates only {config.verify.workflow}; {len(ungated)} other "
        f"PR-triggered workflow(s) run jobs that need no verify job: {', '.join(ungated)}",
        fix="add a `ci-lint local-gate verify` job to each and make its jobs need it, fold them into the verified "
        "workflow, path-filter them off PRs, or list the workflow file name in 'verify-exempt'",
    )]


def _check_attestation_definition(config: GateConfig, repo_root: Path) -> list[Finding]:
    """GATE-010 static half: a present ci-attestations.yml parses as
    restricted YAML, names declared lanes, and maps every trusted skip job
    (an unmapped one can never skip)."""

    loaded = load_attestation_definition(repo_root, lanes=tuple(lane.id for lane in config.lanes))
    if loaded is None:
        return []
    findings = list(loaded.findings)
    definition = loaded.definition
    if definition is not None and config.trust is not None:
        for job in config.trust.skip:
            if definition.job(job) is None:
                findings.append(Finding(
                    rule="GATE-010", path=definition.source, status=Status.NEEDS_REVIEW,
                    message=f"[gate.trust] skip job '{job}' is not mapped to gates, so it never skips",
                    fix=f"add `{job}: [<gate paths>]` under `jobs:`"))
    return findings


def _push_triggered(wfs: _Workflows) -> frozenset[str]:
    """Workflow files that run on a push: an `on: push` trigger, or an
    `on: workflow_call` reusable workflow a push-triggered workflow calls
    with `uses: ./.github/workflows/<file>` (it runs inside that push run,
    so it inherits the post-merge catch; obs-rust/obs-studio#13)."""
    from ci_lint.workflow_scan import get_on_section, jobs_of  # noqa: PLC0415

    triggered = {wf.name for wf in wfs.parsed if "push" in get_on_section(wf.doc)}
    reusable = {wf.name for wf in wfs.parsed if "workflow_call" in get_on_section(wf.doc)}
    callees: dict[str, frozenset[str]] = {}
    for wf in wfs.parsed:
        called: set[str] = set()
        for job in jobs_of(wf.doc).values():
            uses = job.get("uses")
            if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
                called.add(uses.split("@", 1)[0].rsplit("/", 1)[-1])
        callees[wf.name] = frozenset(called & reusable)
    pending = list(triggered)
    while pending:
        for callee in callees.get(pending.pop(), frozenset()) - triggered:
            triggered.add(callee)
            pending.append(callee)
    return frozenset(triggered)


def _check_trust(config: GateConfig, wfs: _Workflows) -> list[Finding]:
    from ci_lint.workflow_scan import jobs_of  # noqa: PLC0415

    if config.trust is None:
        return []
    jobs: list[WorkflowJob] = []
    for wf in wfs.parsed:
        for job_id, job in jobs_of(wf.doc).items():
            cond = job.get("if")
            steps = job.get("steps")
            runs_verify = isinstance(steps, list) and any(
                isinstance(step, dict) and isinstance(step.get("run"), str) and "local-gate verify" in step["run"]
                for step in steps
            )
            jobs.append(WorkflowJob(wf.name, job_id, cond if isinstance(cond, str) else None, runs_verify))
    facts = WorkflowFacts(
        jobs=tuple(jobs),
        push_triggered=_push_triggered(wfs),
    )
    return check_trust_static(
        config.trust,
        verify_job=str(config.verify) if config.verify else None,
        mirrors=tuple(str(m) for m in config.mirrors),
        lane_ids=tuple(lane.id for lane in config.lanes),
        source=config.source,
        workflows=facts,
    )


def hook_installed(repo: Path) -> bool:
    try:
        hooks_dir = Path(_git(repo, "rev-parse", "--git-path", "hooks").strip())
    except GitError:
        return False
    hook = (hooks_dir if hooks_dir.is_absolute() else repo / hooks_dir) / "pre-push"
    return hook.is_file() and HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace")
