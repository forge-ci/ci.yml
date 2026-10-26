"""ACT-002, static clauses: local workflow execution goes through bosn's CI
engine, which pins act2 (zackees/ci.yml#213, docs/policy-general.md "Local
workflow execution uses act2").

Scanned local-CI surfaces (the ones `ci_lint.gate_bare_tools` reads, plus
every script under `ci/` and `scripts/`):

- the local gate's argv and each lane's argv (`local-gate.toml` /
  `ci.toml [local.gate]`), and every repository file those argv name;
- every shell/PowerShell/Python file under `ci/` and `scripts/`;
- every `[task.*].cmd` in `bosn.toml`.

Violations (all named by the policy text):

1. a bare `act`/`act2` command (shell word or Python argv) instead of
   `bosn ci run`;
2. a legacy action-cache flag: `--use-new-action-cache`,
   `--action-offline-mode`, `--action-cache-path` (the last is also how a
   second action-cache directory is configured);
3. a repository-owned act/act2 pin: an `ACT_VERSION`/`ACT2_VERSION`
   assignment, or a download URL for a nektos/act or zackees/act2 release.

`needs_review`: a command whose first word is a variable that names act
(`"$ACT_BIN" ...`, `$ACT_EXE`) -- it might be act, statically unknowable.

A same-line `# ci-lint: allow ACT-002 <reason>` excuses one line.
Workflows (`.github/workflows/*`) are out of scope: they run ON act, they do
not invoke it.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.test_invocations import allowed, split_commands

RULE = "ACT-002"
ACT_COMMANDS: frozenset[str] = frozenset({"act", "act2", "act.exe", "act2.exe"})
LEGACY_FLAGS: tuple[str, ...] = ("--use-new-action-cache", "--action-offline-mode", "--action-cache-path")
SCRIPT_DIRS: tuple[str, ...] = ("ci", "scripts")
SCRIPT_SUFFIXES: frozenset[str] = frozenset({".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ""})
FIX_BARE = "run the workflow through bosn's CI engine ('bosn ci run ...'), which pins act2 by version and sha256"
FIX_FLAG = "drop the flag: act2's content-addressed action cache is the only cache, and bosn passes no cache flags"
FIX_PIN = "delete the repository's act/act2 pin; bosn is the one place act2 is pinned (version + sha256)"
_LEGACY_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(f) for f in LEGACY_FLAGS) + r")(?![\w-])")
_PIN_RE = re.compile(r"\bACT2?_VERSION\b\s*[:=]")
_URL_RE = re.compile(r"github\.com/(nektos/act|zackees/act2)/releases/download", re.IGNORECASE)
_VAR_RE = re.compile(r"""^["']?\$\{?ACT2?_(BIN|EXE|PATH|CMD)\}?["']?$""")


@dataclass(frozen=True)
class _Site:
    path: str
    line: int | None
    where: str


def _cmd_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _bare_act(tokens: Sequence[str]) -> str | None:
    if not tokens:
        return None
    if len(tokens) > 1 and tokens[1].startswith(("=", "+=", ":=")):
        return None
    name = _cmd_name(tokens[0])
    return tokens[0] if name in ACT_COMMANDS else None


def _text_findings(text: str, site: _Site, *, words: bool) -> list[Finding]:
    """`words=False` for Python source: only flags/pins are textual there;
    argv is read from the AST."""

    out: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "REM ", "::")) or allowed(line, RULE):
            continue
        lineno = site.line if site.line is not None else idx
        for flag in dict.fromkeys(_LEGACY_RE.findall(stripped)):
            out.append(Finding(rule=RULE, path=site.path, line=lineno,
                               message=f"{site.where}: legacy action-cache flag '{flag}'", fix=FIX_FLAG))
        if _PIN_RE.search(stripped) or _URL_RE.search(stripped):
            out.append(Finding(rule=RULE, path=site.path, line=lineno,
                               message=f"{site.where}: repository-owned act/act2 pin (`{stripped[:100]}`)", fix=FIX_PIN))
        if not words:
            continue
        for tokens in split_commands(stripped):
            cmd = _bare_act(tokens)
            if cmd is not None:
                out.append(Finding(rule=RULE, path=site.path, line=lineno,
                                   message=f"{site.where}: bare '{cmd}' (`{stripped[:100]}`)", fix=FIX_BARE))
            elif tokens and _VAR_RE.match(tokens[0]):
                out.append(Finding(rule=RULE, path=site.path, line=lineno, status=Status.NEEDS_REVIEW,
                                   message=f"{site.where}: command '{tokens[0]}' may be a direct act invocation",
                                   fix=FIX_BARE))
    return out


def _python_findings(text: str, path: str) -> list[Finding]:
    out = _text_findings(text, _Site(path, None, "script"), words=False)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    lines = text.splitlines()
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.List, ast.Tuple)) and node.elts):
            continue
        first = node.elts[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        cmd = _bare_act([first.value])
        if cmd is None:
            continue
        src = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if allowed(src, RULE):
            continue
        out.append(Finding(rule=RULE, path=path, line=node.lineno,
                           message=f"script: argv starts with bare '{cmd}'", fix=FIX_BARE))
    return out


def _file_findings(path: Path, rel: str) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix == ".py":
        return _python_findings(text, rel)
    return _text_findings(text, _Site(rel, None, "script"), words=True)


def _argv_findings(argv: Sequence[str], where: str) -> list[Finding]:
    out: list[Finding] = []
    cmd = _bare_act(argv)
    if cmd is not None:
        out.append(Finding(rule=RULE, path="local gate", message=f"{where}: argv is bare '{cmd}'", fix=FIX_BARE))
    for token in argv:
        for flag in _LEGACY_RE.findall(token):
            out.append(Finding(rule=RULE, path="local gate",
                               message=f"{where}: legacy action-cache flag '{flag}'", fix=FIX_FLAG))
    return out


def _script_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for name in SCRIPT_DIRS:
        base = repo_root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in SCRIPT_SUFFIXES and "/." not in "/" + path.relative_to(repo_root).as_posix():
                files.append(path)
    return files


def _bosn_findings(repo_root: Path) -> list[Finding]:
    manifest = repo_root / "bosn.toml"
    if not manifest.is_file():
        return []
    try:
        doc = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return []
    out: list[Finding] = []
    tasks = doc.get("task")
    for name, spec in (tasks.items() if isinstance(tasks, dict) else []):
        cmd = spec.get("cmd") if isinstance(spec, dict) else None
        if isinstance(cmd, str):
            out.extend(_text_findings(cmd, _Site("bosn.toml", None, f"task '{name}'"), words=True))
    return out


def check_act_002(repo_root: Path, gate_runs: Sequence[Sequence[str]] = ()) -> list[Finding]:
    """`gate_runs`: the local gate's `run` argv followed by each lane's."""

    findings: list[Finding] = []
    seen: set[Path] = set()
    for i, argv in enumerate(gate_runs):
        findings.extend(_argv_findings(argv, "the gate" if i == 0 else f"lane {i}"))
        for token in argv:
            script = repo_root / token
            if script.is_file() and script.resolve() not in seen:
                seen.add(script.resolve())
                findings.extend(_file_findings(script, script.relative_to(repo_root).as_posix()))
    for path in _script_files(repo_root):
        if path.resolve() in seen:
            continue
        seen.add(path.resolve())
        findings.extend(_file_findings(path, path.relative_to(repo_root).as_posix()))
    findings.extend(_bosn_findings(repo_root))
    return findings


# --- ACT-003 (zackees/ci.yml#226): act-only workarounds in workflows ---------

RULE_003 = "ACT-003"
# Registered exceptions: (action prefix, input) pairs a workflow may key on
# `ACT`, each with its tracking issue. Only a stock action that exposes the
# knob but has no runner-aware default qualifies (docs/policy-general.md).
@dataclass(frozen=True)
class ActException:
    action_prefix: str
    input_name: str
    issue: str


ACT_003_EXCEPTIONS: tuple[ActException, ...] = (
    ActException("astral-sh/setup-uv@", "prune-cache", "zackees/ci.yml#226"),
    ActException("astral-sh/setup-uv@", "cache-suffix", "zackees/ci.yml#226"),
)
FIX_003 = ("drop the act-only workaround -- fix it in act2, bosn or the action -- or register an ACT-003 "
           "exception (docs/policy-general.md 'Stock actions work out of the box under act2')")
_ACT_REF_RE = re.compile(r"""\benv\.ACT\b|\benv\[\s*['"]ACT['"]\s*\]|\$\{?ACT\b(?!_)""")
_STEP_RE = re.compile(r"^\s*-\s")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)")
_KEY_RE = re.compile(r"^\s*(?:-\s+)?([\w-]+):")


def _workflow_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    wf = repo_root / ".github" / "workflows"
    if wf.is_dir():
        files.extend(p for p in sorted(wf.iterdir()) if p.suffix in (".yml", ".yaml"))
    for name in ("action.yml", "action.yaml"):
        files.extend(sorted(repo_root.glob(f".github/actions/*/{name}")))
        if (repo_root / name).is_file():
            files.append(repo_root / name)
    return files


def _registered(uses: str | None, key: str | None) -> str | None:
    for exc in ACT_003_EXCEPTIONS:
        if uses is not None and uses.startswith(exc.action_prefix) and key == exc.input_name:
            return exc.issue
    return None


def _act_003_file(path: Path, rel: str) -> list[Finding]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[Finding] = []
    uses: str | None = None
    for idx, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        if _STEP_RE.match(line):
            uses = None
        m = _USES_RE.match(line)
        if m:
            uses = m.group(1)
        if not _ACT_REF_RE.search(line) or allowed(line, RULE_003):
            continue
        km = _KEY_RE.match(line)
        if _registered(uses, km.group(1) if km else None) is not None:
            continue
        out.append(Finding(rule=RULE_003, path=rel, line=idx,
                           message=f"act-only workaround keyed on ACT (`{line.strip()[:100]}`)", fix=FIX_003))
    return out


def check_act_003(repo_root: Path) -> list[Finding]:
    """An `ACT`-keyed expression in a workflow or composite action is a
    violation unless it is a registered exception (`ACT_003_EXCEPTIONS`) or
    the line carries `# ci-lint: allow ACT-003 <reason>`."""

    findings: list[Finding] = []
    for path in _workflow_files(repo_root):
        findings.extend(_act_003_file(path, path.relative_to(repo_root).as_posix()))
    return findings
