"""TEST-005: sanitizer lanes are not PR gates -- nightly or `workflow_dispatch` only.

Policy (docs/policy-general.md, "Sanitizer lanes run nightly, never on the PR
path (TEST-005)"; applies to C++ and Rust apps): any workflow that runs
sanitizers (ASan, TSan, MSan, UBSan, LSan, valgrind) must be triggered by
`schedule` (nightly) and/or `workflow_dispatch`, never by `pull_request` or
`merge_group`.

Incident (Zach, 2026-11): "ASAN was running on every PR and probably the
merge queue; that's overkill and extremely slow, so we moved it to the
nightly. We want this to become policy for C++ and Rust apps." Measured on
obs-rust/obs-studio (zackees/ci.yml#519): the asan job ran on 21 of the last
30 PR runs at 22-35 min each, 40% of the PR runner-minutes, and its failure
failed the required aggregate despite `gating = false`.

Detection is a textual scan (stdlib only, no YAML parser) of every
`.github/workflows/*.yml|yaml`:

- Sanitizer signal, per job: the job's text contains `-fsanitize=`,
  `ASAN_OPTIONS`, `TSAN_OPTIONS`, `UBSAN_OPTIONS`, `MSAN_OPTIONS`,
  `sanitize=address|thread|undefined|memory|leak`, or the job id, its
  `name:`, or a step `id:`/`name:` contains asan/tsan/msan/ubsan/valgrind as a
  standalone word part (`asan`, `build-asan`, `Sanitize / asan`; not
  `casanova`), or (extension, see limits) a quoted `'asan'`/`'tsan'`/`'msan'`/`'ubsan'` literal or a
  `tool:`/`tools:` value naming one. A signal outside any job (workflow-level `env:`) counts for the
  whole workflow.
- Trigger signal, from the same file's top-level `on:` (scalar, flow list,
  flow map, block list or block map; `"on":` too): `pull_request` or
  `merge_group` => VIOLATION, one finding per sanitizer job at its first
  signal line. A job-level `if:` that names only schedule/workflow_dispatch
  events and neither PR event makes that job unreachable from the PR trigger.
- `workflow_call` without its own PR trigger: the PR trigger lives in a caller.
  Callers in the same repository (`uses: ./.github/workflows/<file>`) are
  resolved one level: a caller with a PR trigger, an unresolvable caller, or no
  in-repo caller (external callers possible) => NEEDS_REVIEW; callers that are
  all schedule/dispatch-only => silent.
- Commented lines (`#` at line start, or a trailing ` #...` outside quotes) are
  skipped. A workflow that is schedule/workflow_dispatch-only is silent.

Limits, stated so the rule stays a policy and not a nuisance: it does not
follow `uses:` chains deeper than one caller, does not evaluate `if:`
expressions beyond the event-name check above (a label- or path-gated asan job
on `pull_request` is still a violation: gating by label is still the PR path),
does not look at `pull_request_target`, composite actions, scripts under `ci/`
that run sanitizers, or sanitizer use hidden behind a matrix expression, and a
sanitizer token in a `run:` shell comment is skipped because comments are.
There is no same-line allow comment: move the lane to `schedule`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import discover_workflow_files

RULE = "TEST-005"
POLICY = (
    "sanitizer lanes are not PR gates: schedule (nightly) or workflow_dispatch only, never pull_request or merge_group"
)

PR_EVENTS = frozenset({"pull_request", "merge_group"})
_OFF_PATH_EVENTS = frozenset({"schedule", "workflow_dispatch"})

_TOKEN_RE = re.compile(
    r"-fsanitize=|\b(?:ASAN|TSAN|UBSAN|MSAN)_OPTIONS\b|\bsanitize=(?:address|thread|undefined|memory|leak)\b"
    # Extension beyond the id/name tokens (found on obs-studio's sanitizers.yaml, whose tool is a matrix
    # input and so never appears in a job id or name): a quoted tool literal (`matrix.tool == 'asan'`) or a
    # `tool:`/`tools:` value naming one.
    r"""|['"](?:asan|tsan|msan|ubsan)['"]|\btools?:\s*['"]?(?:asan|tsan|msan|ubsan)\b"""
)
_NAME_RE = re.compile(r"(?<![a-z])(?:asan|tsan|msan|ubsan|valgrind)(?![a-z])", re.IGNORECASE)
_NAME_LINE_RE = re.compile(r"^\s*(?:-\s+)?(?:name|id):\s*(.*)$")
_KEY_RE = re.compile(r"""^(\s*)(?:-\s+)?("[^"]*"|'[^']*'|[\w.\-]+)\s*:(.*)$""")
_USES_LOCAL_RE = re.compile(r"""^\s*(?:-\s+)?uses:\s*["']?\./\.github/workflows/([\w.\-]+)""")
_EVENT_WORD_RE = re.compile(r"[a-z_]+")

FIX = (
    "move the sanitizer lane off the PR path: trigger its workflow only by `schedule` (nightly) and/or "
    "`workflow_dispatch`, and drop `pull_request`/`merge_group` from its `on:`. A PR that needs sanitizer "
    "evidence runs it by hand via workflow_dispatch (docs/policy-general.md, TEST-005)"
)


@dataclass(frozen=True)
class _Span:
    lo: int  # 0-based, inclusive
    hi: int  # exclusive


@dataclass(frozen=True)
class _Signal:
    line: int  # 1-based
    what: str


@dataclass
class _Job:
    job_id: str
    start: int  # 1-based line of the job key
    end: int  # inclusive
    hit_line: int | None = None
    hit_what: str = ""
    off_path_if: bool = False


@dataclass
class _Workflow:
    path: str
    events: set[str] = field(default_factory=set)
    jobs: list[_Job] = field(default_factory=list)
    global_hit: _Signal | None = None
    callees: list[str] = field(default_factory=list)  # local reusable workflow file names


def _strip_comment(raw: str) -> str:
    """Drop a full-line or trailing ` #` comment; keep `#` inside quotes."""

    if raw.lstrip().startswith("#"):
        return ""
    quote = ""
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i].rstrip()
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _events(lines: list[str]) -> set[str]:
    """Event names declared by the top-level `on:` key."""

    for idx, line in enumerate(lines):
        m = re.match(r"""^(?:on|"on"|'on')\s*:(.*)$""", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest:
            return set(_EVENT_WORD_RE.findall(rest.lower()))
        events: set[str] = set()
        base: int | None = None
        for body in lines[idx + 1 :]:
            if not body.strip():
                continue
            if _indent(body) == 0:
                break
            if base is None:
                base = _indent(body)
            if _indent(body) != base:
                continue
            item = body.strip().removeprefix("-").strip()
            km = re.match(r"""["']?([a-z_]+)["']?\s*(?::|$)""", item)
            if km:
                events.add(km.group(1))
        return events
    return set()


def _jobs_block(lines: list[str]) -> _Span | None:
    """Half-open 0-based line range of the body of the top-level `jobs:` key."""

    start = next((i for i, ln in enumerate(lines) if re.match(r"^jobs\s*:\s*$", ln)), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and _indent(lines[i]) == 0:
            end = i
            break
    return _Span(start + 1, end)


def _job_spans(lines: list[str]) -> list[_Job]:
    block = _jobs_block(lines)
    if block is None:
        return []
    lo, hi = block.lo, block.hi
    indents = [_indent(ln) for ln in lines[lo:hi] if ln.strip()]
    if not indents:
        return []
    base = min(indents)
    jobs: list[_Job] = []
    for i in range(lo, hi):
        m = _KEY_RE.match(lines[i]) if lines[i].strip() and _indent(lines[i]) == base else None
        if m:
            if jobs:
                jobs[-1].end = i
            jobs.append(_Job(job_id=m.group(2).strip("\"'"), start=i + 1, end=hi))
    return jobs


def _first_signal(lines: list[str], lo: int, hi: int, job_id: str | None) -> _Signal | None:
    """First (1-based line, what) sanitizer signal in lines[lo:hi] (0-based slice)."""

    if job_id is not None and _NAME_RE.search(job_id):
        return _Signal(lo + 1, f"job id `{job_id}`")
    for i in range(lo, hi):
        line = lines[i]
        tok = _TOKEN_RE.search(line)
        if tok:
            return _Signal(i + 1, f"`{tok.group(0)}`")
        nm = _NAME_LINE_RE.match(line)
        if nm and _NAME_RE.search(nm.group(1)):
            return _Signal(i + 1, f"`{line.strip().removeprefix('- ').strip()}`")
    return None


def _off_path_if(lines: list[str], job: _Job) -> bool:
    """Job-level `if:` that names schedule/workflow_dispatch and no PR event."""

    key_indent = _indent(lines[job.start - 1])
    for i in range(job.start, job.end):
        line = lines[i]
        if line.strip() and _indent(line) == key_indent + 2 and re.match(r"\s*if\s*:", line):
            cond = line.split(":", 1)[1]
            names = set(_EVENT_WORD_RE.findall(cond.lower()))
            return bool(names & _OFF_PATH_EVENTS) and not (names & PR_EVENTS)
    return False


def parse_workflow(text: str, rel_path: str) -> _Workflow:
    lines = [_strip_comment(raw) for raw in text.splitlines()]
    wf = _Workflow(path=rel_path, events=_events(lines))
    wf.jobs = _job_spans(lines)
    first_job_line = wf.jobs[0].start - 1 if wf.jobs else len(lines)
    wf.global_hit = _first_signal(lines, 0, first_job_line, None)
    for job in wf.jobs:
        hit = _first_signal(lines, job.start - 1, job.end, job.job_id)
        if hit:
            job.hit_line, job.hit_what = hit.line, hit.what
        job.off_path_if = _off_path_if(lines, job)
    for line in lines:
        um = _USES_LOCAL_RE.match(line)
        if um:
            wf.callees.append(um.group(1))
    return wf


def _is_sanitizer_workflow(wf: _Workflow) -> bool:
    return wf.global_hit is not None or any(j.hit_line is not None for j in wf.jobs)


def _violation(wf: _Workflow, line: int, what: str, scope: str, pr: list[str]) -> Finding:
    return Finding(
        rule=RULE,
        path=wf.path,
        line=line,
        message=f"{wf.path}:{line}: {scope} runs sanitizers ({what}) but the workflow is triggered by "
        f"{' and '.join(pr)}; {POLICY}",
        fix=FIX,
    )


def _first_hit(wf: _Workflow) -> _Signal:
    if wf.global_hit is not None:
        return wf.global_hit
    job = next(j for j in wf.jobs if j.hit_line is not None)
    assert job.hit_line is not None
    return _Signal(job.hit_line, job.hit_what)


def _reusable_finding(name: str, wf: _Workflow, workflows: dict[str, _Workflow]) -> Finding | None:
    hit = _first_hit(wf)
    callers = [w for w in workflows.values() if name in w.callees]
    pr_callers = [w for w in callers if (w.events & PR_EVENTS) or "workflow_call" in w.events]
    if callers and not pr_callers:
        return None  # every in-repo caller is schedule/workflow_dispatch-only
    if pr_callers:
        who = ", ".join(w.path for w in pr_callers)
        reason = f"in-repo caller(s) with a PR trigger or their own workflow_call: {who}"
    else:
        reason = "no in-repo caller found, so an external caller may pass a PR trigger"
    return Finding(
        rule=RULE,
        path=wf.path,
        line=hit.line,
        status=Status.NEEDS_REVIEW,
        message=f"{wf.path}:{hit.line}: reusable workflow runs sanitizers ({hit.what}); {reason}. {POLICY}",
        fix=FIX + "; for a reusable workflow, make every caller schedule/workflow_dispatch-only",
    )


def _pr_findings(wf: _Workflow, pr: list[str]) -> list[Finding]:
    if wf.global_hit is not None:
        return [_violation(wf, wf.global_hit.line, wf.global_hit.what, "workflow-level configuration", pr)]
    return [
        _violation(wf, job.hit_line, job.hit_what, f"job `{job.job_id}`", pr)
        for job in wf.jobs
        if job.hit_line is not None and not job.off_path_if
    ]


def _unparsed_finding(wf: _Workflow) -> Finding:
    hit = _first_hit(wf)
    return Finding(
        rule=RULE,
        path=wf.path,
        line=hit.line,
        status=Status.NEEDS_REVIEW,
        message=f"{wf.path}:{hit.line}: runs sanitizers ({hit.what}) but its `on:` triggers could not be parsed",
        fix=FIX,
    )


def _load_workflows(repo_root: Path) -> dict[str, _Workflow]:
    workflows: dict[str, _Workflow] = {}
    for path in discover_workflow_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        workflows[path.name] = parse_workflow(text, path.relative_to(repo_root).as_posix())
    return workflows


def check_test_005(repo_root: Path) -> list[Finding]:
    workflows = _load_workflows(repo_root)
    findings: list[Finding] = []
    for name, wf in workflows.items():
        if not _is_sanitizer_workflow(wf):
            continue
        pr = sorted(wf.events & PR_EVENTS)
        if pr:
            findings.extend(_pr_findings(wf, pr))
        elif "workflow_call" in wf.events:
            finding = _reusable_finding(name, wf, workflows)
            if finding is not None:
                findings.append(finding)
        elif not wf.events:
            findings.append(_unparsed_finding(wf))
    return findings
