"""GEN-023 (static clause): compiler-cache stats stay in the step summary.

zackees/ci.yml#411 (docs/policy-general.md). Every compiling job publishes
its compiler-cache result to `$GITHUB_STEP_SUMMARY`. For Rust jobs the
emitter is setup-soldr itself: its `compile-cache-stats` input defaults to
`summarize` (`none` suppresses all compile-cache output; `summarize`,
`detailed` and `insights` all write the summary -- setup-soldr action.yml).

Static signals (workflows and composite actions; a workflow job's
`uses: ./<local composite>` step is followed one level):

- violation: a `zackees/setup-soldr` step whose `with:` sets
  `compile-cache-stats: none` (case-insensitive literal).
- needs_review: a job that invokes ccache/zccache itself -- a
  `CMAKE_<LANG>_COMPILER_LAUNCHER` set to ccache/zccache (env or run line) or
  a `ccache`/`zccache` command on a `run:` line -- with no setup-soldr step,
  and no `run:` step that writes `ccache -s`/`--show-stats` or
  `zccache stats`/`--show-stats` output to `$GITHUB_STEP_SUMMARY`.

Still candidate (setup-soldr work, not checked here): the exact-vs-fallback
restore flag in the summary and the < 80% hit-rate warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.tools import UsesStep, WorkflowJob, _iter_workflow_jobs
from ci_lint.workflow_scan import as_dict, as_list, load, load_composite_actions, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "GEN-023"
SETUP_SOLDR = "zackees/setup-soldr"
INPUT = "compile-cache-stats"

_LAUNCHER = re.compile(r"CMAKE_\w*_COMPILER_LAUNCHER\W+[\w./-]*\b(?:ccache|zccache)\b")
_TOOL_CMD = re.compile(r"(?:^|[\s;&|(`])(?:[\w./-]*/)?(ccache|zccache)(?:\.exe)?(?=\s|$)", re.MULTILINE)
_STATS = re.compile(r"\b(?:ccache|zccache)\b[^\n]*(?:\s-s\b|--show-stats|\sstats\b|--print-stats)")

_FIX_NONE = (
    "remove 'compile-cache-stats: none' (the default 'summarize' writes the per-session hit/miss table "
    "to $GITHUB_STEP_SUMMARY); do not replace it with a duplicate stats step"
)
_FIX_SUMMARY = (
    "add a final 'if: always()' step that appends 'ccache --show-stats' (or 'zccache --show-stats') "
    "output in a fenced block under a '### <os> <tool> stats (<target>)' heading to $GITHUB_STEP_SUMMARY"
)


@dataclass(frozen=True)
class JobStep:
    """A step a job runs, with the file/location it was declared at."""

    step: dict[str, YamlValue]
    path: str
    loc: str


def _is_setup_soldr(uses: str) -> bool:
    return uses.split("@", 1)[0].rstrip("/") == SETUP_SOLDR


def _uses_steps(steps: list[YamlValue], prefix: str) -> list[UsesStep]:
    out: list[UsesStep] = []
    for i, raw in enumerate(steps):
        step = as_dict(raw)
        uses = step.get("uses")
        if isinstance(uses, str):
            out.append(UsesStep(uses, f"{prefix}steps[{i}]", as_dict(step.get("with"))))
    return out


def _none_findings(path: str, steps: list[UsesStep]) -> list[Finding]:
    out: list[Finding] = []
    for us in steps:
        value = us.with_.get(INPUT)
        if _is_setup_soldr(us.uses) and isinstance(value, str) and value.strip().lower() == "none":
            out.append(
                Finding(
                    rule=RULE,
                    path=path,
                    message=f"{us.loc}: {SETUP_SOLDR} sets '{INPUT}: none', suppressing the compiler-cache "
                    "summary every compiling job must publish",
                    fix=_FIX_NONE,
                )
            )
    return out


def _composite_steps(repo_root: Path, uses: str) -> list[JobStep]:
    if not uses.startswith("./"):
        return []
    base = repo_root / uses
    for name in ("action.yml", "action.yaml"):
        if (base / name).is_file():
            parsed = load(repo_root, base / name)
            if parsed.status != LoadStatus.OK:
                return []
            runs = as_dict(as_dict(parsed.document).get("runs"))
            return [
                JobStep(as_dict(s), parsed.path, f"runs.steps[{i}]") for i, s in enumerate(as_list(runs.get("steps")))
            ]
    return []


def _effective_steps(repo_root: Path, wj: WorkflowJob) -> list[JobStep]:
    out: list[JobStep] = []
    for i, step in enumerate(steps_of(wj.job)):
        out.append(JobStep(step, wj.path, f"jobs.{wj.job_id}.steps[{i}]"))
        uses = step.get("uses")
        if isinstance(uses, str):
            out.extend(_composite_steps(repo_root, uses))
    return out


def _env_text(value: YamlValue) -> str:
    return "\n".join(f"{k}={v}" for k, v in as_dict(value).items())


def _invokes_cache(wj: WorkflowJob, steps: list[JobStep]) -> bool:
    texts = [_env_text(wj.job.get("env"))]
    for js in steps:
        texts.append(_env_text(js.step.get("env")))
        run = js.step.get("run")
        if isinstance(run, str):
            texts.append(run)
    return any(_LAUNCHER.search(t) or _TOOL_CMD.search(t) for t in texts)


def _publishes_stats(steps: list[JobStep]) -> bool:
    for js in steps:
        run = js.step.get("run")
        if isinstance(run, str) and "GITHUB_STEP_SUMMARY" in run and _STATS.search(run):
            return True
    return False


def check_gen_023(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for act in load_composite_actions(repo_root):
        if act.status == LoadStatus.OK:
            runs = as_dict(as_dict(act.document).get("runs"))
            findings.extend(_none_findings(act.path, _uses_steps(as_list(runs.get("steps")), "runs.")))
    for wj in _iter_workflow_jobs(repo_root):
        raw_steps: list[YamlValue] = list(steps_of(wj.job))
        findings.extend(_none_findings(wj.path, _uses_steps(raw_steps, f"jobs.{wj.job_id}.")))
        steps = _effective_steps(repo_root, wj)
        if any(isinstance(s.step.get("uses"), str) and _is_setup_soldr(str(s.step.get("uses"))) for s in steps):
            continue
        if _invokes_cache(wj, steps) and not _publishes_stats(steps):
            findings.append(
                Finding(
                    rule=RULE,
                    path=wj.path,
                    status=Status.NEEDS_REVIEW,
                    message=f"jobs.{wj.job_id}: invokes ccache/zccache outside setup-soldr but no step writes "
                    "its stats to $GITHUB_STEP_SUMMARY",
                    fix=_FIX_SUMMARY,
                )
            )
    return findings
