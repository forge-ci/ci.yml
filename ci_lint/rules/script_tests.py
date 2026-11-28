"""GATE-018 (static): a script-style test the gate never runs.

docs/policy-general.md, "Script-style tests run in the gate": a test file
that is a script -- it has a `__main__` entry and no pytest/unittest test
cases, so `pytest`/`unittest discover` never collect it -- only runs when
something names it. If neither the local gate nor any workflow names it, a
change can break it silently. forge-ci/gh-runner#249 merged with
`tests/lanes_test.py` (the nix-evaluating mirror check between `fleet.toml`
and `hosts/*/default.nix`) failing five checks: the repository has no
workflows and no gate, and pytest does not collect that file.

What it reports:

- a script-style test (`tests/`/`ci/` file named `*_test.py` or `test_*.py`
  with `if __name__ == "__main__":` and no `def test_`/`TestCase`) whose
  repo-relative path or file name appears in none of: the gate's `run` and
  lane argv, a `ci/`/`scripts/` script those argv name, a root
  `Makefile`/`justfile`, `local-gate.toml`, or a workflow file;
- a repository with tests (`tests/` or `ci/test_*.py`), no workflow file and
  no gate declaration: nothing enforces any of its tests.

A `# ci-lint: allow GATE-018 <reason>` line in the test file excuses it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ci_lint.finding import Finding, Status

RULE = "GATE-018"
POLICY = "docs/policy-general.md#script-style-tests-run-in-the-gate-gate-018"
_MAIN = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.MULTILINE)
_CASES = re.compile(r"^\s*(?:async\s+)?def\s+test_|\bTestCase\b|\bIsolatedAsyncioTestCase\b", re.MULTILINE)
_ALLOW = re.compile(rf"#\s*ci-lint:\s*allow\s+{RULE}\s+\S")
_ROOT_FILES = ("Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", "local-gate.toml", "ci.toml")
_SCRIPT = re.compile(r"^(?:\./)?(?:ci|scripts)/\S+\.(?:py|sh|bash)$")


def _is_test_name(name: str) -> bool:
    return name.endswith(".py") and (name.endswith("_test.py") or name.startswith("test_"))


def _candidates(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for top in ("tests", "ci"):
        base = repo_root / top
        if base.is_dir():
            out.extend(p for p in sorted(base.rglob("*.py")) if _is_test_name(p.name) and "fixtures" not in p.parts)
    return out


def is_script_test(text: str) -> bool:
    return bool(_MAIN.search(text)) and not _CASES.search(text)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _workflow_files(repo_root: Path) -> list[Path]:
    wf = repo_root / ".github" / "workflows"
    return sorted(p for p in wf.glob("*") if p.suffix in (".yml", ".yaml")) if wf.is_dir() else []


def _haystack(repo_root: Path, gate_runs: tuple[tuple[str, ...], ...]) -> str:
    parts: list[str] = [" ".join(argv) for argv in gate_runs]
    for argv in gate_runs:
        for tok in argv:
            if _SCRIPT.match(tok):
                parts.append(_read(repo_root / tok.removeprefix("./")))
    parts.extend(_read(repo_root / name) for name in _ROOT_FILES)
    parts.extend(_read(p) for p in _workflow_files(repo_root))
    return "\n".join(parts)


def check_gate_018(repo_root: Path, gate_runs: tuple[tuple[str, ...], ...] = (), gate_declared: bool | None = None) -> list[Finding]:
    """`gate_runs` are the declared gate's `run` and lane argv; `gate_declared`
    defaults to whether any argv was given."""
    declared = bool(gate_runs) if gate_declared is None else gate_declared
    candidates = _candidates(repo_root)
    findings: list[Finding] = []
    has_tests = (repo_root / "tests").is_dir() or bool(candidates)
    if has_tests and not declared and not _workflow_files(repo_root):
        findings.append(
            Finding(
                rule=RULE,
                path=".",
                status=Status.VIOLATION,
                message="the repository has tests but no workflow and no local gate: nothing runs them before a merge",
                fix="declare a local gate (local-gate.toml [gate] with mode = \"enforce\") whose run executes the "
                f"test suite and every script-style test, and install its pre-push hook ({POLICY})",
            )
        )
    hay = _haystack(repo_root, gate_runs)
    for path in candidates:
        text = _read(path)
        if not is_script_test(text) or _ALLOW.search(text):
            continue
        rel = path.relative_to(repo_root).as_posix()
        if rel in hay or path.name in hay:
            continue
        findings.append(
            Finding(
                rule=RULE,
                path=rel,
                status=Status.VIOLATION,
                message="script-style test (a __main__ entry, no test cases) that no gate, script or workflow runs: "
                "pytest/unittest never collect it, so it can fail unnoticed",
                fix=f"add `{rel}` to the gate's run or a gate lane (or a workflow job), or convert it to "
                f"collected test cases ({POLICY})",
            )
        )
    return findings
