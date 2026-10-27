"""REL-008: a release-acceptance evidence probe is total.

A probe that proves a *published or promoted* artifact must always emit its
evidence row. A skip removes the row, and an aggregate that requires one row
per declared target then either fails closed -- rolling back a release that
was actually fine -- or, if it discovers its denominator by globbing, passes
vacuously. A probe whose precondition is genuinely absent must record an
explicit typed marker (``{"unavailable": "<why>"}``) inside the row it emits,
never vanish.

Motivating incident (talos-hq/clud, release run 40478241694, tag 2.8.37): the
release promoted 2.8.37 to the ``latest-stable`` channel and then rolled it
straight back to 2.8.31. Every one of the five native runners plus the
NixOS/distro guests installed and verified the promoted bytes, but
``tests/test_installer_public_ci.py``'s ``assert_older_installer_discovery``
called ``pytest.skip(...)`` when there was no *prior* catalog release to
compare against, so that host emitted no row and the aggregate
(``ci/installer_public_evidence.py::require_evidence``,
``if len(rows) != len(expected): raise ValueError("missing public host
evidence")``) failed closed. A benign, unverifiable precondition became a
release-blocking failure. Fixed by making the probe total (clud 2.8.38, PRs
talos-hq/clud#2253/#2255).

The static check scans Python sources on a release-acceptance evidence path
(a file whose name or path matches ``installer``, ``acceptance``,
``evidence``, ``release_gate``, ``public_release`` or ``public_catalog``) and
flags:

- **clause A** (``violation``): the probe suppresses its evidence row -- a
  bare ``pytest.skip(...)``, ``raise unittest.SkipTest``/``raise SkipTest``,
  ``pytest.importorskip(...)``, or a ``sys.exit(0)`` in a file that also
  writes an evidence row. ``pytest.skip.Exception`` (and a
  ``pytest.raises(pytest.skip.Exception)`` match) is *not* a suppression and
  is not flagged.
- **clause A, workflow input** (``needs_review``): a
  ``pytest.mark.skipif(...)`` whose ``reason=`` names a workflow-provided
  input (an env var such as ``os.environ.get("CLUD_...")``); when that input
  is absent the probe still vanishes.
- **clause B** (``needs_review``): a file that collects per-target evidence
  rows from a directory (a ``Path.glob("*.json")``/``rglob("*.json")`` or
  ``os.listdir`` loaded as rows) but shows no declared-expectation signal, so
  its denominator is whatever it happens to find and a short row set passes
  vacuously. A purely static check cannot follow the comparison through
  indirection, so this is ``needs_review``, not a violation.

Detection is text/AST only -- no JSON is ever parsed (PY-002). A same-line
``# ci-lint: allow REL-008 <reason>`` trailing an offending construct excuses
that one line; a bare allow comment with no reason is not an excuse.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.repo_files import list_repo_files

RULE = "REL-008"
POLICY = "docs/policy-general.md#a-release-acceptance-probe-is-total-rel-008"

INCIDENT = (
    "talos-hq/clud release run 40478241694, tag 2.8.37: a host target's "
    "assert_older_installer_discovery called pytest.skip() when there was no prior "
    "catalog release, emitted no evidence row, and ci/installer_public_evidence.py::"
    "require_evidence failed with `ValueError: missing public host evidence`, rolling a "
    "healthy release back to 2.8.31"
)

# Directories a release-acceptance scan never reads. `list_repo_files` already
# drops these (git-tracked scope / filesystem walk), but the rule states them
# explicitly so a fixture directory or a stray build tree can never be scanned.
_EXCLUDED_PARTS: tuple[str, ...] = (
    "target",
    ".git",
    "node_modules",
    "dist",
    "__pycache__",
)
_EXCLUDED_PART_PREFIXES: tuple[str, ...] = (".venv",)
_EXCLUDED_PATH_PREFIXES: tuple[str, ...] = ("ci_lint/tests/fixtures/",)

# A release-acceptance evidence path: its file name or path names the probe.
_ACCEPTANCE_PATH = re.compile(
    r"installer|acceptance|evidence|release_gate|public_release|public_catalog",
    re.IGNORECASE,
)

# The house escape hatch: a reason is required, a bare comment is not an excuse.
_ALLOW = re.compile(rf"ci-lint:\s*allow\s+{re.escape(RULE)}\s+\S")

# A file "writes an evidence row" when it serialises a row or a document.
_EVIDENCE_WRITE = re.compile(r"json\.dumps?\s*\(|write_text\s*\(|\.write\s*\(")
_EVIDENCE_WORD = re.compile(r"\bevidence\b|\brows?\b", re.IGNORECASE)

# A skipif `reason=` that names a workflow-provided input.
_ENV_INPUT = re.compile(r"os\.environ|os\.getenv|\b(?:CLUD|CLUED|CI)_[A-Z0-9_]+")

# Clause B: per-target row collection from a directory, and a declared
# denominator (a comparison that ties the collection to an expectation set).
_ROW_GLOB = re.compile(r"\.(?:glob|rglob)\s*\([^)]*\bjson\b|\bos\.listdir\s*\(")
_ROW_LOAD = re.compile(r"json\.loads?\s*\(")
_DECLARED_DENOMINATOR = re.compile(
    r"len\s*\([^)]*\)\s*!=|!= \s*len\s*\(|\bREQUIRED_TARGETS\b|\bexpected(?:_targets)?\b"
)

_SKIP_CONSTRUCTS = (
    "calls `pytest.skip(...)`, which emits no evidence row",
    "calls `pytest.importorskip(...)`, which emits no evidence row",
    "raises `unittest.SkipTest`, which emits no evidence row",
    "exits with `sys.exit(0)` in a file that also writes an evidence row",
)


@dataclass(frozen=True)
class _Offense:
    """One row-suppressing construct, at its 1-based line."""

    line: int
    what: str


def _excluded(rel: str) -> bool:
    if any(rel.startswith(prefix) for prefix in _EXCLUDED_PATH_PREFIXES):
        return True
    for part in Path(rel).parts:
        if part in _EXCLUDED_PARTS or part.startswith(_EXCLUDED_PART_PREFIXES):
            return True
    return False


def _is_acceptance_path(rel: str) -> bool:
    return _ACCEPTANCE_PATH.search(rel) is not None


def _allowed(line: str) -> bool:
    return _ALLOW.search(line) is not None


def _bare_pytest_skip(call: ast.Call) -> bool:
    """`pytest.skip(...)` only -- never `pytest.skip.Exception` (an attribute,
    not a call) and never `pytest.raises(pytest.skip.Exception)`."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "skip"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pytest"
    )


def _pytest_importorskip(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "importorskip"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pytest"
    )


def _skip_test_raise(node: ast.Raise) -> bool:
    exc = node.exc
    if exc is None:
        return False
    target = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(target, ast.Name):
        return target.id == "SkipTest"
    if isinstance(target, ast.Attribute):
        return target.attr == "SkipTest"
    return False


def _sys_exit_zero(call: ast.Call) -> bool:
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "exit"):
        return False
    if not (isinstance(func.value, ast.Name) and func.value.id == "sys"):
        return False
    if len(call.args) != 1:
        return False
    arg = call.args[0]
    return isinstance(arg, ast.Constant) and arg.value == 0


def _clause_a_offenses(tree: ast.AST, source: str, lines: list[str]) -> list[_Offense]:
    writes_row = bool(_EVIDENCE_WRITE.search(source) and _EVIDENCE_WORD.search(source))
    offenses: list[_Offense] = []
    for node in ast.walk(tree):
        what: str | None = None
        if isinstance(node, ast.Call) and _bare_pytest_skip(node):
            what = _SKIP_CONSTRUCTS[0]
        elif isinstance(node, ast.Call) and _pytest_importorskip(node):
            what = _SKIP_CONSTRUCTS[1]
        elif isinstance(node, ast.Raise) and _skip_test_raise(node):
            what = _SKIP_CONSTRUCTS[2]
        elif writes_row and isinstance(node, ast.Call) and _sys_exit_zero(node):
            what = _SKIP_CONSTRUCTS[3]
        if what is None:
            continue
        line = getattr(node, "lineno", 0)
        if 0 < line <= len(lines) and _allowed(lines[line - 1]):
            continue
        offenses.append(_Offense(line, what))
    return offenses


def _skipif_reason_offense(tree: ast.AST, source: str, lines: list[str]) -> _Offense | None:
    """One `pytest.mark.skipif(...)` whose `reason=` names a workflow input."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "skipif"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "reason":
                continue
            segment = ast.get_source_segment(source, keyword.value) or ""
            env = _ENV_INPUT.search(segment)
            if env is None:
                continue
            line = getattr(node, "lineno", 0)
            if 0 < line <= len(lines) and _allowed(lines[line - 1]):
                continue
            return _Offense(line, env.group(0))
    return None


def _clause_a_findings(rel: str, offenses: list[_Offense]) -> list[Finding]:
    if not offenses:
        return []
    first = min(offenses, key=lambda o: o.line)
    if len(offenses) == 1:
        detail = first.what
    else:
        detail = f"{first.what} (and {len(offenses) - 1} more row-suppressing construct(s))"
    return [
        Finding(
            rule=RULE,
            path=rel,
            line=first.line,
            message=f"release-acceptance probe {rel}:{first.line} {detail}. An aggregate that "
            "requires exactly one evidence row per declared target then either fails closed and "
            f"rolls back a healthy release, or passes vacuously ({INCIDENT})",
            fix="make the probe total: record the unavailable precondition as an explicit marker "
            "inside the row it emits, e.g. {\"unavailable\": \"no catalog-aware prior release\", "
            "\"catalog_move_version\": \"2.8.35\", \"legacy_host\": ...}, instead of skipping -- "
            "never let a benign missing precondition remove the row (talos-hq/clud run 40478241694, "
            "tag 2.8.37 rolled back to 2.8.31 with 'ValueError: missing public host evidence'); a "
            f"deliberate skip takes a same-line `# ci-lint: allow {RULE} <reason>` ({POLICY})",
        )
    ]


def _clause_a_input_finding(rel: str, offense: _Offense) -> Finding:
    return Finding(
        rule=RULE,
        path=rel,
        line=offense.line,
        status=Status.NEEDS_REVIEW,
        message=f"{rel}:{offense.line}: a `pytest.mark.skipif(...)` whose `reason=` names a "
        f"workflow-provided input (`{offense.what}`) -- when that input is absent the probe skips "
        "and its evidence row vanishes, the same failure mode as a bare skip",
        fix="record the missing workflow input inside the row the probe emits "
        "(`{\"unavailable\": \"<why>\"}`), never gate the row's existence on the input; or confine "
        f"the mark to a non-acceptance path ({POLICY})",
    )


def _clause_b_finding(rel: str, source: str) -> Finding | None:
    if not _ROW_GLOB.search(source) or not _ROW_LOAD.search(source):
        return None
    if _DECLARED_DENOMINATOR.search(source):
        return None
    return Finding(
        rule=RULE,
        path=rel,
        status=Status.NEEDS_REVIEW,
        message=f"{rel} collects per-target evidence rows from a directory (a `*.json` "
        "glob/rglob or `os.listdir` loaded as rows) but shows no declared expectation set -- its "
        "denominator is whatever it happens to find, so an empty or short row set passes vacuously",
        fix="compare the collected rows against a declared expectation set (e.g. "
        "`if len(rows) != len(REQUIRED_TARGETS): raise ValueError('missing evidence')`), so a "
        f"missing row fails loudly instead of passing ({POLICY})",
    )


def _scan_file(rel: str, source: str) -> list[Finding]:
    lines = source.splitlines()
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        findings.extend(_clause_a_findings(rel, _clause_a_offenses(tree, source, lines)))
        input_offense = _skipif_reason_offense(tree, source, lines)
        if input_offense is not None:
            findings.append(_clause_a_input_finding(rel, input_offense))
    clause_b = _clause_b_finding(rel, source)
    if clause_b is not None:
        findings.append(clause_b)
    return findings


def check_rel_008(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel in list_repo_files(repo_root):
        if not rel.endswith(".py") or _excluded(rel) or not _is_acceptance_path(rel):
            continue
        path = repo_root / rel
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(_scan_file(rel, source))
    return findings
