"""GEN-026 (zackees/ci.yml#124, docs/policy-general.md "Status badges and
per-target sweeps run on the default branch"): the statically checkable
clauses.

- Clause 1, badges: every GitHub Actions workflow badge URL
  (``https://github.com/<o>/<r>/actions/workflows/<file>/badge.svg``) in
  README.md, CONTRIBUTING.md or ``docs/**/*.md`` must carry
  ``?branch=<default>``. A badge with no ``branch=`` is a violation
  whatever the default branch is. A ``branch=`` naming another ref is a
  violation when the default branch is known, ``needs_review`` when it is
  not. ``event=push`` on a badge for a workflow in this repository whose
  ``on:`` has no ``push`` trigger (dispatch/``workflow_call``/schedule
  only) is a violation: it freezes the badge on the last push-era run.
- Clause 2, the scheduled sweep that keeps badges current runs on the
  default branch: a ``schedule``-triggered workflow whose
  ``actions/checkout`` step pins a literal ``ref:`` other than the default
  branch is a violation (``needs_review`` when the default is unknown).
  Expression refs (``${{ ... }}``) are not judged.

The default branch comes from a caller (``ci-lint audit`` passes the one it
read live), else from the checkout's ``refs/remotes/origin/HEAD``; with
neither, branch comparisons are ``needs_review``. ci.toml has no
default-branch field. Clauses 2 (path-selected dispatch), 3, 4 and 6 need
run history or prose judgment and stay candidate; clause 5 is CACHE-003.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import as_dict, get_on_section, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus

RULE = "GEN-026"

BADGE_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/actions/workflows/"
    r"(?P<file>[^/\s?)\"'\]]+)/badge\.svg(?:\?(?P<query>[^\s)\"'\]>]*))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Badge:
    path: str
    line: int
    workflow: str
    branch: str | None
    event: str | None
    url: str


def _doc_files(repo_root: Path) -> list[Path]:
    out = [repo_root / n for n in ("README.md", "CONTRIBUTING.md") if (repo_root / n).is_file()]
    docs = repo_root / "docs"
    if docs.is_dir():
        out.extend(sorted(p for p in docs.rglob("*.md") if p.is_file()))
    return out


def scan_badges(rel_path: str, text: str) -> list[Badge]:
    badges: list[Badge] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in BADGE_RE.finditer(line):
            query = parse_qs((m.group("query") or "").replace("&amp;", "&"))
            branch = query.get("branch", [None])[0]
            event = query.get("event", [None])[0]
            badges.append(Badge(rel_path, lineno, m.group("file"), branch, event, m.group(0)))
    return badges


def _git_dir(repo_root: Path) -> Path | None:
    dot = repo_root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            text = dot.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            gd = Path(text[len("gitdir:"):].strip())
            gd = gd if gd.is_absolute() else (repo_root / gd)
            common = gd / "commondir"
            if common.is_file():
                try:
                    c = Path(common.read_text(encoding="utf-8").strip())
                except OSError:
                    return gd
                return c if c.is_absolute() else (gd / c)
            return gd
    return None


def resolve_default_branch(repo_root: Path) -> str | None:
    """The checkout's ``origin/HEAD`` symbolic ref, read from disk; None when
    absent (no remote HEAD recorded) -- never guessed."""

    gd = _git_dir(repo_root)
    if gd is None:
        return None
    head = gd / "refs" / "remotes" / "origin" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/remotes/origin/"
    return (text[len(prefix):] or None) if text.startswith(prefix) else None


def _branch_finding(path: str, line: int, what: str, ref: str, default: str | None, fix: str) -> Finding | None:
    if default is not None and ref == default:
        return None
    if default is None:
        return Finding(
            rule=RULE, status=Status.NEEDS_REVIEW, path=path, line=line,
            message=f"{what} is pinned to {ref!r}; the default branch is unknown offline (no "
            "refs/remotes/origin/HEAD), so it cannot be confirmed as the default ref",
            fix=f"{fix}; or run 'git remote set-head origin --auto' / 'ci-lint audit' to resolve the default branch",
        )
    return Finding(
        rule=RULE, path=path, line=line,
        message=f"{what} is pinned to {ref!r}, not the default branch {default!r}",
        fix=fix,
    )


@dataclass(frozen=True)
class WorkflowTriggers:
    name: str  # workflow file basename, as a badge URL names it
    events: frozenset[str]


def _local_triggers(repo_root: Path) -> tuple[WorkflowTriggers, ...]:
    return tuple(
        WorkflowTriggers(Path(wf.path).name, frozenset(str(k) for k in get_on_section(as_dict(wf.document))))
        for wf in load_workflows(repo_root)
        if wf.status == LoadStatus.OK
    )


def _check_badges(repo_root: Path, default: str | None) -> list[Finding]:
    findings: list[Finding] = []
    triggers = _local_triggers(repo_root)
    for doc in _doc_files(repo_root):
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = doc.relative_to(repo_root).as_posix()
        for b in scan_badges(rel, text):
            want = default or "<default>"
            if b.branch is None:
                findings.append(Finding(
                    rule=RULE, path=b.path, line=b.line,
                    message=f"workflow badge for {b.workflow!r} has no branch= filter ({b.url}); GitHub shows "
                    "whatever run it picks, including branch dispatches and cancelled runs (render as failing)",
                    fix=f"append ?branch={want} to the badge URL",
                ))
            else:
                f = _branch_finding(b.path, b.line, f"workflow badge for {b.workflow!r}", b.branch, default,
                                    f"set the badge URL's branch= to {want}")
                if f is not None:
                    findings.append(f)
            trig = next((t.events for t in triggers if t.name == b.workflow), None)
            if b.event == "push" and trig is not None and "push" not in trig:
                findings.append(Finding(
                    rule=RULE, path=b.path, line=b.line,
                    message=f"workflow badge for {b.workflow!r} sets event=push, but that workflow has no push "
                    f"trigger ({', '.join(sorted(trig)) or 'none'}); the badge freezes on the last push-era run",
                    fix="drop event=push from the badge URL and keep only ?branch=<default>",
                ))
    return findings


def _check_sweeps(repo_root: Path, default: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        if "schedule" not in get_on_section(doc):
            continue
        for job_id, job in jobs_of(doc).items():
            for step in steps_of(job):
                uses = step.get("uses")
                if not isinstance(uses, str) or not uses.startswith("actions/checkout@"):
                    continue
                ref = as_dict(step.get("with")).get("ref")
                if not isinstance(ref, str) or not ref.strip() or "${{" in ref:
                    continue
                ref = ref.strip().removeprefix("refs/heads/")
                f = _branch_finding(
                    wf.path, None, f"scheduled sweep job {job_id!r} checks out a ref that", ref, default,
                    "let the scheduled sweep check out the default branch (drop the literal ref:)",
                )
                if f is not None:
                    findings.append(f)
    return findings


def check_gen_026(repo_root: Path, default_branch: str | None = None) -> list[Finding]:
    default = default_branch or resolve_default_branch(repo_root)
    return [*_check_badges(repo_root, default), *_check_sweeps(repo_root, default)]
