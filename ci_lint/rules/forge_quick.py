"""RUN-004: a job that can run on the `forge-quick` runner class is small,
pinned, container-free and behind the kill switch.

`forge-quick` is a dense pool of ephemeral self-hosted Linux runners for
short administrative jobs (pool-select routing, lease calls,
cancel-superseded, labelers, status probes), so they never wait in an
organization's hosted-runner queue. The image (forge-ci/soldrix
`image/quick/`) enforces a wall-clock budget, a memory budget and a
pre-seeded action cache at run time; this rule is the static half.

A job "can resolve to forge-quick" when its `runs-on` names the label
`forge-quick` (literal, list, `labels:` of a group mapping, or a matrix value
it reads) or reads `vars.FORGE_QUICK_LABELS`. Such a job is a violation
unless all of these hold:

1. `timeout-minutes` is a literal number <= 2;
2. every step `uses:` is in the shared allowlist at exactly its pinned sha
   (`ci_lint/data/forge_quick_allowlist.toml`, a byte-identical copy of
   soldrix `image/quick/allowlist.toml`, which seeds the runner's action
   cache from the same list);
3. no `container:` and no `services:`;
4. `runs-on` goes through the kill switch: one expression that tests
   `vars.FORGE_QUICK_LABELS != ''`, selects `fromJSON(vars.FORGE_QUICK_LABELS)`
   and falls back to a GitHub-hosted Ubuntu label, e.g.
   `${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'ubuntu-latest' }}`.
   Unsetting the variable sends every such job back to hosted runners.

A job whose `runs-on` reads another job's outputs (`needs.<j>.outputs.*`)
while that job computes them from the forge-quick label or variable is
`needs_review`: the selected labels are not statically known, so the kill
switch cannot be verified.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import as_dict, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "RUN-004"
LABEL = "forge-quick"
VAR = "FORGE_QUICK_LABELS"
MAX_TIMEOUT_MINUTES = 2
ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "forge_quick_allowlist.toml"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NEEDS_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.outputs")
_HOSTED_FALLBACK_RE = re.compile(r"\|\|\s*'(ubuntu-[A-Za-z0-9.-]+)'\s*\}\}\s*$")
_VAR_SET_RE = re.compile(r"vars\.FORGE_QUICK_LABELS\s*!=\s*''", re.IGNORECASE)
_VAR_FROMJSON_RE = re.compile(r"fromJSON\(\s*vars\.FORGE_QUICK_LABELS\s*\)", re.IGNORECASE)
_KILL_SWITCH_EXAMPLE = "${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'ubuntu-latest' }}"


@dataclass(frozen=True)
class AllowedAction:
    repo: str
    path: str
    sha: str
    version: str

    @property
    def uses(self) -> str:
        sub = f"/{self.path}" if self.path else ""
        return f"{self.repo}{sub}@{self.sha}"


@dataclass(frozen=True)
class Allowlist:
    actions: tuple[AllowedAction, ...]

    def allows(self, uses: str) -> bool:
        name, _, ref = uses.strip().partition("@")
        return any(name.lower() == a.uses.partition("@")[0].lower() and ref == a.sha for a in self.actions)

    def pinned_for(self, uses: str) -> str | None:
        """The pin to suggest: the last entry for this action (newer pins are appended)."""
        name = uses.strip().partition("@")[0].lower()
        for action in reversed(self.actions):
            if action.uses.partition("@")[0].lower() == name:
                return action.uses
        return None


def parse_allowlist(text: str) -> Allowlist:
    """Parse allowlist TOML (schema 1); raise ValueError on any malformed entry."""

    doc = tomllib.loads(text)
    if doc.get("schema") != 1:
        raise ValueError("forge-quick allowlist: schema must be 1")
    entries = doc.get("action")
    if not isinstance(entries, list) or not entries:
        raise ValueError("forge-quick allowlist: no [[action]] entries")
    actions: list[AllowedAction] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("forge-quick allowlist: [[action]] must be a table")
        action = AllowedAction(
            repo=str(raw.get("repo", "")),
            path=str(raw.get("path", "")).strip("/"),
            sha=str(raw.get("sha", "")),
            version=str(raw.get("version", "")),
        )
        if action.repo.count("/") != 1 or not _SHA_RE.match(action.sha):
            raise ValueError(f"forge-quick allowlist: bad entry {action.repo!r} @ {action.sha!r}")
        actions.append(action)
    return Allowlist(tuple(actions))


@cache
def load_allowlist(path: Path = ALLOWLIST_PATH) -> Allowlist:
    return parse_allowlist(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Reach:
    direct: bool
    why: str
    via_needs: tuple[str, ...]


def _flat(value: YamlValue) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _names_quick(text: str) -> bool:
    low = text.lower()
    return LABEL in low or VAR.lower() in low


def _reach(job: dict[str, YamlValue], quick_producers: frozenset[str]) -> Reach:
    runs_on = job.get("runs-on")
    if runs_on is None:
        return Reach(False, "", ())
    text = _flat(runs_on)
    if VAR.lower() in text.lower():
        return Reach(True, f"runs-on reads vars.{VAR}", ())
    if LABEL in text.lower():
        return Reach(True, f"runs-on names '{LABEL}'", ())
    if "matrix." in text and _names_quick(_flat(as_dict(job.get("strategy")).get("matrix"))):
        return Reach(True, f"runs-on reads a matrix value that names '{LABEL}'", ())
    return Reach(False, "", tuple(dep for dep in _NEEDS_RE.findall(text) if dep in quick_producers))


def _quick_producers(doc: YamlValue) -> frozenset[str]:
    """Jobs whose body, apart from their own runs-on, names the forge-quick
    label or variable: their outputs may carry forge-quick labels."""

    out: set[str] = set()
    for job_id, job in jobs_of(as_dict(doc)).items():
        if _names_quick(_flat({k: v for k, v in job.items() if k != "runs-on"})):
            out.add(job_id)
    return frozenset(out)


def _kill_switch_ok(runs_on: YamlValue) -> bool:
    if not isinstance(runs_on, str):
        return False
    expr = runs_on.strip()
    if not (expr.startswith("${{") and expr.endswith("}}")):
        return False
    if not (_VAR_SET_RE.search(expr) and _VAR_FROMJSON_RE.search(expr) and _HOSTED_FALLBACK_RE.search(expr)):
        return False
    # No hard-wired forge-quick or self-hosted label beside the variable:
    # unsetting it must leave only the hosted fallback. Other literals (event
    # names in a trusted-event guard, '') are fine.
    literals = [lit.lower() for lit in re.findall(r"'([^']*)'", expr)]
    return not any(LABEL in lit or lit == "self-hosted" for lit in literals)


def _timeout_ok(value: YamlValue) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= MAX_TIMEOUT_MINUTES


def _job_line(text: str, job_id: str) -> int | None:
    match = re.search(rf"^\s+{re.escape(job_id)}\s*:", text, re.MULTILINE)
    return text.count("\n", 0, match.start()) + 1 if match else None


@dataclass(frozen=True)
class JobContext:
    path: str
    job_id: str
    line: int | None
    why: str


def _finding(ctx: JobContext, problem: str, fix: str) -> Finding:
    return Finding(
        rule=RULE,
        path=ctx.path,
        line=ctx.line,
        message=f"job '{ctx.job_id}' can run on forge-quick ({ctx.why}) but {problem}",
        fix=fix,
    )


def _uses_findings(ctx: JobContext, job: dict[str, YamlValue], allowlist: Allowlist) -> list[Finding]:
    findings: list[Finding] = []
    for step in steps_of(job):
        uses = step.get("uses")
        if not isinstance(uses, str) or allowlist.allows(uses):
            continue
        pinned = allowlist.pinned_for(uses)
        if pinned is not None:
            fix = f"pin it to the allowlisted sha: `uses: {pinned}`"
        else:
            fix = (
                "use only allowlisted actions on forge-quick ("
                + ", ".join(a.uses.partition("@")[0] for a in allowlist.actions)
                + "); anything else runs on a hosted or forge-pool runner"
            )
        findings.append(_finding(ctx, f"step `uses: {uses.strip()}` is not in the forge-quick allowlist at its pinned sha", fix))
    return findings


def _job_findings(ctx: JobContext, job: dict[str, YamlValue], allowlist: Allowlist) -> list[Finding]:
    findings: list[Finding] = []
    if not _timeout_ok(job.get("timeout-minutes")):
        findings.append(
            _finding(
                ctx,
                f"`timeout-minutes` is {job.get('timeout-minutes')!r}, not a number <= {MAX_TIMEOUT_MINUTES}",
                f"set `timeout-minutes: {MAX_TIMEOUT_MINUTES}` (or less); longer work belongs on a hosted or forge-pool runner",
            )
        )
    findings.extend(_uses_findings(ctx, job, allowlist))
    for key in ("container", "services"):
        if key in job:
            findings.append(
                _finding(
                    ctx,
                    f"it declares `{key}:`",
                    f"drop `{key}:`; forge-quick runners have no docker, so container work runs elsewhere",
                )
            )
    if not _kill_switch_ok(job.get("runs-on")):
        findings.append(
            _finding(
                ctx,
                "its `runs-on` does not go through the kill switch",
                f"use `runs-on: {_KILL_SWITCH_EXAMPLE}`: an unset vars.{VAR} sends the job to hosted runners",
            )
        )
    return findings


def check_run_004(repo_root: Path, allowlist: Allowlist | None = None) -> list[Finding]:
    allowed = load_allowlist() if allowlist is None else allowlist
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        try:
            text = (repo_root / wf.path).read_text(encoding="utf-8")
        except OSError:
            text = ""
        producers = _quick_producers(wf.document)
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            reach = _reach(job, producers)
            ctx = JobContext(wf.path, job_id, _job_line(text, job_id), reach.why)
            if reach.direct:
                findings.extend(_job_findings(ctx, job, allowed))
            elif reach.via_needs:
                findings.append(
                    Finding(
                        rule=RULE,
                        path=wf.path,
                        line=ctx.line,
                        status=Status.NEEDS_REVIEW,
                        message=f"job '{job_id}' takes runs-on from needs.{'/'.join(reach.via_needs)} outputs that are "
                        f"computed from the forge-quick label or vars.{VAR}; RUN-004 cannot verify its limits statically",
                        fix=f"if the job may land on forge-quick, give it `runs-on: {_KILL_SWITCH_EXAMPLE}` and the "
                        "forge-quick limits; otherwise compute its labels from a non-forge-quick variable",
                    )
                )
    return findings
