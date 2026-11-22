"""GATE-014: `./push` delegates to the prooft runner. GATE-017: nothing is built
before lint passed (owner directive 2026-11-22, docs/policy-general.md).

The prooft runner owns the publishing sequence so every repository gets the
same order and the same refusals: lint first (`soldr lint` for Rust), then the
test lanes when asked (`--test`), then `git push --force-with-lease` and the
PR. `./push` is a thin delegation to it; a repository that re-implements the
sequence drifts (tests before lint, a second push path, a bypass flag).

Static clauses, checked by `precheck` (group 17) and `local-gate lint`:

GATE-014
1. a repository with `ci-attestations.yml` ships a root `./push`
   (`needs_review` for a `local-gate.toml`-only repository, which needs a
   `ci-attestations.yml` before it can move to prooft);
2. `./push` invokes `prooft` (the runner, `prooft --push`);
3. `./push` does not publish or attest by itself: no `git push`,
   `gh pr create`, `gh pr ready`, `ci-lint local-gate run`, `prooft run`
   or `prooft soldr` (the runner calls those);
4. `./push` does not pass `--no-lint`.

GATE-017
5. a repository with `./push` declares a runnable lint lane in
   `ci-attestations.yml`: a lane with `run:` whose stage is lint (declared
   `stage: lint`, or every gate of the lane is a lint check). That lane is
   what `prooft` runs first and what `prooft check-build` keys its build
   permission on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.mini_yaml import MiniYamlError, parse

RULE_PUSH = "GATE-014"
RULE_LINT_FIRST = "GATE-017"
PUSH = "push"
DEFINITION = "ci-attestations.yml"
LOCAL_GATE = "local-gate.toml"
# A delegating `./push` is a few lines; past this it is probably re-implementing the runner.
THIN_LINES = 25
LINT_CHECKS = frozenset({"fmt", "lint", "clippy", "dylint", "types", "check", "static"})
DELEGATE = 'os.execvp("prooft", ["prooft", "--push", *sys.argv[1:]])'
FIX_DELEGATE = (
    f"make `./push` a thin Python delegation to the prooft runner (GEN-005 bans an extensionless shell "
    f"script): `#!/usr/bin/env python`, then `{DELEGATE}` "
    "(resolve `prooft` from the repository's single prooft pin, GATE-014 item 5); prooft runs lint first, "
    "`--test` adds the test lanes, and it pushes with a lease and opens or reuses the PR"
)
FIX_LINT_LANE = (
    "declare the lint lane's command in ci-attestations.yml so the runner can run it first: "
    "`lanes: {rust: {run: soldr}}` for a Rust repository (`soldr lint` in porcelain mode), or "
    "`lanes:` / `lint:` / `run: [./lint]` (block style), adding `stage: lint` when its gates are not lint checks"
)
_PUBLISH = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?push\b"
    r"|\bgh\s+pr\s+(?:create|ready|merge)\b"
    r"|\bci-lint\s+local-gate\s+run\b"
    r"|\bprooft\s+(?:-C\s+\S+\s+)?(?:run|soldr|apply)\b"
)
_PROOFT = re.compile(r"\bprooft\b")
# `["git", "push"]` and `"git push"` read the same: drop quotes, brackets and commas.
_ARGV_PUNCT = re.compile(r"[\'\"\[\](),]")


@dataclass(frozen=True)
class CodeLine:
    number: int
    text: str


def _code_lines(text: str) -> list[CodeLine]:
    """Lines that are code: comments and Python docstring blocks are prose."""
    lines: list[CodeLine] = []
    quote: str | None = None  # the open docstring delimiter
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if quote is not None:
            if quote in line:
                quote = None
            continue
        if line.startswith(('"""', "'''")):
            delim = line[:3]
            if delim not in line[3:]:
                quote = delim
            continue
        if not line or line.startswith(("#", "//", "rem ", "REM ", "::")):
            continue
        lines.append(CodeLine(number, line))
    return lines


def _finding(rule: str, message: str, fix: str, line: int | None = None,
             status: Status = Status.VIOLATION, path: str = PUSH) -> Finding:
    return Finding(rule=rule, path=path, line=line, message=message, fix=fix, status=status)


def check_gate_014(repo_root: Path) -> list[Finding]:
    push = repo_root / PUSH
    if not push.is_file():
        if (repo_root / DEFINITION).is_file():
            return [_finding(RULE_PUSH, "an attesting repository has no root `./push`", FIX_DELEGATE)]
        if (repo_root / LOCAL_GATE).is_file():
            # A Local-Gate-only repository attests through `ci-lint local-gate run`;
            # moving it to prooft needs a ci-attestations.yml first, so it is reviewed, not failed.
            return [_finding(RULE_PUSH, "a local-gate repository has no root `./push`", FIX_DELEGATE,
                             status=Status.NEEDS_REVIEW)]
        return []
    try:
        text = push.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [_finding(RULE_PUSH, f"`./push` is unreadable ({exc})", FIX_DELEGATE, status=Status.NEEDS_REVIEW)]
    code = _code_lines(text)
    findings: list[Finding] = []
    if not any(_PROOFT.search(c.text) for c in code):
        findings.append(_finding(RULE_PUSH, "`./push` does not delegate to the prooft runner", FIX_DELEGATE))
    for c in code:
        number, line = c.number, c.text
        match = _PUBLISH.search(" ".join(_ARGV_PUNCT.sub(" ", line).split()))
        if match:
            findings.append(_finding(
                RULE_PUSH, f"`./push` runs `{match.group(0)}` itself instead of leaving publishing to `prooft --push`",
                FIX_DELEGATE, number))
        if "--no-lint" in line:
            findings.append(_finding(
                RULE_PUSH, "`./push` skips lint (`--no-lint`); lint is the first stage of every push",
                "drop `--no-lint`; a contributor who must skip lint runs `prooft --no-lint` by hand (discouraged)",
                number))
    if len(code) > THIN_LINES and not findings:
        findings.append(_finding(
            RULE_PUSH, f"`./push` has {len(code)} lines of code; a delegation to `prooft --push` needs a few",
            FIX_DELEGATE, status=Status.NEEDS_REVIEW))
    return findings


def _lane_checks(gates: object, lane: str) -> list[str]:
    """The check names (last path segment) of every gate `lane` proves."""
    if not isinstance(gates, dict):
        return []
    return [str(path).rsplit("/", 1)[-1] for path, body in gates.items()
            if isinstance(body, dict) and body.get("lane") == lane]


def runnable_lint_lanes(doc: object) -> list[str]:
    """Lanes the prooft runner runs first: a `run`, and stage lint (declared or inferred)."""

    if not isinstance(doc, dict):
        return []
    lanes = doc.get("lanes")
    found: list[str] = []
    for lane, spec in (lanes.items() if isinstance(lanes, dict) else ()):
        if not isinstance(spec, dict) or "run" not in spec:
            continue
        stage = spec.get("stage")
        if stage is None:
            gate_checks = _lane_checks(doc.get("gates"), str(lane))
            stage = "lint" if gate_checks and all(c in LINT_CHECKS for c in gate_checks) else "test"
        if stage == "lint":
            found.append(str(lane))
    return found


def check_gate_017(repo_root: Path) -> list[Finding]:
    if not (repo_root / PUSH).is_file():
        return []
    path = repo_root / DEFINITION
    if not path.is_file():
        return [_finding(RULE_LINT_FIRST, "`./push` exists but there is no ci-attestations.yml to name its lint lane",
                         FIX_LINT_LANE, path=DEFINITION)]
    try:
        doc = parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, MiniYamlError) as exc:
        return [_finding(RULE_LINT_FIRST, f"ci-attestations.yml is unreadable ({exc})", FIX_LINT_LANE,
                         status=Status.NEEDS_REVIEW, path=DEFINITION)]
    if not runnable_lint_lanes(doc):
        return [_finding(RULE_LINT_FIRST, "ci-attestations.yml declares no runnable lint lane, so `prooft` cannot "
                         "lint first and `prooft check-build` refuses every build", FIX_LINT_LANE, path=DEFINITION)]
    return []


def check_push_prooft(repo_root: Path) -> list[Finding]:
    return [*check_gate_014(repo_root), *check_gate_017(repo_root)]
