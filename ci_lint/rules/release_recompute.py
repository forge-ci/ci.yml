"""REL-007: a release recomputes its validation; it consults no earlier evidence.

Owner directive 2026-10-26, piloted in talos-hq/clud#2202. PRs may skip a
job on a valid local-gate attestation (GATE-008/010) and a default-branch
push may reuse a byte-identical merge's proof (GEN-021). A release does
neither: everything is recalculated inside the release run, with no
consideration for attestation. clud's release looked up an earlier,
manually dispatched full-CI run by SHA and failed every tag pushed without
that manual step (talos-hq/clud run 40397465277).

The static check scans every workflow a tag push or `release` event starts,
plus the local reusable workflows it calls, and flags:

- a step that can run there and invokes `local-gate verify`, `reuse-check`
  or `attest resolve`. A step whose `if:` (or whose job's `if:`) requires a
  PR, merge-group or schedule event, or a branch ref, cannot run from a tag
  push or a release and is skipped. Any other condition is assumed reachable.
- a step that runs a repository script (`python -m ci.x`, `python ci/x.py`)
  which lists earlier workflow runs (`gh run list`, `/actions/runs`) and
  reads their `conclusion`: that is earlier-run proof. Downloading a run's
  artifacts by id (REL-004/REL-005) reads no conclusion and stays clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.workflow_scan import ParsedYamlFile, as_dict, get_on_section, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "REL-007"
POLICY = "docs/policy-general.md#a-release-recomputes-its-validation-rel-007"

# The command, not a file named after it: `ci-lint gate --default-branch-reuse
# reuse-check.json` reads a document that only a default-branch push fills.
_EVIDENCE = re.compile(r"\bci[-_]lint\s+(local-gate\s+verify|reuse-check|attest\s+resolve)\b")
_RUN_LISTING = re.compile(
    r"gh\s+run\s+list\b|[\"']gh[\"'],\s*[\"']run[\"'],\s*[\"']list[\"']"
    r"|/actions/runs\b|/actions/workflows/[^\s\"']*/runs\b"
)
_CONCLUSION = re.compile(r"\bconclusion\b")
_SCRIPT = re.compile(r"\bpython[0-9.]*\s+(?:-m\s+([A-Za-z_][\w.]*)|([\w./-]+\.py))")
# A disjunct holding one of these cannot be true for a tag push or a release.
_UNREACHABLE = re.compile(
    r"github\.event_name\s*==\s*'(?:pull_request|pull_request_target|merge_group|schedule)'"
    r"|github\.ref\s*==\s*'refs/heads/"
)


@dataclass(frozen=True)
class _Step:
    workflow: ParsedYamlFile
    job_id: str
    step: dict[str, YamlValue]


def is_release_workflow(document: YamlValue) -> bool:
    on = get_on_section(as_dict(document))
    if "release" in on:
        return True
    return "push" in on and "tags" in as_dict(on.get("push"))


def reachable_from_release(condition: YamlValue) -> bool:
    """False only when every `||` disjunct requires a non-release event."""
    if not isinstance(condition, str) or not condition.strip():
        return True
    return not all(_UNREACHABLE.search(part) for part in condition.split("||"))


def _local_callee(uses: YamlValue) -> str | None:
    if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
        return uses[2:].split("@", 1)[0]
    return None


def _release_scope(workflows: list[ParsedYamlFile]) -> list[ParsedYamlFile]:
    by_path = {wf.path: wf for wf in workflows}
    pending = [wf for wf in workflows if is_release_workflow(wf.document)]
    seen: dict[str, ParsedYamlFile] = {}
    while pending:
        wf = pending.pop()
        if wf.path in seen:
            continue
        seen[wf.path] = wf
        for job in jobs_of(as_dict(wf.document)).values():
            callee = _local_callee(job.get("uses"))
            if callee in by_path and reachable_from_release(job.get("if")):
                pending.append(by_path[callee])
    return list(seen.values())


def _reachable_steps(wf: ParsedYamlFile) -> list[_Step]:
    steps: list[_Step] = []
    for job_id, job in jobs_of(as_dict(wf.document)).items():
        if not reachable_from_release(job.get("if")):
            continue
        steps.extend(
            _Step(wf, job_id, step) for step in steps_of(job) if reachable_from_release(step.get("if"))
        )
    return steps


def _script_path(repo_root: Path, match: re.Match[str]) -> Path | None:
    module, file = match.group(1), match.group(2)
    candidate = repo_root / (module.replace(".", "/") + ".py" if module else file)
    return candidate if candidate.is_file() else None


def _reads_earlier_run_conclusion(text: str) -> bool:
    return bool(_RUN_LISTING.search(text) and _CONCLUSION.search(text))


def _step_findings(repo_root: Path, item: _Step) -> list[Finding]:
    run = item.step.get("run")
    if not isinstance(run, str):
        return []
    where = f"job '{item.job_id}'"
    findings: list[Finding] = []
    evidence = _EVIDENCE.search(run)
    if evidence:
        findings.append(Finding(
            rule=RULE, path=item.workflow.path,
            message=f"{where} runs `{evidence.group(1)}` where a tag push or release can reach it; "
            "a release must not skip or reuse a job on attestation or reuse evidence",
            fix="confine the step with `if: github.event_name == 'pull_request'` (or a default-branch push "
            f"ref), so the release executes every job itself ({POLICY})",
        ))
    for match in _SCRIPT.finditer(run):
        script = _script_path(repo_root, match)
        if script is None or not _reads_earlier_run_conclusion(script.read_text(encoding="utf-8")):
            continue
        findings.append(Finding(
            rule=RULE, path=item.workflow.path,
            message=f"{where} runs `{script.relative_to(repo_root).as_posix()}`, which lists earlier workflow "
            "runs and reads their conclusion; a release must not accept an earlier run as proof",
            fix="replace the lookup with an in-run execution of the validation, e.g. a job that calls the CI "
            f"workflow (`uses: ./.github/workflows/ci.yml`) on the release SHA ({POLICY})",
        ))
    return findings


def check_rel_007(repo_root: Path) -> list[Finding]:
    workflows = [wf for wf in load_workflows(repo_root) if wf.status == LoadStatus.OK]
    findings: list[Finding] = []
    for wf in _release_scope(workflows):
        for item in _reachable_steps(wf):
            findings.extend(_step_findings(repo_root, item))
    return findings
