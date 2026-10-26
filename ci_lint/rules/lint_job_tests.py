"""GEN-019: a lint job runs no test suite (zackees/ci.yml#111).

Evidence: running-process job 109269111902 -- a 442 s preflight where Lint
took 39 s but unit tests (164 s) ran in the same job, so lint feedback
waited on the slowest test. Tests belong in their own lanes.

Static signal (workflow `run:` lines, plus one level into a `ci/*.py`
script such a line names -- GEN-004's convention):

- violation: a lint job runs a test suite. A lint job is one whose job id
  or `name:` contains the word `lint` or `clippy` (delimited by anything but
  a letter/digit, case-insensitive -- so a quick-gate `fast-lint` /
  `quick_lint` job and a job named `Lint` count, while `dylint` and
  `linter` do not). A test suite is `cargo test`, `cargo nextest run` (any
  wrapper, e.g. `soldr cargo ...`), or `pytest` (`pytest`, `python -m
  pytest`, `uv run pytest`).

Not a finding: building the native module needed for pylint imports (no
test invocation), and stable clippy plus nightly Dylint in one lint job
(RUST-032 overlap). A bare `fast`/`quick` job not named as a lint job is
not classified: the policy's quick-gate case is a *lint* job, and job ids
alone cannot prove a `fast` job is lint-only by design.

A same-line `# ci-lint: allow GEN-019 <reason>` excuses that one line.
"""

from __future__ import annotations

import re
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.test_invocations import (
    allowed,
    find_test_invocation,
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

RULE = "GEN-019"
_LINT_WORD_RE = re.compile(r"(?<![a-z0-9])(lint|clippy)(?![a-z0-9])", re.IGNORECASE)
_FIX = (
    "move the test suite into its own job (e.g. split `lint` and `unit-test`) so slow tests never "
    "delay lint feedback; or excuse the line with '# ci-lint: allow GEN-019 <reason>'"
)


def is_lint_job(job_id: str, name: str | None) -> bool:
    return any(_LINT_WORD_RE.search(text) for text in (job_id, name or "") if text)


def find_test_suite(tokens: tuple[str, ...]) -> str | None:
    found = find_test_invocation(tokens)
    if found is not None:
        return "cargo nextest run" if found[0] == "nextest" else "cargo test"
    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] in ("pytest", "py.test", "pytest.exe"):
            return "pytest"
        if tok == "-m" and i + 1 < len(tokens) and tokens[i + 1] == "pytest":
            return "pytest"
    return None


def check_gen_019(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        raw_lines = raw_lines_of(repo_root / wf.path)
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            name = job.get("name")
            if not is_lint_job(job_id, name if isinstance(name, str) else None):
                continue
            for i, step in enumerate(steps_of(job)):
                run_text = step.get("run")
                if isinstance(run_text, str):
                    loc = f"jobs.{job_id}.steps[{i}]"
                    findings.extend(_run_findings(repo_root, wf.path, raw_lines, run_text, loc, job_id))
    return findings


def _run_findings(
    repo_root: Path, path: str, raw_lines: list[str], run_text: str, loc: str, job_id: str
) -> list[Finding]:
    out: list[Finding] = []
    for ln in logical_lines(run_text):
        raw = with_yaml_comment(ln, raw_lines)
        for tokens in split_commands(ln):
            suite = find_test_suite(tuple(tokens))
            if suite is not None and not allowed(raw, RULE):
                out.append(_finding(path, None, loc, job_id, suite))
            for tok in tokens:
                if CI_SCRIPT_RE.match(tok) and (repo_root / tok).is_file():
                    out.extend(_script_findings(repo_root, tok, loc, job_id))
    return out


def _script_findings(repo_root: Path, rel: str, loc: str, job_id: str) -> list[Finding]:
    script = repo_root / rel
    out: list[Finding] = []
    for call in script_calls(script):
        suite = find_test_suite(call.tokens)
        if suite is None or allowed(source_line(script, call.line), RULE):
            continue
        out.append(_finding(rel, call.line, f"{loc} -> {rel} line {call.line}", job_id, suite))
    return out


def _finding(path: str, line: int | None, loc: str, job_id: str, suite: str) -> Finding:
    return Finding(
        rule=RULE,
        path=path,
        line=line,
        message=f"{loc}: lint job '{job_id}' runs a test suite ('{suite}'); lint feedback then waits "
        "on the tests",
        fix=_FIX,
    )
