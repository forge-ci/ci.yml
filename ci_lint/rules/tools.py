"""Group 4: tool rules.

TOOL-001 (no bare cargo/rustc/rustup/maturin/... -- use soldr/uv), TOOL-002
(a dep-resolving cargo subcommand needs --locked), TOOL-003 (in a Rust
repo -- a `pyproject.toml` with `[build-system]` or a `Cargo.toml` with
`[workspace]` -- plain `uv run`/`uv sync` is banned in `run:` lines,
composite-action steps, `ci/**/*.py` subprocess argv, and `uv run`
shebangs, because a plain `uv run`/`uv sync` first syncs the project,
which means a hidden PEP 517 rebuild of the Rust package inside that
step; ci.yml#16/#50), CACHE-009 (zackees/setup-soldr may only be called
from its one wrapper action, with its required inputs, and never enabling
a retired cache family), RUST-002 (Dylint bypasses soldr, is invoked from
more than one job or a non-Linux job, builds cargo-dylint/dylint-link
from source per run, or passes --workspace without --all -- round-2B's
cargo-dylint no-op footgun), and GEN-004 (when the repo has Python
sources, the `fast` job must run Ruff check + format --check + Pylint,
directly or via a `ci/*.py` script it calls one level deep; standalone
Black/isort is GEN-004 too).
"""

from __future__ import annotations

from ci_lint import repo_identity

import ast
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.repo_files import list_repo_files
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import (
    as_dict,
    as_list,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)
from ci_lint.yaml_io import LoadStatus, YamlValue


@dataclass(frozen=True)
class RunStep:
    """One `run:` step in a workflow or composite action."""

    run_text: str
    path: str
    loc: str


@dataclass(frozen=True)
class AstCommand:
    """A literal subprocess/os command found in a Python script."""

    tokens: list[str]
    lineno: int


@dataclass(frozen=True)
class UsesStep:
    """One `uses:` step and its `with:` inputs."""

    uses: str
    loc: str
    with_: dict[str, YamlValue]


@dataclass(frozen=True)
class YamlFileDoc:
    """A loaded workflow or composite-action document."""

    path: str
    is_composite: bool
    doc: dict[str, YamlValue]


@dataclass(frozen=True)
class WorkflowJob:
    """One job of a loaded workflow."""

    path: str
    job_id: str
    job: dict[str, YamlValue]


@dataclass(frozen=True, order=True)
class LintToolHit:
    """A standalone Black/isort invocation found by GEN-004."""

    kind: str
    loc: str

BARE_BANNED: frozenset[str] = frozenset(
    {
        "cargo",
        "rustc",
        "rustup",
        "maturin",
        "cross",
        "cibuildwheel",
        "pip",
        "pipx",
        "twine",
        "curl",
        "wget",
    }
)
LOCKED_SUBCOMMANDS: frozenset[str] = frozenset(
    {"build", "test", "check", "clippy", "doc", "run", "nextest"}
)
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A root-level `*.py` or anything under `ci/` -- shared by GEN-004's "follow
# one level into a ci/*.py script" and RUST-002's identical convention.
CI_SCRIPT_RE = re.compile(r"^ci/.*\.py$")


def _is_bare_banned(cmd: str) -> bool:
    return cmd in BARE_BANNED or cmd.startswith("cargo-")


def find_commands(text: str) -> list[list[str]]:
    """Split a shell string on control separators, strip leading env
    assignments, and return each segment's remaining tokens."""

    segments = re.split(r"&&|\|\||[;|]", text)
    out: list[list[str]] = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            continue
        i = 0
        while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
        if i < len(tokens):
            out.append(tokens[i:])
    return out


def _tool_findings_for_commands(commands: list[list[str]], path: str, loc: str) -> list[Finding]:
    findings: list[Finding] = []
    for tokens in commands:
        if not tokens:
            continue
        cmd = tokens[0]
        cargo_tokens: list[str] | None = None
        if cmd == "soldr":
            # 'soldr cargo <subcommand> ...' still needs to be checked for
            # --locked; soldr itself is never a TOOL-001 violation.
            if len(tokens) > 1 and tokens[1] == "cargo":
                cargo_tokens = tokens[1:]
        else:
            if cmd == "cargo":
                cargo_tokens = tokens
            if _is_bare_banned(cmd):
                findings.append(
                    Finding(
                        rule="TOOL-001",
                        path=path,
                        message=f"{loc}: bare '{cmd}' invoked directly",
                        fix=f"wrap the Rust/wheel tool through soldr ('soldr cargo {cmd} ...' or "
                        "'soldr wheel ...') or through uv for Python packaging; never call it bare",
                    )
                )
                if cmd in ("cargo", "rustc", "rustup", "maturin"):
                    # M2-21 (ci.yml#44): a bare Rust toolchain invocation is
                    # also a Soldr bypass -- record it under RUST-001 too, so
                    # a Rust-scoped audit finds it without cross-referencing
                    # TOOL-001's generic banned-tool list.
                    findings.append(
                        Finding(
                            rule="RUST-001",
                            path=path,
                            message=f"{loc}: bare '{cmd}' bypasses Soldr's Rust toolchain wrapper",
                            fix=f"wrap this through soldr ('soldr cargo {cmd} ...') so the pinned "
                            "toolchain, cache, and Dylint wiring apply",
                        )
                    )
        if (
            cargo_tokens is not None
            and len(cargo_tokens) > 1
            and cargo_tokens[1] in LOCKED_SUBCOMMANDS
            and "--locked" not in cargo_tokens
        ):
            findings.append(
                Finding(
                    rule="TOOL-002",
                    path=path,
                    message=f"{loc}: 'cargo {cargo_tokens[1]}' resolves dependencies without --locked",
                    fix=f"add '--locked' to the 'cargo {cargo_tokens[1]}' invocation at {loc}",
                )
            )
    return findings


def _iter_run_steps(repo_root: Path) -> list[RunStep]:  # noqa: C901
    """(run_text, path, loc) for every run: step in workflows and composite actions."""

    out: list[RunStep] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        for job_id, job in jobs_of(doc).items():
            for i, step in enumerate(steps_of(job)):
                run_text = step.get("run")
                if isinstance(run_text, str):
                    out.append(RunStep(run_text, wf.path, f"jobs.{job_id}.steps[{i}]"))
    for act in load_composite_actions(repo_root):
        if act.status != LoadStatus.OK:
            continue
        runs = as_dict(act.document).get("runs")
        if isinstance(runs, dict):
            for i, step in enumerate(as_list(runs.get("steps"))):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    out.append(RunStep(step["run"], act.path, f"runs.steps[{i}]"))
    return out


TOOL_002 = "TOOL-002"


def check_tool_rules_workflows(repo_root: Path) -> list[Finding]:
    # Deferred: test_invocations imports this module (circular at import time).
    from ci_lint.rules.test_invocations import (  # pylint: disable=import-outside-toplevel
        allowed,
        raw_lines_of,
        with_yaml_comment,
    )

    findings: list[Finding] = []
    raw_cache: dict[str, list[str]] = {}
    for run_step in _iter_run_steps(repo_root):
        run_text = run_step.run_text
        path = run_step.path
        loc = run_step.loc
        commands = find_commands(run_text)
        step_findings = _tool_findings_for_commands(commands, path, loc)
        if any(f.rule == TOOL_002 for f in step_findings):
            # ci.yml#138: a same-line '# ci-lint: allow TOOL-002 <reason>'
            # excuses that one line's missing --locked (the RUST-015/016/017
            # convention); every other line of the step is still checked.
            raw = raw_cache.setdefault(path, raw_lines_of(repo_root / path))
            kept = "\n".join(
                ln for ln in run_text.splitlines() if not allowed(with_yaml_comment(ln, raw), TOOL_002)
            )
            step_findings = [f for f in step_findings if f.rule != TOOL_002] + [
                f for f in _tool_findings_for_commands(find_commands(kept), path, loc) if f.rule == TOOL_002
            ]
        findings.extend(step_findings)
    return findings


SUBPROCESS_METHODS: frozenset[str] = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _root_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _literal_str_list(node: ast.expr) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            else:
                return None
        return out
    return None


def _literal_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _discover_python_command_files(repo_root: Path) -> list[Path]:
    """Root-level `*.py` and everything under `ci/`, restricted to
    git-tracked (or not-yet-ignored) files (`ci_lint.repo_files`) so this
    AST scan never walks into gitignored build output that happens to sit
    under a `ci/` directory name."""

    out: list[Path] = []
    for rel in list_repo_files(repo_root):
        if not rel.endswith(".py"):
            continue
        if "/" not in rel or rel.startswith("ci/"):
            out.append(repo_root / rel)
    return sorted(out)


def _extract_ast_commands(path: Path) -> list[AstCommand]:  # noqa: C901
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    out: list[AstCommand] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        matched = False
        if isinstance(func, ast.Attribute):
            root = _root_name(func.value)
            if root == "subprocess" and func.attr in SUBPROCESS_METHODS:
                matched = True
            elif root == "os" and func.attr in ("system", "spawnl", "spawnv", "spawnve"):
                matched = True
        if not matched or not node.args:
            continue
        first = node.args[0]
        tokens = _literal_str_list(first)
        if tokens is None:
            as_str = _literal_str(first)
            if as_str is not None:
                try:
                    tokens = shlex.split(as_str)
                except ValueError:
                    tokens = None
        if tokens:
            out.append(AstCommand(tokens, node.lineno))
    return out


def check_tool_rules_python(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _discover_python_command_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        for cmd in _extract_ast_commands(path):
            tokens = cmd.tokens
            lineno = cmd.lineno
            i = 0
            while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
                i += 1
            remaining = tokens[i:]
            if not remaining:
                continue
            for f in _tool_findings_for_commands([remaining], rel, f"line {lineno}"):
                findings.append(
                    Finding(rule=f.rule, path=f.path, line=lineno, message=f.message, fix=f.fix)
                )
    return findings


def _truthy(value: YamlValue) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


def _iter_uses_with(
    doc: dict[str, YamlValue], *, is_composite: bool
) -> list[UsesStep]:
    out: list[UsesStep] = []
    if is_composite:
        runs = doc.get("runs")
        if isinstance(runs, dict):
            for i, step in enumerate(as_list(runs.get("steps"))):
                if isinstance(step, dict) and isinstance(step.get("uses"), str):
                    with_ = step.get("with")
                    out.append(UsesStep(step["uses"], f"runs.steps[{i}]", with_ if isinstance(with_, dict) else {}))
    else:
        for job_id, job in jobs_of(doc).items():
            for i, step in enumerate(steps_of(job)):
                uses = step.get("uses")
                if isinstance(uses, str):
                    with_ = step.get("with")
                    out.append(
                        UsesStep(uses, f"jobs.{job_id}.steps[{i}]", with_ if isinstance(with_, dict) else {})
                    )
    return out


def _is_plan_expr(value: object) -> bool:
    """Round-4B: an `[allow].setup-soldr.require`/`[allow].setup-uv.require`
    value of the special string `"plan"` means the actual input must be an
    expression DERIVED from the precheck plan -- `needs.precheck.outputs.*`
    in a workflow job, or an `inputs.*` passthrough inside a composite
    action wrapper (whose own caller supplies that input from the plan) --
    never a literal."""

    return isinstance(value, str) and ("needs.precheck.outputs." in value or "inputs." in value)


def check_cache_009(ci: CiToml, repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    only_in = ci.allow.setup_soldr.only_in.strip("/")

    files: list[YamlFileDoc] = []
    for wf in load_workflows(repo_root):
        if wf.status == LoadStatus.OK:
            files.append(YamlFileDoc(wf.path, False, as_dict(wf.document)))
    for act in load_composite_actions(repo_root):
        if act.status == LoadStatus.OK:
            files.append(YamlFileDoc(act.path, True, as_dict(act.document)))

    for yf in files:
        path, is_composite, doc = yf.path, yf.is_composite, yf.doc
        for use_step in _iter_uses_with(doc, is_composite=is_composite):
            uses = use_step.uses
            loc = use_step.loc
            with_ = use_step.with_
            if repo_identity.setup_soldr_prefix(uses) is None:
                continue
            file_dir = Path(path).parent.as_posix()
            if file_dir != only_in:
                findings.append(
                    Finding(
                        rule="CACHE-009",
                        path=path,
                        message=f"{loc}: zackees/setup-soldr is called outside the allowed wrapper "
                        f"location '{only_in}' (found in '{file_dir}')",
                        fix=f"move this call into the wrapper composite action at {only_in}/action.yml "
                        f"and call that wrapper from {path} instead",
                    )
                )
                findings.append(
                    Finding(
                        rule="RUST-001",
                        path=path,
                        message=f"{loc}: zackees/setup-soldr called outside the sanctioned wrapper "
                        f"'{only_in}' -- this is a Soldr bypass",
                        fix=f"move this call into the wrapper composite action at {only_in}/action.yml "
                        f"and call that wrapper from {path} instead",
                    )
                )
                continue

            for entry in ci.allow.setup_soldr.require.entries:
                key, expected = entry.key, entry.value
                actual = with_.get(key)
                if actual is None:
                    findings.append(
                        Finding(
                            rule="CACHE-009",
                            path=path,
                            message=f"{loc}: setup-soldr wrapper is missing required input '{key}'",
                            fix=(
                                f"add '{key}: \"{expected}\"' to {loc}'s with: block"
                                if expected != "plan"
                                else f"add '{key}: ${{{{ inputs.{key} }}}}' to {loc}'s with: block, "
                                f"passed through from an '{key}' composite input the caller sets to "
                                "'${{ needs.precheck.outputs.cache_save }}'"
                            ),
                        )
                    )
                elif expected == "plan":
                    if not _is_plan_expr(actual):
                        findings.append(
                            Finding(
                                rule="CACHE-009",
                                path=path,
                                message=f"{loc}: setup-soldr input '{key}' = {actual!r} must be an "
                                "expression derived from the precheck plan (contains "
                                "'needs.precheck.outputs.' or passes through an 'inputs.*' value), "
                                "not a literal",
                                fix=f"set '{key}' to '${{{{ needs.precheck.outputs.cache_save }}}}' "
                                f"(in the calling workflow) or '${{{{ inputs.{key} }}}}' (inside the "
                                f"wrapper composite, passed through from its caller) at {loc}",
                            )
                        )
                elif str(actual).lower() != expected.lower():
                    findings.append(
                        Finding(
                            rule="CACHE-009",
                            path=path,
                            message=f"{loc}: setup-soldr input '{key}' = {actual!r}, expected "
                            f"'{expected}'",
                            fix=f'set \'{key}: "{expected}"\' in {loc}\'s with: block',
                        )
                    )
            for key, val in with_.items():
                key_norm = key.lower().replace("_", "-")
                for retired in ci.cache.retired:
                    if retired.lower() in key_norm and _truthy(val):
                        findings.append(
                            Finding(
                                rule="CACHE-009",
                                path=path,
                                message=f"{loc}: input '{key}' = {val!r} enables retired cache "
                                f"family '{retired}'",
                                fix=f"remove or disable '{key}' in {loc}'s with: block; the "
                                f"'{retired}' cache family is retired (ci.toml [cache].retired)",
                            )
                        )
    return findings


# ── RUST-002: Dylint bypasses Soldr / duplicates across jobs / footguns ────
#
# zackees/ci.yml#1's acceptance criteria (round-6E brief): Dylint invoked in
# more than one job; Dylint in any job whose runs-on is not Linux; bare
# `cargo dylint`/`cargo-dylint`/`dylint-link` (not `soldr dylint`/`soldr
# cargo dylint`) in a run: line or a ci/*.py script's argv, followed one
# level deep exactly like GEN-004; `cargo install cargo-dylint`/`dylint-link`
# anywhere (a per-run tool build, never soldr's own catalogued prebuilt);
# and `--workspace` without `--all` -- round-2B's evidence that cargo-dylint
# prints "Nothing to do. Did you forget --all?" and silently lints nothing.

DYLINT_BARE_TOOL_NAMES: frozenset[str] = frozenset({"cargo-dylint", "dylint-link"})


def _is_bare_dylint_cargo_subcommand(tokens: list[str]) -> bool:
    return len(tokens) > 1 and tokens[0] == "cargo" and tokens[1] == "dylint"


def _is_bare_dylint_tool(tokens: list[str]) -> bool:
    return bool(tokens) and (tokens[0] in DYLINT_BARE_TOOL_NAMES or _is_bare_dylint_cargo_subcommand(tokens))


def _is_soldr_dylint_invocation(tokens: list[str]) -> bool:
    """The two accepted indirections: `soldr dylint ...` (soldr's own
    subcommand) and `soldr cargo dylint ...` (soldr's cargo passthrough)."""

    if not tokens or tokens[0] != "soldr":
        return False
    if len(tokens) > 1 and tokens[1] == "dylint":
        return True
    return len(tokens) > 2 and tokens[1] == "cargo" and tokens[2] == "dylint"


def _is_dylint_related(tokens: list[str]) -> bool:
    return _is_bare_dylint_tool(tokens) or _is_soldr_dylint_invocation(tokens)


def _is_cargo_install_dylint_tool(tokens: list[str]) -> bool:
    if len(tokens) < 3 or tokens[0] != "cargo" or tokens[1] != "install":
        return False
    return any(arg.split("@", 1)[0] in DYLINT_BARE_TOOL_NAMES for arg in tokens[2:])


def _rust_002_findings_for_command(tokens: list[str], path: str | None, loc: str) -> list[Finding]:
    findings: list[Finding] = []
    rendered = " ".join(tokens)
    if _is_bare_dylint_tool(tokens):
        findings.append(
            Finding(
                rule="RUST-002",
                path=path,
                message=f"{loc}: '{rendered}' invokes cargo-dylint directly, bypassing soldr",
                fix="use 'soldr dylint --all -- <cargo-dylint args>' (or 'soldr cargo dylint ...') so "
                "the prebuilt driver/rust-std and Dylint's own cache are used instead of a per-run "
                "cargo-dylint resolved from PATH",
            )
        )
    if _is_cargo_install_dylint_tool(tokens):
        findings.append(
            Finding(
                rule="RUST-002",
                path=path,
                message=f"{loc}: '{rendered}' builds a Dylint tool from source on every run",
                fix="drop the 'cargo install'; soldr resolves cargo-dylint/dylint-link from its own "
                "catalogued prebuilts ('soldr dylint prepare --target T' / setup-soldr's "
                "dylint-toolchain input), never compiled per run",
            )
        )
    if _is_dylint_related(tokens) and "--workspace" in tokens and "--all" not in tokens:
        findings.append(
            Finding(
                rule="RUST-002",
                path=path,
                message=f"{loc}: '{rendered}' passes --workspace without --all -- cargo-dylint prints "
                '"Nothing to do. Did you forget --all?" and lints nothing',
                fix="add '--all' (soldr dylint's own flag, before the '--' separator: 'soldr dylint "
                "--all -- --workspace --all-targets ...') -- round-2B's no-op footgun",
            )
        )
    return findings


def _follow_ci_scripts(commands: list[list[str]], repo_root: Path) -> list[list[str]]:
    """Every literal command, PLUS (one level only -- GEN-004's own "follow
    one level into ci/*.py" convention, reused here) the subprocess argv
    literals of any `ci/*.py` script named in one of those commands'
    tokens."""

    out = list(commands)
    for tokens in commands:
        for candidate in tokens:
            if not CI_SCRIPT_RE.match(candidate):
                continue
            script_path = repo_root / candidate
            if not script_path.is_file():
                continue
            for cmd in _extract_ast_commands(script_path):
                sub_tokens = cmd.tokens
                _lineno = cmd.lineno
                i = 0
                while i < len(sub_tokens) and ENV_ASSIGN_RE.match(sub_tokens[i]):
                    i += 1
                remaining = sub_tokens[i:]
                if remaining:
                    out.append(remaining)
    return out


def _iter_workflow_jobs(repo_root: Path) -> list[WorkflowJob]:
    out: list[WorkflowJob] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            out.append(WorkflowJob(wf.path, job_id, job))
    return out


def _job_has_dylint_setup(job: dict[str, YamlValue]) -> bool:
    for step in steps_of(job):
        uses = step.get("uses")
        if isinstance(uses, str) and "setup-soldr" in uses:
            with_ = step.get("with")
            if isinstance(with_, dict) and _truthy(with_.get("dylint")):
                return True
    return False


def _job_effective_commands(job: dict[str, YamlValue], repo_root: Path) -> list[list[str]]:
    run_texts = [s["run"] for s in steps_of(job) if isinstance(s.get("run"), str)]
    commands: list[list[str]] = []
    for text in run_texts:
        commands.extend(find_commands(text))
    return _follow_ci_scripts(commands, repo_root)


def _runs_on_is_linux(runs_on: YamlValue) -> bool | None:
    """`True`/`False` for a literal runner label (or list of them);
    `None` -- not statically confirmable -- for a matrix/expression value
    (e.g. `${{ matrix.os }}`) or a self-hosted-labels array ci-lint cannot
    resolve without live context."""

    if isinstance(runs_on, str):
        return runs_on.strip().lower().startswith("ubuntu")
    if isinstance(runs_on, list) and runs_on and all(isinstance(x, str) for x in runs_on):
        return all(x.strip().lower().startswith("ubuntu") for x in runs_on)
    return None


def check_rust_002(repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    dylint_jobs: list[WorkflowJob] = []

    for wj in _iter_workflow_jobs(repo_root):

        path = wj.path

        job_id = wj.job_id

        job = wj.job
        commands = _job_effective_commands(job, repo_root)
        for tokens in commands:
            findings.extend(_rust_002_findings_for_command(tokens, path, f"jobs.{job_id}"))
        if _job_has_dylint_setup(job) or any(_is_dylint_related(c) for c in commands):
            dylint_jobs.append(WorkflowJob(path, job_id, job))

    # Also scan every root/ci/*.py file directly (not only scripts reached
    # via a job's run: line) -- matches check_tool_rules_python's blanket
    # scope, so a Dylint invocation buried in a helper script no workflow
    # calls (yet) is still caught before it is wired up wrong.
    for py_path in _discover_python_command_files(repo_root):
        rel = py_path.relative_to(repo_root).as_posix()
        for cmd in _extract_ast_commands(py_path):
            tokens = cmd.tokens
            lineno = cmd.lineno
            i = 0
            while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
                i += 1
            remaining = tokens[i:]
            if not remaining:
                continue
            for f in _rust_002_findings_for_command(remaining, rel, f"line {lineno}"):
                findings.append(Finding(rule=f.rule, path=f.path, line=lineno, message=f.message, fix=f.fix))

    if len(dylint_jobs) > 1:
        names = ", ".join(f"{dj.path}:{dj.job_id}" for dj in dylint_jobs)
        for dj in dylint_jobs:
            path, job_id = dj.path, dj.job_id
            findings.append(
                Finding(
                    rule="RUST-002",
                    path=path,
                    message=f"jobs.{job_id} invokes Dylint, but so does at least one other job ({names}) "
                    "-- Dylint must run from exactly ONE Linux job that checks every declared platform",
                    fix="consolidate every declared platform's Dylint check into a single Linux 'dylint' "
                    "job (one 'soldr dylint prepare --target T' per cross target, then one multi-target "
                    f"'soldr dylint --all -- --workspace --all-targets [--target T ...]' pass); remove "
                    f"the Dylint step(s) from the other job(s) ({names})",
                )
            )

    for dj in dylint_jobs:
        path, job_id, job = dj.path, dj.job_id, dj.job
        runs_on = job.get("runs-on")
        verdict = _runs_on_is_linux(runs_on)
        if verdict is False:
            findings.append(
                Finding(
                    rule="RUST-002",
                    path=path,
                    message=f"jobs.{job_id} invokes Dylint but runs-on = {runs_on!r}, not a Linux runner",
                    fix="move this job's Dylint check to a 'ubuntu-*' runner -- one Linux host checks "
                    "every declared platform via 'soldr dylint prepare --target T' plus a multi-target "
                    "invocation, never a native macOS/Windows job",
                )
            )
        elif verdict is None:
            findings.append(
                Finding(
                    rule="RUST-002",
                    path=path,
                    status=Status.NEEDS_REVIEW,
                    message=f"jobs.{job_id} invokes Dylint but runs-on = {runs_on!r} is not a literal "
                    "runner label; cannot confirm it resolves to Linux",
                    fix="pin runs-on to a literal 'ubuntu-*' label for the Dylint job, or resolve the "
                    "matrix/expression so ci-lint can confirm it targets Linux",
                )
            )

    return findings


# ── GEN-004: Python quick checks (Ruff + Pylint, no standalone Black/isort) ──

_LINT_KIND_LABEL: dict[str, str] = {
    "ruff-check": "'ruff check'",
    "ruff-format-check": "'ruff format --check'",
    "pylint": "'pylint'",
}


def _classify_lint_tokens(tokens: list[str]) -> str | None:
    """`None`, or one of `"ruff-check"`/`"ruff-format-check"`/`"pylint"`/
    `"black"`/`"isort"` -- recognizes a bare invocation, a `python3 -m
    <tool>` invocation, or a `uv run [flags...] <tool> ...` invocation (the
    two forms AGENTS.md's `run:` convention allows -- see the worker
    contract's engineering rules)."""

    if not tokens:
        return None
    t = list(tokens)
    if t[0] in ("python3", "python", "python3.11", "python3.12", "python3.13") and len(t) > 2 and t[1] == "-m":
        t = t[2:]
    elif t[0] == "uv" and "run" in t[1:]:
        idx = next((i for i, tok in enumerate(t) if tok in ("ruff", "pylint", "black", "isort")), None)
        t = t[idx:] if idx is not None else []
    if not t:
        return None
    if t[0] == "ruff":
        rest = t[1:]
        if "format" in rest and "--check" in rest:
            return "ruff-format-check"
        if "check" in rest:
            return "ruff-check"
        return None
    if t[0] in ("pylint", "black", "isort"):
        return t[0]
    return None


def _scan_commands_for_gen_004(  # noqa: C901
    commands: list[list[str]],
    repo_root: Path,
    *,
    found: set[str],
    black_isort_hits: list[LintToolHit],
    unresolved: list[str],
    loc: str,
    follow_scripts: bool,
) -> None:
    for tokens in commands:
        kind = _classify_lint_tokens(tokens)
        if kind in ("ruff-check", "ruff-format-check", "pylint"):
            found.add(kind)
        elif kind in ("black", "isort"):
            black_isort_hits.append(LintToolHit(kind, loc))

        if not follow_scripts:
            continue
        for candidate in tokens:
            if not CI_SCRIPT_RE.match(candidate):
                continue
            script_path = repo_root / candidate
            if not script_path.is_file():
                unresolved.append(f"{candidate} (referenced by {loc}, but not found in the repo)")
                continue
            try:
                source = script_path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(script_path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                unresolved.append(f"{candidate} (referenced by {loc}, but fails to parse)")
                continue
            sub_commands: list[list[str]] = []
            for cmd in _extract_ast_commands(script_path):
                sub_tokens = cmd.tokens
                _lineno = cmd.lineno
                i = 0
                while i < len(sub_tokens) and ENV_ASSIGN_RE.match(sub_tokens[i]):
                    i += 1
                remaining = sub_tokens[i:]
                if remaining:
                    sub_commands.append(remaining)
            # "Follow one level": the script's own subprocess calls are
            # scanned, but a further ci/*.py reference found inside THAT
            # script is not resolved again.
            _scan_commands_for_gen_004(
                sub_commands,
                repo_root,
                found=found,
                black_isort_hits=black_isort_hits,
                unresolved=unresolved,
                loc=f"{candidate} (called from {loc})",
                follow_scripts=False,
            )


def check_gen_004(repo_root: Path) -> list[Finding]:  # noqa: C901
    if not any(f.endswith(".py") for f in list_repo_files(repo_root)):
        return []  # GEN-004 only applies when the repo has Python sources

    workflows = load_workflows(repo_root)
    ci_yml = next((w for w in workflows if w.path == ".github/workflows/ci.yml"), None)
    if ci_yml is not None and ci_yml.status == LoadStatus.NEEDS_REVIEW:
        return [
            Finding(
                rule="GEN-004",
                path=ci_yml.path,
                status=Status.NEEDS_REVIEW,
                message=f"cannot evaluate GEN-004 for {ci_yml.path}: {ci_yml.reason}",
                fix="make PyYAML importable or `yq` available on PATH so ci-lint can parse "
                f"{ci_yml.path}, then re-run precheck",
            )
        ]

    fast_job: dict[str, YamlValue] | None = None
    fast_path: str | None = None
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        jobs = jobs_of(as_dict(wf.document))
        if "fast" in jobs:
            fast_job = jobs["fast"]
            fast_path = wf.path
            break

    if fast_job is None:
        return [
            Finding(
                rule="GEN-004",
                message="repository has Python sources but no job id 'fast' exists in any workflow",
                fix="add a job id 'fast' that runs 'ruff check', 'ruff format --check', and "
                "'pylint' over the Python sources (directly, or via a 'ci/*.py' script it calls)",
            )
        ]

    if "uses" in fast_job:
        return [
            Finding(
                rule="GEN-004",
                path=fast_path,
                status=Status.NEEDS_REVIEW,
                message="jobs.fast calls a reusable workflow ('uses:'); cannot statically verify its "
                "Ruff/Pylint coverage from this file",
                fix="re-run GEN-004 against the called reusable workflow file directly, or inline "
                "jobs.fast's steps",
            )
        ]

    run_texts = [s["run"] for s in steps_of(fast_job) if isinstance(s.get("run"), str)]
    commands: list[list[str]] = []
    for text in run_texts:
        commands.extend(find_commands(text))

    found: set[str] = set()
    black_isort_hits: list[LintToolHit] = []
    unresolved: list[str] = []
    _scan_commands_for_gen_004(
        commands,
        repo_root,
        found=found,
        black_isort_hits=black_isort_hits,
        unresolved=unresolved,
        loc=f"jobs.fast.steps[*] in {fast_path}",
        follow_scripts=True,
    )

    findings: list[Finding] = []
    for hit in sorted(set(black_isort_hits)):
        kind, loc = hit.kind, hit.loc
        findings.append(
            Finding(
                rule="GEN-004",
                path=fast_path,
                message=f"{loc} invokes standalone '{kind}'; policy-general.md's GEN-004 forbids "
                f"Black/isort once Ruff covers the same checks",
                fix=f"remove the '{kind}' invocation ({loc}); Ruff's 'check' (import sorting "
                "included, rule set 'I') and 'format --check' replace it",
            )
        )

    missing = {"ruff-check", "ruff-format-check", "pylint"} - found
    if missing:
        missing_desc = ", ".join(_LINT_KIND_LABEL[m] for m in sorted(missing))
        if unresolved:
            findings.append(
                Finding(
                    rule="GEN-004",
                    path=fast_path,
                    status=Status.NEEDS_REVIEW,
                    message=f"jobs.fast may be missing {missing_desc}; it also invokes "
                    f"{'; '.join(sorted(set(unresolved)))}, which could not be resolved to confirm "
                    "coverage",
                    fix="make the fast job's Ruff/Pylint invocations directly inspectable: a literal "
                    "'ruff check'/'ruff format --check'/'pylint' in a run: line, or a tracked "
                    "'ci/*.py' script that calls them via a literal subprocess argv",
                )
            )
        else:
            findings.append(
                Finding(
                    rule="GEN-004",
                    path=fast_path,
                    message=f"jobs.fast does not invoke {missing_desc}",
                    fix=f"add {missing_desc} to jobs.fast's run: steps (directly, or via a 'ci/*.py' "
                    f"script it calls) in {fast_path}",
                )
            )
    return findings


# ── TOOL-003: plain `uv run`/`uv sync` in a Rust repo ───────────────────────
#
# ci.yml#50 (closes #16): a plain `uv run`/`uv sync` first syncs the
# project, which means a hidden PEP 517 build of the Rust package inside
# whatever step invokes it -- zackees/python-rust-build-chain#9's zccache
# session errors and wrong-arch wheels came from exactly this. Scoped to a
# repo that actually builds Rust: a `pyproject.toml` with a `[build-system]`
# table, or a `Cargo.toml` with a `[workspace]` table.

ACCEPTED_UV_FLAGS: frozenset[str] = frozenset({"--no-project", "--no-sync", "--script"})
UV_SHEBANG_RE = re.compile(r"^#!\s*/usr/bin/env\s+-S\s+uv\s+(run|sync)\b(.*)$")


def _toml_table_present(path: Path, table: str) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return False
    return table in data


def _repo_builds_rust(repo_root: Path) -> bool:
    """A `pyproject.toml` with `[build-system]` (soldr/maturin/setuptools-rust/
    ...), or a `Cargo.toml` with `[workspace]` -- either means this repo
    builds Rust, so TOOL-003 applies."""

    return _toml_table_present(repo_root / "pyproject.toml", "build-system") or _toml_table_present(
        repo_root / "Cargo.toml", "workspace"
    )


def _uv_run_or_sync_kind(tokens: list[str]) -> str | None:
    if len(tokens) >= 2 and tokens[0] == "uv" and tokens[1] in ("run", "sync"):
        return tokens[1]
    return None


def _has_accepted_uv_flag(tokens: list[str]) -> bool:
    return bool(ACCEPTED_UV_FLAGS & set(tokens))


def _tool_003_finding(kind: str, path: str | None, line: int | None, loc: str) -> Finding:
    return Finding(
        rule="TOOL-003",
        path=path,
        line=line,
        message=f"{loc}: bare 'uv {kind}' syncs the project first -- in a Rust repo that is a hidden "
        "PEP 517 rebuild of the Rust package running inside this step (ci.yml#16/#50)",
        fix=f"add '--no-project' (pure tooling, no project sync), '--no-sync' (venv already synced by "
        f"an earlier step), or '--script' to this 'uv {kind}' at {loc}; or set job-level "
        "'env: UV_NO_SYNC: \"1\"' so every step's uv invocation skips the sync",
    )


def _tool_003_findings_for_run(run_text: str, path: str, loc: str, *, skip: bool) -> list[Finding]:
    if skip:
        return []
    findings: list[Finding] = []
    for tokens in find_commands(run_text):
        kind = _uv_run_or_sync_kind(tokens)
        if kind is not None and not _has_accepted_uv_flag(tokens):
            findings.append(_tool_003_finding(kind, path, None, loc))
    return findings


def _env_no_sync(value: YamlValue) -> bool:
    return isinstance(value, dict) and _truthy(value.get("UV_NO_SYNC"))


def check_tool_003_workflows(repo_root: Path) -> list[Finding]:  # noqa: C901
    if not _repo_builds_rust(repo_root):
        return []

    findings: list[Finding] = []

    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        for job_id, job in jobs_of(doc).items():
            job_no_sync = _env_no_sync(job.get("env"))
            for i, step in enumerate(steps_of(job)):
                run_text = step.get("run")
                if not isinstance(run_text, str):
                    continue
                skip = job_no_sync or _env_no_sync(step.get("env"))
                findings.extend(
                    _tool_003_findings_for_run(run_text, wf.path, f"jobs.{job_id}.steps[{i}]", skip=skip)
                )

    for act in load_composite_actions(repo_root):
        if act.status != LoadStatus.OK:
            continue
        runs = as_dict(act.document).get("runs")
        if not isinstance(runs, dict):
            continue
        composite_no_sync = _env_no_sync(runs.get("env"))
        for i, step in enumerate(as_list(runs.get("steps"))):
            if not isinstance(step, dict):
                continue
            run_text = step.get("run")
            if not isinstance(run_text, str):
                continue
            skip = composite_no_sync or _env_no_sync(step.get("env"))
            findings.extend(_tool_003_findings_for_run(run_text, act.path, f"runs.steps[{i}]", skip=skip))

    return findings


def check_tool_003_python(repo_root: Path) -> list[Finding]:
    if not _repo_builds_rust(repo_root):
        return []

    findings: list[Finding] = []
    for path in _discover_python_command_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        for cmd in _extract_ast_commands(path):
            tokens = cmd.tokens
            lineno = cmd.lineno
            kind = _uv_run_or_sync_kind(tokens)
            if kind is not None and not _has_accepted_uv_flag(tokens):
                findings.append(_tool_003_finding(kind, rel, lineno, f"line {lineno}"))
    return findings


def check_tool_003_shebangs(repo_root: Path) -> list[Finding]:
    if not _repo_builds_rust(repo_root):
        return []

    findings: list[Finding] = []
    for rel in list_repo_files(repo_root):
        path = repo_root / rel
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                first_line = fh.readline()
        except OSError:
            continue
        match = UV_SHEBANG_RE.match(first_line.rstrip("\n"))
        if match is None:
            continue
        kind = match.group(1)
        try:
            rest_tokens = shlex.split(match.group(2))
        except ValueError:
            rest_tokens = []
        if ACCEPTED_UV_FLAGS & set(rest_tokens):
            continue
        findings.append(
            Finding(
                rule="TOOL-003",
                path=rel,
                line=1,
                message=f"{rel}:1: shebang '#!/usr/bin/env -S uv {kind}' syncs the project first -- in "
                "a Rust repo that is a hidden PEP 517 rebuild of the Rust package (ci.yml#16/#50)",
                fix=f"add '--no-project', '--no-sync', or '--script' to the shebang's 'uv {kind}' in "
                f"{rel}",
            )
        )
    return findings


def check_tool_003(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_tool_003_workflows(repo_root))
    findings.extend(check_tool_003_python(repo_root))
    findings.extend(check_tool_003_shebangs(repo_root))
    return findings


def check_group4(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_tool_rules_workflows(repo_root))
    findings.extend(check_tool_rules_python(repo_root))
    findings.extend(check_tool_003(repo_root))
    findings.extend(check_cache_009(ci, repo_root))
    findings.extend(check_rust_002(repo_root))
    findings.extend(check_gen_004(repo_root))
    return findings
