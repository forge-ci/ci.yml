"""GEN-010 (issue #5, renumbered from the clud-ci-cost.md case study's
original `GEN-005` candidate -- see AGENTS.md item 7): a repository's own
docs/instruction files assert CI enforcement that its settings or
workflows don't actually have. Split across two check surfaces because
only part of it is checkable offline:

- Static (this module, group-N precheck rule, no network): scans
  README.md / AGENTS.md / CLAUDE.md and docs/architecture/*.md for three
  claim shapes -- an active merge queue, enforced branch protection, and
  native (non-Linux-host) Windows/macOS Dylint. The native-Dylint claim is
  fully verifiable offline (this repository's own `.github/workflows/*`
  are on disk): a claim with no matching native Dylint job anywhere in the
  workflows is a VIOLATION. The merge-queue/branch-protection claims need
  a live GitHub API read to confirm or refute (RUST/GEN-006 already does
  that fetch for the same branch), so a static-only run reports them
  NEEDS_REVIEW -- exactly the "needs_review allowed" the brief calls for --
  and `ci_lint.settings_audit.check_gen_010` (live) resolves them for real
  using the branch-protection/rulesets payloads GEN-006/011 already fetch.

This module never edits or deletes a doc; ci-lint findings only ever point
an agent at what to fix ("the claim in file:line doesn't match reality"),
never "add it to the allowlist".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import discover_workflow_files
from ci_lint.yaml_io import load_yaml_file

DOC_ROOT_CANDIDATES: tuple[str, ...] = ("README.md", "AGENTS.md", "CLAUDE.md")
DOC_SUBDIRS: tuple[str, ...] = ("docs/architecture",)

# Affirmative present-tense claims only -- discussing the RULE ITSELF (as
# this repository's own policy docs and case studies do, at length) reads
# very differently from asserting the repository's OWN CI has the thing.
# Kept deliberately narrow to avoid false-positiving on ci.yml's own docs.
MERGE_QUEUE_CLAIM_RE = re.compile(
    r"\b(uses?|enforc\w*|requires?|via)\b[^.\n]{0,40}\bmerge queue\b"
    r"|\bmerge queue\b[^.\n]{0,40}\b(is (configured|enabled|active)|enforces)\b",
    re.IGNORECASE,
)
BRANCH_PROTECTION_CLAIM_RE = re.compile(
    r"\b(protected|required status check(s)?)\b[^.\n]{0,60}\b(enforc\w*|require\w*|block\w*)\b"
    r"|\bbranch protection\b[^.\n]{0,40}\b(is (configured|enabled|active))\b",
    re.IGNORECASE,
)
NATIVE_DYLINT_CLAIM_RE = re.compile(
    r"\bnative\b[^.\n]{0,80}\b(windows|macos)\b[^.\n]{0,80}\bdylint\b"
    r"|\bdylint\b[^.\n]{0,80}\bnative\b[^.\n]{0,80}\b(windows|macos)\b"
    r"|\bevery (pr|run)\b[^.\n]{0,60}\bnative\b[^.\n]{0,80}\bdylint\b",
    re.IGNORECASE,
)

ClaimKind = str  # "merge_queue" | "branch_protection" | "native_dylint"


@dataclass(frozen=True)
class DocClaim:
    path: str  # repo-relative
    line: int
    kind: ClaimKind
    text: str


def _discover_doc_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for name in DOC_ROOT_CANDIDATES:
        p = repo_root / name
        if p.is_file():
            out.append(p)
    for sub in DOC_SUBDIRS:
        d = repo_root / sub
        if d.is_dir():
            out.extend(sorted(d.glob("*.md")))
    return out


def scan_text(rel_path: str, text: str) -> list[DocClaim]:
    claims: list[DocClaim] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if MERGE_QUEUE_CLAIM_RE.search(line):
            claims.append(DocClaim(rel_path, lineno, "merge_queue", stripped))
        if BRANCH_PROTECTION_CLAIM_RE.search(line):
            claims.append(DocClaim(rel_path, lineno, "branch_protection", stripped))
        if NATIVE_DYLINT_CLAIM_RE.search(line):
            claims.append(DocClaim(rel_path, lineno, "native_dylint", stripped))
    return claims


def scan_repo_docs(repo_root: Path) -> list[DocClaim]:
    """Pure filesystem; never raises on an unreadable file (skips it)."""

    claims: list[DocClaim] = []
    for path in _discover_doc_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        claims.extend(scan_text(path.relative_to(repo_root).as_posix(), text))
    return claims


def _job_runs_on_labels(job: object) -> list[str]:
    """runs-on label strings of one job (best-effort; a dynamic matrix
    expression is recorded verbatim, never silently dropped)."""

    if not isinstance(job, dict):
        return []
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [str(x) for x in runs_on]
    return []


def _is_dylint_job(job_id: str, job: object) -> bool:
    if "dylint" in job_id.lower():
        return True
    if isinstance(job, dict):
        name = job.get("name")
        if isinstance(name, str) and "dylint" in name.lower():
            return True
    return False


def _has_native_dylint_job(repo_root: Path) -> bool:
    for wf_path in discover_workflow_files(repo_root):
        result = load_yaml_file(wf_path)
        document = result.document
        if not isinstance(document, dict):
            continue
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job_id, str) or not _is_dylint_job(job_id, job):
                continue
            for label in _job_runs_on_labels(job):
                low = label.lower()
                if "windows" in low or "macos" in low or "mac-os" in low:
                    return True
    return False


@dataclass(frozen=True)
class Gen010Static:
    findings: list[Finding]
    claims: list[DocClaim]


def check_gen_010_static(repo_root: Path) -> Gen010Static:
    """Returns the findings and all claims found -- callers that also have live
    access (settings_audit.check_gen_010) reuse the returned claims instead
    of re-scanning the filesystem."""

    claims = scan_repo_docs(repo_root)
    findings: list[Finding] = []

    native_claims = [c for c in claims if c.kind == "native_dylint"]
    if native_claims and not _has_native_dylint_job(repo_root):
        for c in native_claims:
            findings.append(
                Finding(
                    rule="GEN-010",
                    path=c.path,
                    line=c.line,
                    message=f"doc claims native (non-Linux-host) Windows/macOS Dylint ({c.text!r}), but no "
                    "workflow job runs Dylint with a Windows/macOS runs-on label -- checked offline against "
                    "this repository's own .github/workflows/*",
                    fix="fix whichever is wrong: either add the native cross-OS Dylint job the doc "
                    "describes, or correct the doc to describe what actually runs (e.g. a single-target "
                    "Linux pass, or Linux cross-target check-only lanes per RUST-008)",
                )
            )

    advisory_claims = [c for c in claims if c.kind in ("merge_queue", "branch_protection")]
    for c in advisory_claims:
        findings.append(
            Finding(
                rule="GEN-010",
                status=Status.NEEDS_REVIEW,
                path=c.path,
                line=c.line,
                message=f"doc claims a {c.kind.replace('_', ' ')} ({c.text!r}); this cannot be confirmed "
                "offline -- run 'ci-lint audit' (live) to check it against actual branch protection/rulesets",
                fix="run 'python3 -m ci_lint audit --repo <path>' with a token; it resolves this "
                "needs_review into a clean pass or a violation using the same branch-protection/rulesets "
                "read GEN-006/GEN-011 already perform",
            )
        )

    return Gen010Static(findings=findings, claims=claims)
