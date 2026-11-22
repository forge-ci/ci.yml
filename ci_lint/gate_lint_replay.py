"""GATE-016, replayed-workflow clause: a bosn -> act replay of a workflow job
that lints is lint under bosn, whatever the job is called.

`bosn ci run --workflow W --job fast` passes the name-based clauses when
`fast` runs `./ci.py fmt`, `./ci.py clippy`, ruff and pylint inside it
(forge-ci/ghd, zackees/template-python-rust-cmd). This module resolves every
workflow job the local gate replays through bosn and reads its steps:

- a gate/lane argv `bosn ci run [--workflow W] [--job J]` -- W defaults to
  `.github/workflows/ci.yml`, else the only workflow (bosn's own default);
  no `--job` replays every job of W;
- each `[[gate.replay.jobs]]` `source-job` and `[[gate.replay.selections]]`
  job (`[gate.full-run]` stamps lanes from the gate's own `run`, covered by
  the first bullet).

A job that calls a local reusable workflow (`uses: ./.github/workflows/x.yml`)
is followed into every job of `x.yml`. A job lints when its id is lint-like
or any `run:` step runs a lint command (`LINT_TOOLS`, `ci.py
fmt|clippy|lint|dylint`, or a lint-named script, plus `gate_lint_host`'s
cargo/soldr patterns). A step's `if:` is ignored: an `ACT`-guarded lint step
still runs (or was meant to run) under bosn -> act, so it does not exempt the
job.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.gate_lint_host import (
    BOSN_COMMANDS, FIX, RULE, TEST_WORDS, bosn_ci_run_args, cmd_name, lint_like, runs_lint_command,
)
from ci_lint.lane_cache import LaneConfig
from ci_lint.rules.test_invocations import split_commands
from ci_lint.workflow_scan import as_dict, jobs_of, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue, load_yaml_file

WORKFLOW_DIR = ".github/workflows"
DEFAULT_WORKFLOW = f"{WORKFLOW_DIR}/ci.yml"
LINT_TOOLS: frozenset[str] = frozenset(
    {"ruff", "pylint", "pyright", "eslint", "clang-tidy", "rustfmt", "cargo-clippy", "cargo-dylint", "cargo-fmt"}
)
CI_PY_LINT: frozenset[str] = frozenset({"fmt", "clippy", "lint", "dylint", "format"})
# A lint tool named as a package to install/fetch, not run: `pip install
# ruff`, `uv run --with ruff==x ...`, `npm install eslint`.
LINT_NAMED = "lint-named job"
_INSTALL_WORDS: frozenset[str] = frozenset({"install", "add", "--with", "--from", "-p", "--package"})


@dataclass(frozen=True)
class JobRef:
    """One replayed workflow job; `job` empty = every job of `workflow`."""

    workflow: str
    job: str
    by: str


@dataclass(frozen=True)
class LintStep:
    workflow: str
    job: str
    command: str


def _tool_runs(tokens: Sequence[str]) -> bool:
    for i, token in enumerate(tokens):
        name = cmd_name(token)
        if name in LINT_TOOLS and not any(t in _INSTALL_WORDS for t in tokens[:i]):
            return True
        if name == "ci.py" and i + 1 < len(tokens) and tokens[i + 1] in CI_PY_LINT:
            return True
        if token == "-m" and i + 1 < len(tokens) and lint_like(tokens[i + 1]):
            return True  # `python -m ci.lint`, `python -m pylint` (never `-m ci_lint`)
    return False


def _logical_lines(run: str) -> list[str]:
    return run.replace("\\\n", " ").splitlines()


def lint_command_in(run: str) -> str | None:
    """The first line of a `run:` script that runs lint, or None."""

    for line in _logical_lines(run):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if runs_lint_command(stripped) or any(_tool_runs(cmd) for cmd in split_commands(stripped)):
            return stripped
    return None


def _resolve_workflow(repo_root: Path, name: str | None) -> str | None:
    if name:
        for rel in (name, f"{WORKFLOW_DIR}/{name}"):
            if (repo_root / rel).is_file():
                return Path(rel).as_posix()
        return None
    if (repo_root / DEFAULT_WORKFLOW).is_file():
        return DEFAULT_WORKFLOW
    wf_dir = repo_root / WORKFLOW_DIR
    found = sorted(p for p in wf_dir.iterdir() if p.suffix in (".yml", ".yaml")) if wf_dir.is_dir() else []
    return found[0].relative_to(repo_root).as_posix() if len(found) == 1 else None


def _flag(argv: Sequence[str], flag: str) -> str | None:
    for i, token in enumerate(argv):
        name, eq, value = token.partition("=")
        if name == flag:
            return value if eq else (argv[i + 1] if i + 1 < len(argv) else None)
    return None


def argv_refs(argv: Sequence[str], by: str) -> list[JobRef]:
    args = bosn_ci_run_args(argv)
    if args is None:
        return []
    return [JobRef(_flag(args, "--workflow") or "", _flag(args, "--job") or "", by)]


def replay_refs(values: Sequence[str]) -> list[JobRef]:
    """`[gate.replay]` job refs (`ci.yml:fast`)."""

    refs: list[JobRef] = []
    for value in values:
        workflow, sep, job = value.partition(":")
        if sep and job:
            refs.append(JobRef(workflow, job, f"[gate.replay] {Path(workflow).name}:{job}"))
    return refs


def _lint_named_job(job_id: str) -> bool:
    words = {w for w in job_id.lower().replace("_", "-").split("-") if w}
    return lint_like(job_id) and not words & TEST_WORDS


@dataclass(frozen=True)
class _Doc:
    document: dict[str, YamlValue] | None


@dataclass(frozen=True)
class _Resolved:
    workflow: str
    jobs: tuple[str, ...]


@dataclass
class _Hit:
    workflow: str
    job: str
    step: LintStep
    by: list[str]


@dataclass
class _Scan:
    repo_root: Path
    findings: list[Finding]
    docs: dict[str, _Doc]

    def doc(self, rel: str) -> dict[str, YamlValue] | None:
        if rel not in self.docs:
            loaded = load_yaml_file(self.repo_root / rel)
            ok = loaded.status == LoadStatus.OK and isinstance(loaded.document, dict)
            self.docs[rel] = _Doc(as_dict(loaded.document) if ok else None)
            if not ok:
                self.findings.append(Finding(
                    rule=RULE, path=rel, status=Status.NEEDS_REVIEW, fix=FIX,
                    message=f"cannot parse the workflow bosn replays ({loaded.reason}); its jobs' lint steps are unchecked"))
        return self.docs[rel].document

    def lint_steps(self, rel: str, job_id: str, depth: int = 0) -> list[LintStep]:
        doc = self.doc(rel)
        job = jobs_of(doc).get(job_id) if doc is not None else None
        if job is None:
            return []
        out = [LintStep(rel, job_id, LINT_NAMED)] if _lint_named_job(job_id) else []
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./") and depth < 2:
            callee = uses[2:].split("@", 1)[0]
            callee_doc = self.doc(callee) if (self.repo_root / callee).is_file() else None
            return out + [s for sub in jobs_of(callee_doc or {}) for s in self.lint_steps(callee, sub, depth + 1)]
        for step in steps_of(job):
            run = step.get("run")
            hit = lint_command_in(run) if isinstance(run, str) else None
            if hit is not None:
                out.append(LintStep(rel, job_id, hit))
        return out

    def resolve(self, ref: JobRef) -> _Resolved | None:
        rel = _resolve_workflow(self.repo_root, ref.workflow or None)
        doc = self.doc(rel) if rel is not None else None
        if rel is None or doc is None:
            return None
        return _Resolved(rel, (ref.job,) if ref.job else tuple(jobs_of(doc)))


def _line_of(repo_root: Path, rel: str, job: str, command: str | None) -> int | None:
    """The command's line inside the job's block (the job header when None)."""

    try:
        lines = (repo_root / rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    header = next((i for i, line in enumerate(lines) if line.strip() in (f"{job}:", f'"{job}":', f"'{job}':")), None)
    if header is None or command is None:
        return None if header is None else header + 1
    needle = command.strip()[:60]
    return next((i for i, line in enumerate(lines[header:], start=header + 1) if needle in line), header + 1)


def _first_per_job(steps: Sequence[LintStep]) -> list[LintStep]:
    """One step per (workflow, job), preferring a real command over the name."""

    best: dict[str, LintStep] = {}
    for step in steps:
        key = f"{step.workflow}:{step.job}"
        if key not in best or best[key].command == LINT_NAMED:
            best[key] = step
    return list(best.values())


def _collect(scan: _Scan, refs: Sequence[JobRef]) -> dict[str, _Hit]:
    hits: dict[str, _Hit] = {}
    for ref in refs:
        resolved = scan.resolve(ref)
        for job_id in resolved.jobs if resolved is not None else ():
            for step in _first_per_job(scan.lint_steps(resolved.workflow, job_id)):
                hit = hits.setdefault(f"{step.workflow}:{step.job}", _Hit(step.workflow, step.job, step, []))
                if ref.by not in hit.by:
                    hit.by.append(ref.by)
    return hits


def check_replayed_jobs(repo_root: Path, refs: Sequence[JobRef]) -> list[Finding]:
    """One finding per linting workflow job, naming every bosn replayer."""

    scan = _Scan(repo_root, [], {})
    for hit in _collect(scan, refs).values():
        cmd = hit.step.command
        line = _line_of(repo_root, hit.workflow, hit.job, None if cmd == LINT_NAMED else cmd)
        scan.findings.append(Finding(
            rule=RULE, path=hit.workflow, line=line, fix=FIX,
            message=(f"job '{hit.job}' {'is lint-named' if cmd == LINT_NAMED else f'runs lint (`{cmd[:100]}`)'} "
                     f"and is replayed through bosn by {', '.join(hit.by)}")))
    return scan.findings



# ── replay targets hidden inside a bosn-tooled lane's script ─────────────────
# zccache's `ci/local_gate.py --lane check` (tools = ["bosn"]) launches
# `bosn ci run --workflow .github/workflows/ci.yml --job msrv` itself, so the
# lane argv names no workflow job. The targets are read statically from the
# script: argv sequences carrying `--workflow`/`--job` literals, and calls or
# dicts pairing a workflow-file literal with a job literal under `*workflow`/
# `*job` names (`bosn_workflow=`, `bosn_job=`), plus the same in the script's
# local imports (one level).

SCRIPT_SUFFIXES: tuple[str, ...] = (".py", ".sh", ".bash")
_SHELL_FLAG_RE = re.compile(r"--(workflow|job)(?:=|\s+)[\"']?([A-Za-z0-9_./-]+)")


@dataclass(frozen=True)
class Target:
    workflow: str
    job: str


@dataclass(frozen=True)
class _PyScan:
    targets: frozenset[Target]
    imports: frozenset[str]


@dataclass(frozen=True)
class ScriptReplays:
    refs: tuple[JobRef, ...]
    findings: tuple[Finding, ...]


class _Consts:
    """Module-level `NAME = "literal"` assignments (`WORKFLOW = "..."`)."""

    def __init__(self, names: Mapping[str, str]) -> None:
        self._names = dict(names)

    def text(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self._names.get(node.id)
        return None


def _module_consts(tree: ast.Module) -> _Consts:
    names: dict[str, str] = {}
    for stmt in tree.body:
        target = value = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            names[target.id] = value.value
    return _Consts(names)


def _is_workflow_file(text: str) -> bool:
    return text.endswith((".yml", ".yaml"))


def _seq_target(elts: Sequence[ast.AST], consts: _Consts) -> Target | None:
    """`(..., "--workflow", W, "--job", J, ...)`; a flag whose value is not a
    literal (or a module-level string constant) makes it unresolvable."""

    values = [consts.text(e) for e in elts]
    if "--workflow" not in values and "--job" not in values:
        return None
    found = {"--workflow": "", "--job": ""}
    for i, value in enumerate(values):
        if value in found:
            nxt = values[i + 1] if i + 1 < len(values) else None
            if nxt is None:
                return None
            found[value] = nxt
    return Target(found["--workflow"], found["--job"])


def _pair_target(keys: Sequence[str | None], values: Sequence[ast.AST], consts: _Consts) -> Target | None:
    workflow = job = ""
    for key, value in zip(keys, values):
        text = consts.text(value)
        if key is None or text is None:
            continue
        if key.lower().endswith("workflow") and _is_workflow_file(text):
            workflow = text
        elif key.lower().endswith("job"):
            job = text
    return Target(workflow, job) if workflow and job else None


def _node_target(node: ast.AST, consts: _Consts) -> Target | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        return _seq_target(node.elts, consts)
    if isinstance(node, ast.Call):
        return _pair_target([kw.arg for kw in node.keywords], [kw.value for kw in node.keywords], consts)
    if isinstance(node, ast.Dict):
        return _pair_target([consts.text(k) for k in node.keys], node.values, consts)
    return None


def _python_scan(text: str) -> _PyScan:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _PyScan(frozenset(), frozenset())
    consts = _module_consts(tree)
    targets: set[Target] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        hit = _node_target(node, consts)
        if hit is not None:
            targets.add(hit)
    return _PyScan(frozenset(targets), frozenset(imports))


def _shell_targets(text: str) -> frozenset[Target]:
    targets: set[Target] = set()
    for line in text.splitlines():
        flags = {name: value for name, value in _SHELL_FLAG_RE.findall(line)}
        if "bosn" in line and flags:
            targets.add(Target(flags.get("workflow", ""), flags.get("job", "")))
    return frozenset(targets)


def _module_file(repo_root: Path, module: str) -> Path | None:
    base = repo_root.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _lane_script(repo_root: Path, argv: Sequence[str]) -> Path | None:
    for i, token in enumerate(argv):
        if token == "-m" and i + 1 < len(argv):
            return _module_file(repo_root, argv[i + 1])
        if token.endswith(SCRIPT_SUFFIXES) and (repo_root / token).is_file():
            return repo_root / token
    return None


def script_targets(repo_root: Path, script: Path) -> frozenset[Target]:
    """Replay targets in `script` and, for Python, its local imports."""

    text = script.read_text(encoding="utf-8", errors="replace")
    if script.suffix != ".py":
        return _shell_targets(text)
    scan = _python_scan(text)
    targets = set(scan.targets)
    for module in scan.imports:
        path = _module_file(repo_root, module)
        if path is not None and path != script:
            targets.update(_python_scan(path.read_text(encoding="utf-8", errors="replace")).targets)
    return frozenset(targets)


def _bosn_script_lanes(repo_root: Path, lanes: Sequence[LaneConfig]) -> list[LaneConfig]:
    """bosn in `tools`, but bosn is not the lane's own argv: a wrapper script
    launches it. (A lane that runs `bosn ci run` is read from its argv; one
    that runs `bosn run --task` is a bosn.toml task, clause 3.)"""

    return [lane for lane in lanes
            if any(cmd_name(tool) in BOSN_COMMANDS for tool in lane.tools)
            and not any(cmd_name(token) in BOSN_COMMANDS for token in lane.run)]


def _unresolved(lane: LaneConfig) -> Finding:
    return Finding(
        rule=RULE, path="local gate", status=Status.NEEDS_REVIEW,
        message=(f"lane '{lane.id}' declares bosn in tools but no workflow job it replays could be read from "
                 f"`{' '.join(lane.run)[:100]}`, so its replayed steps are unchecked for lint"),
        fix=("declare the jobs the lane replays in [[gate.replay.jobs]] (source-job = \"ci.yml:<job>\", lanes = [...]) "
             "or [[gate.replay.selections]], or make the lane's run the `bosn ci run --workflow W --job J` argv"))


def script_replays(repo_root: Path, lanes: Sequence[LaneConfig], declared_lanes: Sequence[str] = (),
                   reported_lanes: Sequence[str] = ()) -> ScriptReplays:
    """Every bosn-tooled lane whose own `run` is not a `bosn ci run` argv: the
    replay targets its script launches. A script shared by several lanes
    cannot be split per lane statically, so its targets are attributed to the
    script and its bosn lanes together. A lane with no readable target and no
    `[gate.replay]` declaration covering it is needs_review."""

    by_script: dict[str, list[str]] = {}
    findings: list[Finding] = []
    for lane in _bosn_script_lanes(repo_root, lanes):
        script = _lane_script(repo_root, lane.run)
        if script is not None and script_targets(repo_root, script):
            by_script.setdefault(script.relative_to(repo_root).as_posix(), []).append(lane.id)
        elif lane.id not in declared_lanes and lane.id not in reported_lanes:
            findings.append(_unresolved(lane))
    refs: list[JobRef] = []
    for rel, lane_ids in by_script.items():
        label = f"{rel} (bosn lanes: {', '.join(lane_ids)})"
        refs.extend(JobRef(t.workflow, t.job, label)
                    for t in sorted(script_targets(repo_root, repo_root / rel), key=lambda t: (t.workflow, t.job)))
    return ScriptReplays(tuple(refs), tuple(findings))
