"""Group 13: GEN-003 -- a profile-required check skipped via a literal

`if: false` guard (M2-21, ci.yml#44 part B).

Prior to this round `docs/policy-general.md` marked GEN-003 a candidate
because no fleet-wide inventory/profile registry exists yet to check a
repository's declared checks against (issue #2). That gap is still real:
this repository's own suite/flow/tag/plan machinery dispatches a required
suite's `run` command dynamically (through `ci-lint plan`'s matrix/tag
selection), so a static scan cannot reliably tell "this suite's run command
never appears as a literal `run:` line" apart from "this suite is invoked
through the plan, as designed" -- attempting that produced false positives
against this repository's own canonical green fixture.

What IS reliably static, and unambiguous, is a suite marked
`[suites.<id>].required = true` whose invoking step (or that step's job) is
disabled with a literal `if: false` / `if: 'false'` -- there is no
conditional logic to evaluate; the step can never run, full stop. That is
"skipped" in the M2-21 brief's sense, and it is what this rule enforces. A
suite whose run command never appears as a literal `run:` line anywhere
stays `needs_review` (not a pass, not a hard violation) until the
fleet-wide inventory (issue #2) exists to check "wired via the plan" for
real; see docs/policy-general.md's GEN-003 row for the still-open half.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.schema import CiToml
from ci_lint.yaml_io import YamlValue
from ci_lint.workflow_scan import (
    as_dict,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)


def _run_prefix(run_cmd: str) -> str | None:
    try:
        tokens = shlex.split(run_cmd)
    except ValueError:
        return None
    launchers = {"python3", "python", "uv", "soldr", "run"}
    while tokens and tokens[0] in launchers:
        tokens = tokens[1:]
    if not tokens:
        return None
    return " ".join(tokens[:2]) if len(tokens) >= 2 else tokens[0]


def _is_literal_false(value: object) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().strip("'\"").lower() == "false"
    return False


def check_group13(ci: CiToml, repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []

    required = {sid: s for sid, s in ci.suites.items() if s.required}
    if not required:
        return findings

    prefixes = {sid: _run_prefix(s.run) for sid, s in required.items()}

    files: list[dict[str, YamlValue]] = []
    for wf in load_workflows(repo_root):
        if wf.status.name == "OK":
            files.append(as_dict(wf.document))
    for act in load_composite_actions(repo_root):
        if act.status.name == "OK":
            files.append(as_dict(act.document))

    seen_live: dict[str, bool] = {sid: False for sid in required}
    seen_any: dict[str, bool] = {sid: False for sid in required}

    for doc in files:
        for _jid, job in jobs_of(doc).items():
            job_disabled = _is_literal_false(job.get("if"))
            for step in steps_of(job):
                run_cmd = step.get("run")
                if not isinstance(run_cmd, str):
                    continue
                step_disabled = job_disabled or _is_literal_false(step.get("if"))
                for sid, prefix in prefixes.items():
                    if prefix and prefix in run_cmd:
                        seen_any[sid] = True
                        if not step_disabled:
                            seen_live[sid] = True

    for sid, suite in required.items():
        if seen_any[sid] and not seen_live[sid]:
            findings.append(
                Finding(
                    rule="GEN-003",
                    path="ci.toml",
                    message=f"[suites.{sid}] is required = true but every step invoking its run "
                    f"command ('{suite.run}') is disabled with a literal 'if: false'",
                    fix=f"remove the 'if: false' guard so '{sid}' actually runs, or remove "
                    "'required = true' and record an approved coverage replacement in ci.toml's "
                    "history instead",
                )
            )
        elif not seen_any[sid]:
            findings.append(
                Finding(
                    rule="GEN-003",
                    path="ci.toml",
                    message=f"[suites.{sid}] is required = true but no workflow/composite-action "
                    f"step's literal 'run:' line invokes its run command ('{suite.run}') -- it may "
                    "be dispatched dynamically via the plan, which this static scan cannot confirm",
                    fix=f"confirm '{sid}' is actually reachable (e.g. via 'ci-lint plan' output "
                    "consumed by a matrix job), or add a step that runs it directly",
                    status=Status.NEEDS_REVIEW,
                )
            )

    return findings
