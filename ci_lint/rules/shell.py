"""Group 3: shell budget (GEN-005).

Every `run:` step is at most one line calling a single allowlisted command
(round-1A brief; see also the worker contract's "YAML `run:` steps are ONE
line" rule). The C/C++ allowlist (zackees/ci.yml#393, docs/policy-cpp.md):
a CMake configure (`cmake -S/-B`, `cmake --preset`), `cmake --build` or
`ctest` invocation is one command even when it is wrapped over several
lines with trailing-backslash continuations -- configure lines carry many
`-D` flags. Control syntax is still banned inside it. This module also bans `.sh/.ps1/.bat/.cmd` files and
extensionless scripts with a bash/sh shebang anywhere in the tracked tree,
and `shell: pwsh|powershell|cmd`. The draft ci.toml's `[[exceptions]]` entry
for `ci.sh` (GEN-005) is expected to turn exactly that finding into
`approved_exception` once `ci_lint.exceptions` runs.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.repo_files import list_repo_files
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import (
    ParsedYamlFile,
    as_dict,
    as_list,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)
from ci_lint.yaml_io import LoadStatus, YamlValue

SCRIPT_EXTS: frozenset[str] = frozenset({".sh", ".ps1", ".bat", ".cmd"})
BANNED_SHELLS: frozenset[str] = frozenset({"pwsh", "powershell", "cmd"})
CONTROL_TOKENS: tuple[str, ...] = ("&&", "||", ";", "|", "$(", "`", "<<")
LEADING_CONTROL_RE = re.compile(r"^\s*(if|for|while)\s")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CMAKE_CONFIGURE_FLAGS: tuple[str, ...] = ("-S", "-B", "--preset")


def _logical_lines(text: str) -> list[str]:
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
    return [line for line in out if line.strip()]


def cpp_allowlisted_command(run_text: str) -> str | None:
    """The allowlisted C/C++ command `run_text` is -- `cmake configure`,
    `cmake --build` or `ctest` -- when it is exactly one logical command
    (trailing-backslash continuations joined), else None (#393)."""

    lines = _logical_lines(run_text)
    if len(lines) != 1:
        return None
    try:
        tokens = shlex.split(lines[0], comments=True)
    except ValueError:
        return None
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    prog = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")
    if prog == "ctest":
        return "ctest"
    if prog != "cmake":
        return None
    if "--build" in tokens:
        return "cmake --build"
    if any(t == f or (t.startswith(f) and len(t) > len(f)) for t in tokens[1:] for f in _CMAKE_CONFIGURE_FLAGS):
        return "cmake configure"
    return None


def _run_step_finding(run_text: str, path: str, loc: str) -> Finding | None:
    lines = [line for line in run_text.splitlines() if line.strip()]
    cpp = cpp_allowlisted_command(run_text)
    reasons: list[str] = []
    if len(lines) > 1 and cpp is None:
        reasons.append("more than one line")
    found_tokens = [t for t in CONTROL_TOKENS if t in run_text]
    if found_tokens:
        reasons.append(f"control syntax ({', '.join(found_tokens)})")
    if any(LEADING_CONTROL_RE.match(line) for line in lines):
        reasons.append("leading if/for/while")
    if not reasons:
        return None
    fix = (
        f"replace {loc} with a single call to a Python script, e.g. "
        "'run: python3 ci/<script>.py ...' (or 'uv run --no-project --script ...'); move any "
        "control flow into that script"
    )
    if cpp is not None:
        fix = (
            f"keep {loc} a single '{cpp}' command with no shell syntax: drop '$(nproc)' for "
            "'--parallel' / 'ctest -j' (or CMAKE_BUILD_PARALLEL_LEVEL / CTEST_PARALLEL_LEVEL in env:), "
            "or " + fix
        )
    return Finding(
        rule="GEN-005",
        path=path,
        message=f"{loc}: run: step violates the shell budget ({'; '.join(reasons)})",
        fix=fix,
    )


def _shell_key_finding(value: object, path: str, loc: str) -> Finding | None:
    if isinstance(value, str) and value.lower() in BANNED_SHELLS:
        return Finding(
            rule="GEN-005",
            path=path,
            message=f"{loc}: shell: '{value}' is banned",
            fix=f"remove 'shell: {value}' at {loc}; use the default shell to call a Python "
            "script (python3 on every platform, or 'uv run --no-project --script ...')",
        )
    return None


def _nested(doc: object, *keys: str) -> object:
    cur = doc
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _check_steps(
    findings: list[Finding], path: str, job_prefix: str, steps: list[dict[str, YamlValue]]
) -> None:
    for i, step in enumerate(steps):
        loc = f"{job_prefix}.steps[{i}]"
        run_text = step.get("run")
        if isinstance(run_text, str):
            f = _run_step_finding(run_text, path, loc)
            if f is not None:
                findings.append(f)
        f = _shell_key_finding(step.get("shell"), path, f"{loc}.shell")
        if f is not None:
            findings.append(f)


def check_tracked_scripts(repo_root: Path) -> list[Finding]:
    # ci_lint.repo_files.list_repo_files: every git-tracked (or not-yet-
    # ignored) file inside a git work tree, falling back to a filesystem
    # walk (skipping VCS/build noise) for a bare on-disk fixture tree that
    # is not itself inside any git repository.
    findings: list[Finding] = []
    for rel in list_repo_files(repo_root):
        rel_path = Path(rel)
        if rel_path.suffix.lower() in SCRIPT_EXTS:
            findings.append(
                Finding(
                    rule="GEN-005",
                    path=rel,
                    message=f"tracked script file '{rel}' ({rel_path.suffix}) -- CI logic must live "
                    "in Python, not shell/batch/PowerShell files",
                    fix=f"delete {rel} and move its logic into ci/<name>.py, invoked as a single "
                    "'run: python3 ci/<name>.py ...' step",
                )
            )
            continue
        if rel_path.suffix == "":
            full = repo_root / rel_path
            try:
                with full.open("rb") as fh:
                    first_line = fh.readline(200)
            except OSError:
                continue
            if first_line.startswith(b"#!") and (
                b"bash" in first_line or b"/sh" in first_line or first_line.rstrip().endswith(b"sh")
            ):
                findings.append(
                    Finding(
                        rule="GEN-005",
                        path=rel,
                        message=f"tracked extensionless file '{rel}' has a bash/sh shebang",
                        fix=f"delete {rel} and move its logic into ci/<name>.py",
                    )
                )
    return findings


def check_group3(ci: CiToml, repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = check_tracked_scripts(repo_root)

    workflows: list[ParsedYamlFile] = load_workflows(repo_root)
    actions: list[ParsedYamlFile] = load_composite_actions(repo_root)

    for f in workflows + actions:
        if f.status == LoadStatus.NEEDS_REVIEW:
            findings.append(
                Finding(
                    rule="GEN-005",
                    path=f.path,
                    status=Status.NEEDS_REVIEW,
                    message=f"cannot evaluate GEN-005 for {f.path}: {f.reason}",
                    fix="make PyYAML importable or `yq` available on PATH so ci-lint can parse "
                    f"{f.path}, then re-run precheck",
                )
            )

    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        f = _shell_key_finding(_nested(doc, "defaults", "run", "shell"), wf.path, "defaults.run.shell")
        if f is not None:
            findings.append(f)
        for job_id, job in jobs_of(doc).items():
            f = _shell_key_finding(
                _nested(job, "defaults", "run", "shell"),
                wf.path,
                f"jobs.{job_id}.defaults.run.shell",
            )
            if f is not None:
                findings.append(f)
            _check_steps(findings, wf.path, f"jobs.{job_id}", steps_of(job))

    for act in actions:
        if act.status != LoadStatus.OK:
            continue
        runs = as_dict(act.document).get("runs")
        if isinstance(runs, dict):
            steps = [s for s in as_list(runs.get("steps")) if isinstance(s, dict)]
            _check_steps(findings, act.path, "runs", steps)

    return findings
