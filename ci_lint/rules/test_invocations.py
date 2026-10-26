"""Shared scanner for Rust test invocations (`cargo nextest run`, `cargo test`).

Used by the running-process-derived rules (zackees/ci.yml#79/#81, M2-40):
`RUST-017` (`--no-capture` serializes nextest) and `RUST-015` (a test filter
never limits compilation). Both follow GEN-004's convention: every workflow
/ composite-action `run:` line, plus -- one level deep -- the literal
subprocess argv of any `ci/*.py` script such a line names.

A `run:` block is split into logical lines (a trailing `\\` joins the next
line) before tokenizing, so two commands on separate lines never merge into
one argv. A same-line `# ci-lint: allow <RULE> <reason>` shell/Python
comment excuses that one line for that one rule (the GEN-012 escape hatch);
a repository-level exception goes in ci.toml's `[[exceptions]]` as usual.
"""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ci_lint.rules.tools import CI_SCRIPT_RE, ENV_ASSIGN_RE, SUBPROCESS_METHODS
from ci_lint.workflow_scan import (
    as_dict,
    as_list,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)
from ci_lint.yaml_io import LoadStatus


@dataclass(frozen=True)
class CommandSite:
    """One tokenized command and where it came from."""

    tokens: tuple[str, ...]
    path: str  # repo-relative file the command text lives in
    line: int | None  # 1-based, known for ci/*.py sites only
    loc: str  # human-readable location (jobs.<id>.steps[i] / line N)
    raw: str  # the logical source line, comment included


@dataclass(frozen=True)
class ShellCommand:
    """One command of a shell line: its leading env assignments and tokens."""

    env: MappingProxyType[str, str]  # read-only: the record is frozen
    tokens: list[str]


@dataclass(frozen=True)
class TestCommand:
    """`find_test_invocation`'s match: the runner kind and its arguments."""

    kind: str  # "nextest" or "cargo-test"
    args: tuple[str, ...]


@dataclass(frozen=True)
class StringConstant:
    value: str
    line: int


@dataclass(frozen=True)
class RunLine:
    line: str  # the logical run: line
    path: str
    loc: str


@dataclass(frozen=True)
class TestInvocation:
    """A `cargo nextest run` / `cargo test` found inside a command."""

    kind: str  # "nextest" or "cargo-test"
    args: tuple[str, ...]  # everything after the subcommand
    site: CommandSite


_CARGO_VAR_RE = re.compile(r"^\$\{?CARGO[A-Z_]*\}?$")


def allowed(raw: str, rule: str) -> bool:
    return re.search(rf"ci-lint:\s*allow\s+{re.escape(rule)}\s+\S", raw) is not None


def _strip_comment(line: str) -> str:
    # A `#` starts a comment only at the start or after whitespace (shell
    # semantics); `${{ ... }}#frag` style text is left alone.
    m = re.search(r"(^|\s)#", line)
    return line[: m.start()] if m else line


def logical_lines(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def normalize_expr(text: str) -> str:
    """Collapse each `${{ ... }}` Actions expression to one space-free token."""

    return _EXPR_RE.sub(lambda m: "${{" + m.group(1).replace(" ", "") + "}}", text)


def split_commands_with_env(line: str) -> list[ShellCommand]:
    """Tokenize one logical shell line into (leading env assignments,
    command tokens) pairs, splitting on unquoted `;`/`&&`/`||`/`|`/`&` only
    (a quoted nextest filter like `'test(a) | test(b)'` stays one token)."""

    lexer = shlex.shlex(normalize_expr(_strip_comment(line)), posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    try:
        for tok in lexer:
            if tok and set(tok) <= set(";&|"):
                commands.append([])
            else:
                commands[-1].append(tok)
    except ValueError:
        return []
    out: list[ShellCommand] = []
    for tokens in commands:
        env: dict[str, str] = {}
        i = 0
        while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
            key, value = tokens[i].split("=", 1)
            env[key] = value
            i += 1
        if i < len(tokens):
            out.append(ShellCommand(MappingProxyType(env), tokens[i:]))
    return out


def split_commands(line: str) -> list[list[str]]:
    return [cmd.tokens for cmd in split_commands_with_env(line)]


def is_cargo_token(token: str) -> bool:
    base = token.rsplit("/", 1)[-1]
    return base in ("cargo", "cargo.exe") or _CARGO_VAR_RE.match(token) is not None


def find_test_invocation(tokens: tuple[str, ...]) -> TestCommand | None:
    """`("nextest", args)` / `("cargo-test", args)` or `None`. Accepts any
    wrapper prefix (`soldr cargo ...`, `rustup run <tc> cargo ...`,
    `"$CARGO_BIN" test`) and a `+toolchain` override."""

    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] == "cargo-nextest":
            j = i + 1
            if j < len(tokens) and tokens[j] == "nextest":
                j += 1
            if j < len(tokens) and tokens[j] == "run":
                return TestCommand("nextest", tokens[j + 1 :])
            continue
        if not is_cargo_token(tok):
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].startswith("+"):
            j += 1
        if j + 1 < len(tokens) and tokens[j] == "nextest" and tokens[j + 1] == "run":
            return TestCommand("nextest", tokens[j + 2 :])
        if j < len(tokens) and tokens[j] == "test":
            return TestCommand("cargo-test", tokens[j + 1 :])
    return None


@dataclass(frozen=True)
class ScriptCall:
    tokens: tuple[str, ...]
    line: int
    cwd_literal: str | None  # the call's literal `cwd=` value, if any
    has_cwd: bool  # any `cwd=` keyword at all (literal or not)


def script_calls(path: Path) -> list[ScriptCall]:  # noqa: C901
    """Literal subprocess argv in a Python script (GEN-004's extractor, plus
    the call's `cwd=` keyword)."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    out: list[ScriptCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in SUBPROCESS_METHODS
        ):
            continue
        first = node.args[0]
        tokens: list[str] | None = None
        if isinstance(first, (ast.List, ast.Tuple)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in first.elts
        ):
            tokens = [e.value for e in first.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            try:
                tokens = shlex.split(first.value)
            except ValueError:
                tokens = None
        if not tokens:
            continue
        cwd_literal: str | None = None
        has_cwd = False
        for kw in node.keywords:
            if kw.arg == "cwd":
                has_cwd = True
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    cwd_literal = kw.value.value
        i = 0
        while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
        if i < len(tokens):
            out.append(ScriptCall(tuple(tokens[i:]), node.lineno, cwd_literal, has_cwd))
    return out


def script_string_constants(path: Path) -> list[StringConstant]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    return [
        StringConstant(n.value, n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def source_line(path: Path, line: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return ""
    return lines[line - 1] if 0 < line <= len(lines) else ""


def raw_lines_of(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []


def with_yaml_comment(line: str, raw_lines: list[str]) -> str:
    """YAML drops a plain scalar's trailing `# comment`; recover it from the
    raw file line holding this command so a same-line allow marker counts."""

    needle = line.strip()
    if not needle:
        return line
    for raw in raw_lines:
        if needle in raw and "#" in raw.split(needle, 1)[1]:
            return raw
    return line


def iter_run_lines(repo_root: Path) -> list[RunLine]:  # noqa: C901
    """(logical_line, path, loc) for every workflow / composite-action run: line."""

    out: list[RunLine] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        raw_lines = raw_lines_of(repo_root / wf.path)
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            for i, step in enumerate(steps_of(job)):
                run_text = step.get("run")
                if isinstance(run_text, str):
                    for ln in logical_lines(run_text):
                        out.append(RunLine(with_yaml_comment(ln, raw_lines), wf.path, f"jobs.{job_id}.steps[{i}]"))
    for act in load_composite_actions(repo_root):
        if act.status != LoadStatus.OK:
            continue
        runs = as_dict(act.document).get("runs")
        raw_lines = raw_lines_of(repo_root / act.path)
        if isinstance(runs, dict):
            for i, step in enumerate(as_list(runs.get("steps"))):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    for ln in logical_lines(step["run"]):
                        out.append(RunLine(with_yaml_comment(ln, raw_lines), act.path, f"runs.steps[{i}]"))
    return out


def referenced_scripts(repo_root: Path) -> list[str]:
    """Repo-relative `ci/*.py` scripts named by any run: line, sorted, unique."""

    seen: set[str] = set()
    for run in iter_run_lines(repo_root):
        for tokens in split_commands(run.line):
            for i, tok in enumerate(tokens):
                if CI_SCRIPT_RE.match(tok) and (repo_root / tok).is_file():
                    seen.add(tok)
                elif tok in ("-m", "--module") and i + 1 < len(tokens):
                    rel = module_script(tokens[i + 1])
                    if rel is not None and (repo_root / rel).is_file():
                        seen.add(rel)
    return sorted(seen)


_CI_MODULE_RE = re.compile(r"^ci(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def module_script(module: str) -> str | None:
    """`ci.foo.bar` (a `python -m`/`uv run --module` target) -> `ci/foo/bar.py`;
    anything outside the `ci` package -> None (running-process's
    `uv run --no-project --module ci.independent_spawn`)."""

    if not _CI_MODULE_RE.match(module):
        return None
    return module.replace(".", "/") + ".py"


def iter_command_sites(repo_root: Path) -> list[CommandSite]:
    """Every run: line command, plus one level into referenced ci/*.py scripts."""

    out: list[CommandSite] = []
    for run in iter_run_lines(repo_root):
        for tokens in split_commands(run.line):
            out.append(CommandSite(tuple(tokens), run.path, None, run.loc, run.line))
    for rel in referenced_scripts(repo_root):
        script = repo_root / rel
        for call in script_calls(script):
            out.append(
                CommandSite(call.tokens, rel, call.line, f"line {call.line}", source_line(script, call.line))
            )
    return out


def iter_test_invocations(repo_root: Path) -> list[TestInvocation]:
    out: list[TestInvocation] = []
    for site in iter_command_sites(repo_root):
        found = find_test_invocation(site.tokens)
        if found is not None:
            out.append(TestInvocation(found.kind, found.args, site))
    return out
