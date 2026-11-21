"""GATE-016: lint never runs under bosn; `soldr lint` on the host is the one
lint command (owner directive 2026-11-21, docs/policy-general.md "Lint runs
on the host through `soldr lint`, never under bosn").

`soldr lint` is the aggregate linter -- formatting, Clippy and the
repository's configured Dylint in one command, with `soldr lint --check` as
the CI form -- and it always runs locally. Pushing lint into a bosn or
bosn -> act container gives it the container's cache semantics instead of
soldr's verdict cache, which broke the host. bosn stays the place for tests
and builds; this rule only fences lint out of it.

Static clauses, checked by `ci-lint local-gate lint` and `precheck`:

1. a gate lane that is lint-like -- its id, or its `run` argv (a lint
   command word, a `cargo|soldr clippy/dylint/fmt`, `soldr lint`, a
   `--lane`/`--job` value, or a script whose stem is lint-like) -- with
   `bosn` in `tools`, or whose `run` invokes `bosn`;
2. any lane's (or the gate's own) `run` that is `bosn ci run ... --job <id>`
   with a lint-like job id (`lint`, `clippy`, `dylint`, `fmt`, `format`,
   matched as a word of the id, so `lint-linux-x64` counts);
3. a `bosn.toml` `[task.*]` whose name is lint-like (unless it also names a
   test: `migration-lint-test`, `dylint-ui`), or whose `cmd` runs
   `cargo clippy`/`cargo dylint`/`cargo fmt` (directly or through
   `soldr cargo`), `soldr lint` or a `soldr dylint` pass.

"Lint-like" is word-based: the name is split on every non-alphanumeric
character and matched against `LINT_WORDS`, so `static-checks` is not a lint
lane while `dylint_gate.py` and `lint-linux-x64` are; `ci_lint`/`ci-lint` (the
checker's own name) never counts.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.lane_cache import LaneConfig
from ci_lint.workflow_replay_config import ReplayConfig

RULE = "GATE-016"
LINT_WORDS: frozenset[str] = frozenset(
    {"lint", "lints", "linting", "clippy", "dylint", "fmt", "rustfmt", "format", "formatting"}
)
LINT_COMMANDS: frozenset[str] = LINT_WORDS | frozenset({"cargo-clippy", "cargo-dylint", "cargo-fmt"})
BOSN_COMMANDS: frozenset[str] = frozenset({"bosn", "bosn.exe"})
SCRIPT_SUFFIXES: tuple[str, ...] = (".py", ".sh", ".bash", ".ps1", ".bat", ".cmd")
NAME_FLAGS: frozenset[str] = frozenset({"--lane", "--job"})
# A bosn.toml task whose *name* is lint-like but also names a test is a test of
# a lint feature (a Dylint UI test, a pytest of a lint script), which bosn may
# run; its cmd is still checked.
TEST_WORDS: frozenset[str] = frozenset({"test", "tests", "testing", "ui", "e2e", "smoke"})
FIX = (
    "run lint on the host through `soldr lint` (formatting, Clippy and the configured Dylint in one command; "
    "`soldr lint --check` in CI) as a host soldr lane (`run = [\"soldr\", \"lint\", \"--check\"]`, `tools = [\"soldr\"]`), "
    "which GATE-007 accepts beside the gate command's lanes; never in a bosn or bosn -> act container -- "
    "bosn stays for tests and builds"
)
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
# The checker's own name is not a lint lane: `python3 -m ci_lint precheck`.
_NOT_LINT = re.compile(r"ci[-_]lint")
_LINT_CMD_RE = re.compile(
    r"\bcargo(?:\.exe)?\s+(?:\+\S+\s+)?(?:clippy|dylint|fmt)\b"
    r"|\bcargo-(?:clippy|dylint|fmt)\b"
    r"|\bsoldr(?:\.exe)?\s+lint\b"
    r"|\bsoldr(?:\.exe)?\s+dylint\b(?!\s+prepare\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Subject:
    """The gate's own argv or one lane, as GATE-016 sees it."""

    where: str
    name: str
    run: tuple[str, ...]
    tools: tuple[str, ...]


def cmd_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def lint_like(name: str) -> bool:
    words = _WORD_SPLIT.split(_NOT_LINT.sub(" ", name.lower()))
    return any(word in LINT_WORDS for word in words if word)


def _test_named(name: str) -> bool:
    return any(word in TEST_WORDS for word in _WORD_SPLIT.split(name.lower()))


def runs_lint_command(text: str) -> bool:
    """`cargo clippy`/`cargo dylint`/`cargo fmt` (also as `soldr cargo ...`
    or `cargo +nightly ...`), `cargo-clippy`, `soldr lint`, or a `soldr dylint`
    pass (`soldr dylint prepare` only installs the driver)."""

    return _LINT_CMD_RE.search(text) is not None


def _flag_values(argv: Sequence[str], flags: frozenset[str]) -> tuple[str, ...]:
    values: list[str] = []
    for i, token in enumerate(argv):
        flag, eq, value = token.partition("=")
        if flag not in flags:
            continue
        if eq:
            values.append(value)
        elif i + 1 < len(argv):
            values.append(argv[i + 1])
    return tuple(values)


def _bosn_index(argv: Sequence[str]) -> int | None:
    for i, token in enumerate(argv):
        if cmd_name(token) in BOSN_COMMANDS:
            return i
    return None


def bosn_ci_run_args(argv: Sequence[str]) -> tuple[str, ...] | None:
    """The arguments after `bosn ci run`; None when argv is not one."""

    start = _bosn_index(argv)
    if start is None or tuple(argv[start + 1:start + 3]) != ("ci", "run"):
        return None
    return tuple(argv[start + 3:])


def _bosn_ci_run_jobs(argv: Sequence[str]) -> tuple[str, ...]:
    """The `--job` ids of a `bosn ci run ...` argv; () when it is not one."""

    args = bosn_ci_run_args(argv)
    return () if args is None else _flag_values(args, frozenset({"--job"}))


def script_lint_like(token: str) -> bool:
    """`ci/dylint_gate.py`, `scripts/lint.sh`: a script named for lint."""

    return token.lower().endswith(SCRIPT_SUFFIXES) and lint_like(Path(cmd_name(token)).stem)


def _run_lint_like(argv: Sequence[str]) -> bool:
    if any(cmd_name(token) in LINT_COMMANDS for token in argv):
        return True
    if runs_lint_command(shlex.join(argv)):
        return True
    if any(lint_like(value) for value in _flag_values(argv, NAME_FLAGS)):
        return True
    return any(script_lint_like(token) for token in argv)


def _subject_finding(subject: _Subject) -> Finding | None:
    lint_jobs = [job for job in _bosn_ci_run_jobs(subject.run) if lint_like(job)]
    if lint_jobs:
        return Finding(rule=RULE, path="local gate",
                       message=f"{subject.where} runs lint job '{lint_jobs[0]}' through `bosn ci run`", fix=FIX)
    if not (lint_like(subject.name) or _run_lint_like(subject.run)):
        return None
    reasons = []
    if any(cmd_name(tool) in BOSN_COMMANDS for tool in subject.tools):
        reasons.append("declares 'bosn' in tools")
    if _bosn_index(subject.run) is not None:
        reasons.append("its run invokes bosn")
    if not reasons:
        return None
    return Finding(rule=RULE, path="local gate",
                   message=f"lint {subject.where} {' and '.join(reasons)}", fix=FIX)


def _task_line(text: str, name: str) -> int | None:
    pattern = re.compile(r"^\s*\[\s*task\s*\.\s*([\"']?)" + re.escape(name) + r"\1\s*\]")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return lineno
    return None


def _task_cmd(spec: object) -> str:
    cmd = spec.get("cmd") if isinstance(spec, dict) else None
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list):
        return " ".join(part for part in cmd if isinstance(part, str))
    return ""


def _bosn_task_findings(repo_root: Path) -> list[Finding]:
    manifest = repo_root / "bosn.toml"
    if not manifest.is_file():
        return []
    text = manifest.read_text(encoding="utf-8", errors="replace")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    tasks = doc.get("task")
    findings: list[Finding] = []
    for name, spec in (tasks.items() if isinstance(tasks, dict) else []):
        cmd = _task_cmd(spec)
        if lint_like(name) and not _test_named(name):
            why = "its name is lint-like"
        elif runs_lint_command(cmd):
            why = f"its cmd runs lint: `{cmd.strip()[:100]}`"
        else:
            continue
        findings.append(Finding(rule=RULE, path="bosn.toml", line=_task_line(text, name),
                                message=f"task '{name}' runs lint in a bosn container ({why})", fix=FIX))
    return findings


def check_gate_016(repo_root: Path, gate_run: Sequence[str] = (),
                   lanes: Sequence[LaneConfig] = (), replay_jobs: Sequence[str] = ()) -> list[Finding]:
    """`gate_run`: the gate's own `run` argv; `lanes`: its declared lanes;
    `replay_jobs`: `[gate.replay]` job refs (`ci.yml:fast`). The `bosn.toml`
    clause applies whether or not a gate is declared."""

    from ci_lint.gate_lint_replay import argv_refs, check_replayed_jobs, replay_refs  # noqa: PLC0415

    subjects = [_Subject("the gate", "", tuple(gate_run), ())] if gate_run else []
    subjects.extend(_Subject(f"lane '{lane.id}'", lane.id, tuple(lane.run), tuple(lane.tools)) for lane in lanes)
    findings = [f for f in (_subject_finding(s) for s in subjects) if f is not None]
    refs = [ref for s in subjects for ref in argv_refs(s.run, s.where)]
    findings.extend(check_replayed_jobs(repo_root, [*refs, *replay_refs(replay_jobs)]))
    findings.extend(_bosn_task_findings(repo_root))
    return findings


def gate_replay_jobs(replay: ReplayConfig | None) -> tuple[str, ...]:
    """Every `[[gate.replay.jobs]]` source-job and selection, as `wf:job`."""

    if replay is None:
        return ()
    selected = tuple(sel.selected_job if ":" in sel.selected_job else f"{sel.workflow or replay.workflow}:{sel.selected_job}"
                     for sel in replay.selections if sel.selected_job)
    return tuple(job.source_job for job in replay.jobs) + selected
