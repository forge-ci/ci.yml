"""GEN-021 (zackees/ci.yml#156, static half): a default-branch push may skip
a gate-required job only through verified reuse (`ci-lint reuse-check`,
ci_lint.default_branch_reuse), never by a blanket job-level `if:` that
excludes default-branch pushes. Design: docs/designs/default-branch-
verified-reuse.md.

Scope: a workflow that triggers on both `pull_request` and a `push` that
reaches the default branch. Per job (the gate job itself excluded):

  - Blanket skip. A job-level `if:` with a conjunct that runs the job on
    pull requests but never on a default-branch push -- `github.event_name
    != 'push'`, `github.event_name == 'pull_request'`, `github.ref !=
    'refs/heads/<default>'`, `github.ref_name != '<default>'`,
    `startsWith(github.ref, 'refs/pull/')` -- is a VIOLATION when the job is
    in the gate job's `needs:` (the gate is the job with id `ci-ok`/`gate`
    or display name `CI OK`) and the workflow has no reuse decision at all;
    `needs_review` when the workflow does run `reuse-check` elsewhere (this
    job just doesn't consume it) or has no recognizable gate job. A job the
    gate does not need is not flagged. A same-(rule, path) `[[exceptions]]`
    entry is the documented exception, applied by precheck as usual.
  - Unresolvable. An `if:` that references `github.event_name`/
    `github.ref`/`github.ref_name` through `||`, `!(...)`, parentheses, or
    any comparison shape not listed above is `needs_review` on a
    gate-required job -- never a pass.
  - Reuse consumption. A job-level `if:` reading `needs.<X>.outputs.reuse`
    must read it as `!= 'true'` (an empty/absent output -- every pull
    request, every failed decision -- then runs the job), and job `<X>` must
    run `ci-lint reuse-check` (directly, or in the local reusable workflow
    it calls); otherwise `needs_review`.
  - Proof coverage. Every literal `--required-job` value passed to
    `reuse-check` must plausibly name a job of this workflow (its display
    name, `<name> / <callee job>` for a reusable-workflow call, or `<name>
    (<matrix values>)` for a matrix leg), and every job skipped on reuse
    must be covered by at least one of them; otherwise `needs_review`
    (matrix/reusable display names cannot be fully resolved statically; the
    runtime command fails closed on an exact-name mismatch regardless).

This module is static and network-free; the runtime proof itself is
`ci-lint reuse-check`.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import ParsedYamlFile, as_dict, as_list, get_on_section, jobs_of, load, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "GEN-021"
GATE_JOB_IDS: frozenset[str] = frozenset({"ci-ok", "ci_ok", "gate"})
GATE_JOB_NAME = "ci ok"

_REUSE_CHECK_RE = re.compile(r"\bci[-_]lint\b[^\n]*?\breuse-check\b")
_CONSUME_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.outputs\.reuse\b")
_GOOD_CONSUME_RE = re.compile(r"^needs\.[A-Za-z0-9_-]+\.outputs\.reuse\s*!=\s*(?:'true'|\"true\"|true)$")
_EVENT_REF_RE = re.compile(r"\bgithub\.(?:event_name|ref_name|ref)\b")

_Q = r"""['"]"""


def _default_branch_patterns(default_branch: str) -> list[re.Pattern[str]]:
    b = re.escape(default_branch)
    return [
        re.compile(rf"^github\.event_name\s*!=\s*{_Q}push{_Q}$"),
        re.compile(rf"^github\.event_name\s*==\s*{_Q}pull_request{_Q}$"),
        re.compile(rf"^{_Q}pull_request{_Q}\s*==\s*github\.event_name$"),
        re.compile(rf"^github\.ref\s*!=\s*{_Q}refs/heads/{b}{_Q}$"),
        re.compile(rf"^github\.ref_name\s*!=\s*{_Q}{b}{_Q}$"),
        re.compile(rf"^startswith\(\s*github\.ref\s*,\s*{_Q}refs/pull/{_Q}\s*\)$"),
        re.compile(rf"^!\s*startswith\(\s*github\.ref\s*,\s*{_Q}refs/heads/{b}{_Q}\s*\)$"),
    ]


def _never_pr_patterns(default_branch: str) -> list[re.Pattern[str]]:
    """A conjunct that is false on every pull_request event: a job guarded by
    it cannot run on a PR at all (a main-only/dispatch-only/release job), so
    it is not a GEN-021 subject."""

    b = re.escape(default_branch)
    return [
        re.compile(rf"^github\.event_name\s*==\s*{_Q}(?!pull_request(?:_target)?{_Q})[a-z_]+{_Q}$"),
        re.compile(rf"^github\.ref\s*==\s*{_Q}refs/heads/{b}{_Q}$"),
        re.compile(rf"^github\.ref_name\s*==\s*{_Q}{b}{_Q}$"),
        re.compile(rf"^startswith\(\s*github\.ref\s*,\s*{_Q}refs/(?:tags|heads)/{_Q}\s*\)$"),
    ]


def _benign_patterns(default_branch: str) -> list[re.Pattern[str]]:
    """Conjuncts that mention the event/ref but hold on both pull requests
    and default-branch pushes (e.g. `github.event_name != 'schedule'`)."""

    b = re.escape(default_branch)
    return [
        re.compile(rf"^github\.event_name\s*!=\s*{_Q}(?!push{_Q})[a-z_]+{_Q}$"),
        re.compile(r"^github\.ref\s*!=\s*" + _Q + r"refs/heads/(?!" + b + _Q + r")[^'\"]+" + _Q + "$"),
        re.compile(r"^github\.ref_name\s*!=\s*" + _Q + r"(?!" + b + _Q + r")[^'\"]+" + _Q + "$"),
    ]


def _strip_expr(cond: str) -> str:
    text = cond.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return " ".join(text.replace("${{", " ").replace("}}", " ").split())


def _split_top(text: str, op: str) -> list[str]:
    """Split on `op` at parenthesis depth 0, outside quotes."""

    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if quote is not None:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and text.startswith(op, i):
            parts.append(text[start:i].strip())
            i += len(op)
            start = i
            continue
        i += 1
    parts.append(text[start:].strip())
    return parts


def _unwrap(text: str) -> str:
    while text.startswith("(") and text.endswith(")"):
        inner = text[1:-1]
        depth = 0
        for c in inner:
            depth += 1 if c == "(" else -1 if c == ")" else 0
            if depth < 0:
                return text
        text = inner.strip()
    return text


def _classify_chain(chain: str, default_branch: str) -> str:
    """'never-pr' | 'skip' | 'unknown' | 'clean' for one `&&` chain."""

    has_skip = has_unknown = False
    for raw in _split_top(chain, "&&"):
        conjunct = _unwrap(raw)
        if not _EVENT_REF_RE.search(conjunct) or _CONSUME_RE.search(conjunct):
            continue
        if any(r.match(conjunct) for r in _never_pr_patterns(default_branch)):
            return "never-pr"
        if any(r.match(conjunct) for r in _default_branch_patterns(default_branch)):
            has_skip = True
        elif not any(r.match(conjunct) for r in _benign_patterns(default_branch)):
            has_unknown = True
    if has_skip:
        return "skip"
    return "unknown" if has_unknown else "clean"


def classify_condition(cond: str, default_branch: str = "main") -> str:
    """'skip' (the job runs on pull requests but a conjunct excludes every
    default-branch push), 'unknown' (event/ref logic this parser cannot
    resolve), or 'clean' (no event/ref gate, a gate that holds on both, or a
    job that never runs on a pull request at all). Reuse-consumption
    conjuncts are ignored here."""

    text = _strip_expr(cond).replace("startsWith(", "startswith(")
    if not _EVENT_REF_RE.search(text):
        return "clean"
    disjuncts = _split_top(_unwrap(text), "||")
    if len(disjuncts) > 1:
        if all(_classify_chain(_unwrap(d), default_branch) == "never-pr" for d in disjuncts):
            return "clean"
        return "unknown"
    verdict = _classify_chain(disjuncts[0], default_branch)
    return "clean" if verdict == "never-pr" else verdict


def _push_reaches(on: dict[str, YamlValue], default_branch: str) -> bool:
    if "push" not in on:
        return False
    push = on.get("push")
    if push is None:
        return True
    if not isinstance(push, dict):
        return False
    branches = push.get("branches")
    ignored = push.get("branches-ignore")
    if isinstance(ignored, list) and any(isinstance(p, str) and fnmatch.fnmatch(default_branch, p) for p in ignored):
        return False
    if branches is None:
        # tags-only push filters never reach the default branch
        return not (("tags" in push or "tags-ignore" in push) and "branches-ignore" not in push)
    return isinstance(branches, list) and any(
        isinstance(p, str) and fnmatch.fnmatch(default_branch, p) for p in branches
    )


def _reuse_check_lines(job: dict[str, YamlValue]) -> list[str]:
    lines: list[str] = []
    for step in steps_of(job):
        run = step.get("run")
        if isinstance(run, str) and _REUSE_CHECK_RE.search(run):
            lines.append(run)
    return lines


def _local_callee(repo_root: Path, job: dict[str, YamlValue]) -> ParsedYamlFile | None:
    uses = job.get("uses")
    if not isinstance(uses, str) or not uses.startswith("./"):
        return None
    path = (repo_root / uses[2:].split("@", 1)[0]).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    parsed = load(repo_root.resolve(), path)
    return parsed if parsed.status == LoadStatus.OK else None


@dataclass(frozen=True)
class _ReuseRuns:
    """Ids of jobs that make a reuse decision, and every reuse-check `run:`
    line in this workflow or its local reusable-workflow callees."""

    jobs: set[str]
    lines: list[str]


@dataclass(frozen=True)
class _Gate:
    """The gate job's id and its `needs:` set (both None: no gate job)."""

    job_id: str | None
    needs: set[str] | None


def _reuse_check_runs(repo_root: Path, doc: dict[str, YamlValue]) -> _ReuseRuns:

    reuse_jobs: set[str] = set()
    lines: list[str] = []
    for job_id, job in jobs_of(doc).items():
        own = _reuse_check_lines(job)
        callee = _local_callee(repo_root, job)
        if callee is not None:
            for sub in jobs_of(as_dict(callee.document)).values():
                own.extend(_reuse_check_lines(sub))
        if own:
            reuse_jobs.add(job_id)
            lines.extend(own)
    return _ReuseRuns(reuse_jobs, lines)


def _required_job_args(line: str) -> list[str] | None:
    """Literal `--required-job` values, or None when the line cannot be
    tokenized (unbalanced quotes)."""

    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    values: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == "--required-job" and i + 1 < len(tokens):
            values.append(tokens[i + 1])
        elif tok.startswith("--required-job="):
            values.append(tok.split("=", 1)[1])
    return values


def _display_base(job_id: str, job: dict[str, YamlValue]) -> str:
    name = job.get("name")
    return name if isinstance(name, str) and name.strip() else job_id


def _covers(required: str, base: str) -> bool:
    return required == base or required.startswith(f"{base} / ") or required.startswith(f"{base} (")


def _gate(doc: dict[str, YamlValue]) -> _Gate:
    for job_id, job in jobs_of(doc).items():
        name = job.get("name")
        if job_id in GATE_JOB_IDS or (isinstance(name, str) and name.strip().lower() == GATE_JOB_NAME):
            needs = job.get("needs")
            if isinstance(needs, str):
                return _Gate(job_id, {needs})
            return _Gate(job_id, {n for n in as_list(needs) if isinstance(n, str)})
    return _Gate(None, None)


def check_gen_021(repo_root: Path, default_branch: str = "main") -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        on = get_on_section(doc)
        if "pull_request" not in on or not _push_reaches(on, default_branch):
            continue
        jobs = jobs_of(doc)
        gate = _gate(doc)
        gate_id, required_set = gate.job_id, gate.needs
        reuse = _reuse_check_runs(repo_root, doc)
        reuse_jobs, reuse_lines = reuse.jobs, reuse.lines
        has_decision = bool(reuse_jobs)
        bases = {job_id: _display_base(job_id, job) for job_id, job in jobs.items()}
        skipped_on_reuse: list[str] = []

        for job_id, job in jobs.items():
            if job_id == gate_id:
                continue
            cond = job.get("if")
            if not isinstance(cond, str):
                continue
            text = _strip_expr(cond)
            consumed = _CONSUME_RE.findall(text)
            if consumed:
                skipped_on_reuse.append(job_id)
                findings.extend(_consumption_findings(wf.path, job_id, text, consumed, reuse_jobs))
            gate_required = required_set is None or job_id in required_set
            if not gate_required:
                continue
            verdict = classify_condition(cond, default_branch)
            if verdict == "skip":
                blanket = not has_decision and required_set is not None
                findings.append(
                    Finding(
                        rule=RULE,
                        path=wf.path,
                        status=Status.VIOLATION if blanket else Status.NEEDS_REVIEW,
                        message=(
                            f"jobs.{job_id} (`if: {text}`) runs on pull requests but is skipped on every "
                            f"default-branch push, "
                            + (
                                f"and the gate job '{gate_id}' needs it"
                                if required_set is not None
                                else "and no gate job (id ci-ok/gate, or name 'CI OK') shows whether it is required"
                            )
                            + (
                                "; this workflow has no verified-reuse decision (ci-lint reuse-check)"
                                if not has_decision
                                else "; a reuse decision exists in this workflow but this job's skip does not consume it"
                            )
                        ),
                        fix=(
                            "run this job on default-branch pushes, or skip it only through verified reuse: "
                            "`if: needs.<decision job>.outputs.reuse != 'true'` fed by `ci-lint reuse-check` "
                            "(docs/designs/default-branch-verified-reuse.md, reference wiring); a merge queue "
                            "that validates the exact merge commit is the only other justification (GEN-010 "
                            "live half), recorded as a [[exceptions]] entry for GEN-021"
                        ),
                    )
                )
            elif verdict == "unknown":
                findings.append(
                    Finding(
                        rule=RULE,
                        path=wf.path,
                        status=Status.NEEDS_REVIEW,
                        message=(
                            f"jobs.{job_id} has an `if:` over github.event_name/github.ref that cannot be "
                            f"resolved statically (`{text}`) -- confirm it does not skip this required job "
                            "on default-branch pushes"
                        ),
                        fix=(
                            "rewrite the condition as simple `&&` conjuncts, or skip only through "
                            "`needs.<decision job>.outputs.reuse != 'true'` (ci-lint reuse-check)"
                        ),
                    )
                )

        findings.extend(_coverage_findings(wf.path, reuse_lines, bases, skipped_on_reuse))
    return findings


def _consumption_findings(
    path: str, job_id: str, text: str, consumed: list[str], reuse_jobs: set[str]
) -> list[Finding]:
    out: list[Finding] = []
    for producer in sorted(set(consumed)):
        if producer not in reuse_jobs:
            out.append(
                Finding(
                    rule=RULE,
                    path=path,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"jobs.{job_id} skips on needs.{producer}.outputs.reuse, but job '{producer}' does not "
                        "run `ci-lint reuse-check` (directly or in the local reusable workflow it calls) -- "
                        "an unverified reuse flag"
                    ),
                    fix=(
                        f"produce the output in job '{producer}' with `ci-lint reuse-check --github-output` "
                        "(GET-only, fail-closed), not a hand-written flag"
                    ),
                )
            )
    for raw in _split_top(_unwrap(text), "&&"):
        conjunct = _unwrap(raw)
        if _CONSUME_RE.search(conjunct) and not _GOOD_CONSUME_RE.match(conjunct):
            out.append(
                Finding(
                    rule=RULE,
                    path=path,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"jobs.{job_id} reads the reuse output as `{conjunct}` -- only `!= 'true'` keeps the "
                        "job running when the output is empty (pull requests, a failed or skipped decision)"
                    ),
                    fix="use `needs.<decision job>.outputs.reuse != 'true'` as its own `&&` conjunct",
                )
            )
    return out


def _coverage_findings(  # noqa: C901
    path: str, reuse_lines: list[str], bases: dict[str, str], skipped_on_reuse: list[str]
) -> list[Finding]:
    out: list[Finding] = []
    literals: list[str] = []
    unresolved = False
    for line in reuse_lines:
        values = _required_job_args(line)
        if values is None:
            unresolved = True
            continue
        if not values:
            out.append(
                Finding(
                    rule=RULE,
                    path=path,
                    status=Status.NEEDS_REVIEW,
                    message="a `ci-lint reuse-check` invocation passes no --required-job (it will exit 2)",
                    fix="list every job the default-branch run would skip on reuse with --required-job '<exact name>'",
                )
            )
            continue
        for value in values:
            if "${{" in value:
                unresolved = True
            else:
                literals.append(value)
    for value in literals:
        if not any("${{" in b or _covers(value, b) for b in bases.values()):
            out.append(
                Finding(
                    rule=RULE,
                    path=path,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"reuse-check --required-job '{value}' matches no job display name in this workflow "
                        "(renamed job?) -- reuse-check will fail closed on it (required-job-missing)"
                    ),
                    fix="use the exact job display name from a run's jobs list (`<name> / <callee job>` for a "
                    "reusable-workflow call, `<name> (<matrix values>)` for a matrix leg)",
                )
            )
    if unresolved:
        return out
    for job_id in skipped_on_reuse:
        base = bases.get(job_id, job_id)
        if "${{" in base or any(_covers(v, base) for v in literals):
            continue
        out.append(
            Finding(
                rule=RULE,
                path=path,
                status=Status.NEEDS_REVIEW,
                message=(
                    f"jobs.{job_id} ('{base}') is skipped on reuse but no reuse-check --required-job proves it -- "
                    "a reused run would skip it without per-job proof"
                ),
                fix=f"add --required-job '<exact display name of {base}>' (every matrix leg / callee job) to the "
                "reuse-check invocation",
            )
        )
    return out
