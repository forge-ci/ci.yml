"""Group 2: workflow rules.

GEN-001, GEN-002, GEN-008, TAG-003, SEC-003, SEC-004, RUN-001, RUN-002, RUN-003,
WF-001, WF-002, WF-003, and CT-004 (the linter pin, verified against the actual
checkout step here since CT-001..003/005 live in schema/exceptions). All of
these need parsed workflow YAML; a file that fails to parse (no PyYAML, no
`yq` on PATH) makes every rule that depends on it `needs_review` for that
file rather than silently passing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import (
    ParsedYamlFile,
    as_dict,
    as_list,
    get_on_section,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)
from ci_lint.yaml_io import LoadStatus, YamlValue

FLEET_RUNNERS: frozenset[str] = frozenset(
    {
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "windows-2025",
        "windows-11-arm",
        "macos-15",
        "macos-15-intel",
    }
)
NON_LINUX_FLEET_LABELS: frozenset[str] = frozenset(
    {"windows-2025", "windows-11-arm", "macos-15", "macos-15-intel"}
)
SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")

WORKFLOW_FILE_RULES: tuple[str, ...] = (
    "GEN-001",
    "GEN-002",
    "GEN-008",
    "TAG-003",
    "SEC-003",
    "SEC-004",
    "RUN-001",
    "RUN-002",
    "RUN-003",
    "WF-001",
    "WF-002",
    "WF-003",
    "CT-004",
)
ACTION_FILE_RULES: tuple[str, ...] = ("SEC-004", "WF-003")


def _needs_review_for_unparsed(
    files: list[ParsedYamlFile], rules: tuple[str, ...]
) -> list[Finding]:
    findings: list[Finding] = []
    for f in files:
        if f.status != LoadStatus.NEEDS_REVIEW:
            continue
        for rule in rules:
            findings.append(
                Finding(
                    rule=rule,
                    path=f.path,
                    status=Status.NEEDS_REVIEW,
                    message=f"cannot evaluate {rule} for {f.path}: {f.reason}",
                    fix="make PyYAML importable or `yq` available on PATH so ci-lint can parse "
                    f"{f.path}, then re-run precheck",
                )
            )
    return findings


def _trigger_strings(doc: dict[str, YamlValue]) -> list[str]:
    on = get_on_section(doc)
    out: list[str] = []
    for key, val in on.items():
        if key == "push":
            branches: list[str] = []
            if isinstance(val, dict):
                raw = val.get("branches")
                if isinstance(raw, list):
                    branches = [b for b in raw if isinstance(b, str)]
            out.append("push:main" if branches == ["main"] else "push")
        else:
            out.append(key)
    return out


def check_gen_001(workflows: list[ParsedYamlFile]) -> list[Finding]:
    match = next((w for w in workflows if w.path == ".github/workflows/ci.yml"), None)
    if match is None:
        return [
            Finding(
                rule="GEN-001",
                message="no .github/workflows/ci.yml found",
                fix="add .github/workflows/ci.yml as the pull_request entry point "
                "(see [allow].workflows in ci.toml)",
            )
        ]
    if match.status != LoadStatus.OK:
        return []  # already reported as needs_review
    on = get_on_section(as_dict(match.document))
    if "pull_request" not in on:
        return [
            Finding(
                rule="GEN-001",
                path=match.path,
                message="ci.yml does not declare an 'on.pull_request' trigger",
                fix="add 'pull_request:' under ci.yml's 'on:' section",
            )
        ]
    return []


def check_gen_008(ci: CiToml, workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    pr_files: list[str] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        basename = Path(wf.path).name
        doc = as_dict(wf.document)
        triggers = _trigger_strings(doc)
        if "pull_request" in triggers:
            pr_files.append(wf.path)
        allowed = ci.allow.workflows.get(basename)
        if allowed is None:
            findings.append(
                Finding(
                    rule="GEN-008",
                    path=wf.path,
                    message=f"workflow file '{basename}' is not declared in ci.toml's [allow].workflows",
                    fix=f'add "{basename}" = [...] to ci.toml\'s [allow].workflows, listing its exact '
                    f"triggers, or delete {wf.path}",
                )
            )
            continue
        for trig in triggers:
            if trig not in allowed:
                findings.append(
                    Finding(
                        rule="GEN-008",
                        path=wf.path,
                        message=f"workflow '{basename}' declares trigger '{trig}', which is not in "
                        f"its [allow].workflows entry {list(allowed)}",
                        fix=f'add "{trig}" to allow.workflows."{basename}" in ci.toml, or remove that '
                        f"trigger from {wf.path}",
                    )
                )
    if len(pr_files) > 1:
        findings.append(
            Finding(
                rule="GEN-008",
                message=f"{len(pr_files)} workflow files declare pull_request: {pr_files}",
                fix="keep 'pull_request:' in exactly one workflow file (.github/workflows/ci.yml); "
                "remove it from the others",
            )
        )
    return findings


def check_tag_003(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        on = get_on_section(as_dict(wf.document))
        if "pull_request" not in on:
            continue
        pr_val = on["pull_request"]
        types: list[YamlValue] | None = None
        if isinstance(pr_val, dict):
            raw = pr_val.get("types")
            if isinstance(raw, list):
                types = raw
        if types is None or "edited" not in types:
            findings.append(
                Finding(
                    rule="TAG-003",
                    path=wf.path,
                    message="'on.pull_request.types' does not include 'edited' "
                    "(a title-only tag edit would not re-trigger the run)",
                    fix="add 'types: [opened, synchronize, reopened, edited]' under on.pull_request "
                    f"in {wf.path}",
                )
            )
    return findings


def check_sec_003(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        on = get_on_section(as_dict(wf.document))
        for bad in ("pull_request_target", "workflow_run"):
            if bad in on:
                findings.append(
                    Finding(
                        rule="SEC-003",
                        path=wf.path,
                        message=f"workflow declares '{bad}:', which runs with base-branch privileges "
                        "against untrusted PR content",
                        fix=f"remove '{bad}:' from {wf.path}; use 'pull_request:' with the default "
                        "read-only token instead",
                    )
                )
    return findings


@dataclass(frozen=True)
class _UsesRef:
    uses: str
    loc: str


def _iter_uses(
    doc: dict[str, YamlValue], *, is_composite: bool
) -> list[_UsesRef]:
    out: list[_UsesRef] = []
    if is_composite:
        runs = doc.get("runs")
        if isinstance(runs, dict):
            for i, step in enumerate(as_list(runs.get("steps"))):
                if isinstance(step, dict) and isinstance(step.get("uses"), str):
                    out.append(_UsesRef(step["uses"], f"runs.steps[{i}]"))
    else:
        for job_id, job in jobs_of(doc).items():
            for i, step in enumerate(steps_of(job)):
                uses = step.get("uses")
                if isinstance(uses, str):
                    out.append(_UsesRef(uses, f"jobs.{job_id}.steps[{i}]"))
    return out


# zackees/ci.yml#33 (closes #18/#31's SHA-pinning-vs-float tension): the ONE
# sanctioned first-party floating ref SEC-004 accepts in place of a 40-hex
# SHA. `zackees/setup-soldr@v0`'s `v0` tag moves only through setup-soldr's
# own gated promotion (`update-v0-tag.yml`'s exact-main contract plus a
# FastLED/fbuild canary), not an arbitrary maintainer push, unlike a
# third-party action's floating tag -- so this does not generalize to any
# other action, first- or third-party. `/cook` and `/cleanup` are
# setup-soldr's own sub-actions and share its release cadence. A repository
# with GitHub's own `sha_pinning_required` setting on cannot use this float
# (that setting rejects `@v0` before any job runs) and must SHA-pin instead
# -- `RUST-014` (docs/policy-rust.md) then requires that pin stay fresh.
SETUP_SOLDR_FLOAT_REPO = "zackees/setup-soldr"
SETUP_SOLDR_FLOAT_REF = "v0"


def _is_sanctioned_setup_soldr_float(repo_slug: str, ref: str) -> bool:
    return repo_slug == SETUP_SOLDR_FLOAT_REPO and ref == SETUP_SOLDR_FLOAT_REF


def _check_one_use(uses: str, path: str, loc: str, allowed: frozenset[str]) -> list[Finding]:
    if uses.startswith("./") or uses.startswith("docker://"):
        return []
    if "@" not in uses:
        return [
            Finding(
                rule="SEC-004",
                path=path,
                message=f"{loc}: uses '{uses}' has no @ref",
                fix=f"pin {loc} to a 40-character commit SHA: 'uses: {uses}@<40-hex-sha>  # vX.Y.Z'",
            )
        ]
    slug, ref = uses.split("@", 1)
    repo_slug = "/".join(slug.split("/")[:2])
    out: list[Finding] = []
    if repo_slug not in allowed:
        out.append(
            Finding(
                rule="SEC-004",
                path=path,
                message=f"{loc}: action '{repo_slug}' is not in ci.toml's [allow].actions",
                fix=f'add "{repo_slug}" to ci.toml\'s [allow].actions, or replace {loc} with an '
                "allowlisted action",
            )
        )
    if not SHA40_RE.match(ref) and not _is_sanctioned_setup_soldr_float(repo_slug, ref):
        out.append(
            Finding(
                rule="SEC-004",
                path=path,
                message=f"{loc}: uses '{uses}' is not pinned to a 40-character commit SHA",
                fix=f"pin {loc} to the action's full commit SHA with a version comment: "
                f"'uses: {slug}@<40-hex-sha>  # vX.Y.Z' -- or, only for {SETUP_SOLDR_FLOAT_REPO}, "
                f"float at '@{SETUP_SOLDR_FLOAT_REF}' (zackees/ci.yml#33's one sanctioned "
                "first-party float; every other action stays SHA-pinned)",
            )
        )
    return out


def check_sec_004(
    ci: CiToml, workflows: list[ParsedYamlFile], actions: list[ParsedYamlFile]
) -> list[Finding]:
    findings: list[Finding] = []
    allowed = frozenset(ci.allow.actions)
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for ref in _iter_uses(as_dict(wf.document), is_composite=False):
            findings.extend(_check_one_use(ref.uses, wf.path, ref.loc, allowed))
    for act in actions:
        if act.status != LoadStatus.OK:
            continue
        for ref in _iter_uses(as_dict(act.document), is_composite=True):
            findings.extend(_check_one_use(ref.uses, act.path, ref.loc, allowed))
    return findings


# A precheck-plan output that enumerates platform matrix legs built from
# ci.toml's [platforms] table (see `ci_lint.plan.PlatformLane.to_json_dict`,
# which emits a `runs_on` field per leg straight from `Platform.runs_on`).
# A `strategy.matrix.<key>` sourced from one of these, combined with a
# `runs-on: ${{ matrix.<key>.runs_on }}` expression using that same key, is
# resolvable statically: it can only ever take the values ci.toml's
# [platforms] declare, so RUN-001 validates those instead of giving up.
PLATFORM_LANES_OUTPUTS: frozenset[str] = frozenset(
    {"platform_lanes_todo_json", "platform_lanes_json"}
)
_NEEDS_OUTPUTS_RE = re.compile(r"needs\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_]+")


def _matrix_platform_lane_key(job: dict[str, YamlValue]) -> str | None:
    """Return the `strategy.matrix` key (e.g. `"lane"`) whose value is a
    `fromJSON(needs.<job>.outputs.<platform-lanes output>)` expression, or
    None if this job's matrix isn't built from a precheck plan platform-
    lanes output."""
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return None
    for key, val in matrix.items():
        if not isinstance(val, str):
            continue
        if not _NEEDS_OUTPUTS_RE.search(val):
            continue
        if any(output in val for output in PLATFORM_LANES_OUTPUTS):
            return key
    return None


def _check_platform_runs_on(ci: CiToml) -> list[Finding]:
    """RUN-001 resolved for the matrix case: validate ci.toml's own
    [platforms].*.runs-on values, since that is what the matrix leg's
    `runs_on` field can ever contain at runtime."""
    findings: list[Finding] = []
    for pid, platform in sorted(ci.platforms.items()):
        label = platform.runs_on
        if not label:
            continue
        if label in FLEET_RUNNERS:
            continue
        reason = (
            "a '-latest' label (retires without notice)"
            if label.endswith("-latest")
            else "not in the fleet runner list"
        )
        findings.append(
            Finding(
                rule="RUN-001",
                path="ci.toml",
                message=f"[platforms.{pid}].runs-on = '{label}' is {reason} (resolved from a "
                "matrix built over ci.toml's [platforms] and used as 'runs-on: "
                "${{ matrix.<var>.runs_on }}')",
                fix=f"set [platforms.{pid}].runs-on in ci.toml to one of the fleet labels: "
                f"{sorted(FLEET_RUNNERS)}",
            )
        )
    return findings


def check_run_001(ci: CiToml, workflows: list[ParsedYamlFile]) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    platform_runs_on_checked = False
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            runs_on = job.get("runs-on")
            labels: list[str] = []
            if isinstance(runs_on, str):
                labels = [runs_on]
            elif isinstance(runs_on, list):
                labels = [x for x in runs_on if isinstance(x, str)]
            lane_key = _matrix_platform_lane_key(job)
            for label in labels:
                if "${{" in label:
                    if lane_key is not None and f"matrix.{lane_key}.runs_on" in label:
                        if not platform_runs_on_checked:
                            findings.extend(_check_platform_runs_on(ci))
                            platform_runs_on_checked = True
                        continue
                    findings.append(
                        Finding(
                            rule="RUN-001",
                            path=wf.path,
                            status=Status.NEEDS_REVIEW,
                            message=f"jobs.{job_id}.runs-on is a dynamic expression '{label}'; cannot "
                            "statically verify it against the fleet runner list",
                            fix="resolve what the expression evaluates to and confirm it is one of the "
                            "fleet labels documented in docs/ci-toml.md",
                        )
                    )
                elif label not in FLEET_RUNNERS:
                    reason = "a '-latest' label (retires without notice)" if label.endswith(
                        "-latest"
                    ) else "not in the fleet runner list"
                    findings.append(
                        Finding(
                            rule="RUN-001",
                            path=wf.path,
                            message=f"jobs.{job_id}.runs-on = '{label}' is {reason}",
                            fix="use one of the fleet labels: "
                            f"{sorted(FLEET_RUNNERS)}",
                        )
                    )
    return findings


_BUILD_HOST_ANNOTATION_RE = re.compile(r"\(on\s+[\w.\-]+", re.IGNORECASE)
_NATIVE_ANNOTATION_RE = re.compile(r"\(native\b", re.IGNORECASE)


def check_run_002(workflows: list[ParsedYamlFile]) -> list[Finding]:
    """RUN-002 (ci.yml#12): a cross-compiled job's display name must show
    where it builds and where it runs. Detection reuses RUN-001's
    platform-lanes matrix signal (`_matrix_platform_lane_key`) -- a job
    whose `strategy.matrix` is built from the precheck plan's
    `platform_lanes_json`/`platform_lanes_todo_json` output is one leg of a
    cross-compile pipeline:

    - a leg whose `runs-on` is a STATIC label (it always builds on that
      host, regardless of the lane's target) must say so in its `name:`,
      e.g. `... (on ubuntu-24.04, soldr) ...`, so a glance at the Checks
      tab shows the build never happens on the target platform.
    - a leg whose `runs-on` is the dynamic `${{ matrix.<key>.runs_on }}`
      expression (it executes natively on the lane's own runner, with
      compiling assumed impossible) must say so, e.g. `... (native) ...`.
    """
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            lane_key = _matrix_platform_lane_key(job)
            if lane_key is None:
                continue
            runs_on = job.get("runs-on")
            name = job.get("name")
            name_str = name if isinstance(name, str) else ""
            is_dynamic_lane_runner = (
                isinstance(runs_on, str) and f"matrix.{lane_key}.runs_on" in runs_on
            )
            if is_dynamic_lane_runner:
                if _NATIVE_ANNOTATION_RE.search(name_str):
                    continue
                findings.append(
                    Finding(
                        rule="RUN-002",
                        path=wf.path,
                        message=f"jobs.{job_id} executes natively on its lane's own runner "
                        f"(runs-on: {runs_on}) but its `name:` does not say so",
                        fix=f"add a '(native)' (or '(native {{runner}})') annotation to "
                        f"jobs.{job_id}.name so the Checks tab shows this job runs on the "
                        "target platform and never compiles there",
                    )
                )
            elif isinstance(runs_on, str) and "${{" not in runs_on:
                if _BUILD_HOST_ANNOTATION_RE.search(name_str):
                    continue
                findings.append(
                    Finding(
                        rule="RUN-002",
                        path=wf.path,
                        message=f"jobs.{job_id} is one leg of a cross-compile matrix but always "
                        f"builds on a fixed host (runs-on: {runs_on}); its `name:` does not "
                        "say where it builds",
                        fix=f"add a '(on {runs_on}, ...)' annotation to jobs.{job_id}.name "
                        "(e.g. '(on ubuntu-24.04, soldr)') so the Checks tab shows the build "
                        "host, not just the target",
                    )
                )
    return findings


# ── RUN-003 (#150) ────────────────────────────────────────────────────────
# A native non-x64 Linux job stays off the pull_request and default-branch
# push path. Static clause only: the dispatch-only and lost-RUST-008 clauses
# need cross-workflow/run evidence and stay candidate.

_NON_LINUX_PREFIXES: tuple[str, ...] = ("windows", "macos")
_EVENT_EQ_RE = re.compile(r"github\.event_name\s*==\s*['\"]([A-Za-z_]+)['\"]")
_EVENT_NE_RE = re.compile(r"github\.event_name\s*!=\s*['\"]([A-Za-z_]+)['\"]")
_TAG_REF_RE = re.compile(r"startsWith\(\s*github\.ref\s*,\s*['\"]refs/tags/")
_RUN_003_OFF_PATH_EVENTS: frozenset[str] = frozenset({"schedule", "workflow_dispatch", "release"})
_DEFAULT_BRANCHES: frozenset[str] = frozenset({"main", "master"})


def is_native_arm_linux_label(label: str) -> bool:
    """`ubuntu-24.04-arm` or any other `*-arm` label that is not Windows/macOS."""
    low = label.strip().lower()
    if "${{" in low or low.startswith(_NON_LINUX_PREFIXES):
        return False
    return low.endswith("-arm") or low.endswith("-arm64")


def _push_reaches_default_branch(push: YamlValue) -> bool:
    if not isinstance(push, dict):
        return True
    branches = push.get("branches")
    if isinstance(branches, list):
        names = [b for b in branches if isinstance(b, str)]
        return any(b in _DEFAULT_BRANCHES or "*" in b for b in names)
    if branches is None and push.get("branches-ignore") is None:
        # tags-only push filter never reaches the default branch
        return not ("tags" in push or "tags-ignore" in push)
    ignored = push.get("branches-ignore")
    if isinstance(ignored, list):
        return not any(b in _DEFAULT_BRANCHES for b in ignored if isinstance(b, str))
    return True


def _pr_push_events(doc: dict[str, YamlValue]) -> tuple[str, ...]:
    on = get_on_section(doc)
    events: list[str] = []
    for ev in ("pull_request", "pull_request_target"):
        if ev in on:
            events.append(ev)
    if "push" in on and _push_reaches_default_branch(on["push"]):
        events.append("push")
    return tuple(events)


def _if_confines_off_pr_push(cond: str) -> bool | None:
    """True: the `if:` provably confines the job to schedule/dispatch/release,
    a tag ref, or the `ci-full` tag. None: an `if:` we cannot prove either way."""
    if "ci-full" in cond:
        return True
    positives = set(_EVENT_EQ_RE.findall(cond))
    negatives = set(_EVENT_NE_RE.findall(cond))
    if "||" not in cond:
        if positives and positives <= _RUN_003_OFF_PATH_EVENTS:
            return True
        if {"push"} <= negatives and negatives & {"pull_request", "pull_request_target"}:
            return True
        if _TAG_REF_RE.search(cond):
            return True
    elif positives and positives <= _RUN_003_OFF_PATH_EVENTS and not negatives:
        stripped = _EVENT_EQ_RE.sub("", cond)
        if "github." not in stripped and "needs." not in stripped and "matrix." not in stripped:
            return True
    return None


def _pick_literal(val: YamlValue, rest: list[str]) -> str | None:
    for part in rest:
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    if isinstance(val, str) and "${{" not in val:
        return val
    return None


def _pick_all(items: list[YamlValue], rest: list[str]) -> list[str] | None:
    picked = [_pick_literal(v, rest) for v in items]
    if any(p is None for p in picked):
        return None
    return [p for p in picked if p is not None]


def _literal_matrix_values(job: dict[str, YamlValue], key_path: str) -> list[str] | None:
    """Resolve `matrix.<k>` or `matrix.<k>.<f>` from a literal strategy.matrix
    (list values and `include` entries). None if anything is dynamic."""
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return None
    head, *rest = key_path.split(".")
    top = matrix.get(head)
    include = matrix.get("include")
    if (top is not None and not isinstance(top, list)) or (
        include is not None and not isinstance(include, list)
    ):
        return None
    from_top = _pick_all(top or [], rest)
    entries = [e[head] for e in include or [] if isinstance(e, dict) and head in e]
    from_include = _pick_all(entries, rest)
    if from_top is None or from_include is None:
        return None
    return (from_top + from_include) or None


_MATRIX_EXPR_RE = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_.-]+)\s*\}\}$")


@dataclass(frozen=True)
class _Run003Labels:
    arm: tuple[str, ...]
    unresolved: tuple[str, ...]


def _run_003_labels(ci: CiToml, job: dict[str, YamlValue]) -> _Run003Labels:
    """Native arm64 Linux labels and unresolvable dynamic labels for one job."""
    runs_on = job.get("runs-on")
    labels: list[str] = []
    if isinstance(runs_on, str):
        labels = [runs_on]
    elif isinstance(runs_on, list):
        labels = [x for x in runs_on if isinstance(x, str)]
    arm: list[str] = []
    unresolved: list[str] = []
    lane_key = _matrix_platform_lane_key(job)
    for label in labels:
        if "${{" not in label:
            resolved: list[str] | None = [label]
        elif lane_key is not None and f"matrix.{lane_key}.runs_on" in label:
            arm.extend(
                f"{p.runs_on} ([platforms.{pid}])"
                for pid, p in sorted(ci.platforms.items())
                if is_native_arm_linux_label(p.runs_on)
            )
            continue
        else:
            m = _MATRIX_EXPR_RE.match(label.strip())
            resolved = _literal_matrix_values(job, m.group(1)) if m else None
        if resolved is None:
            unresolved.append(label)
        else:
            arm.extend(v for v in resolved if is_native_arm_linux_label(v))
    return _Run003Labels(tuple(arm), tuple(unresolved))


def _run_003_confined(cond: YamlValue) -> bool | None:
    if isinstance(cond, str):
        return _if_confines_off_pr_push(cond)
    return cond is False


def _run_003_finding(
    path: str, job_id: str, events: str, labels: _Run003Labels, cond: YamlValue
) -> Finding:
    arm, unresolved = labels.arm, labels.unresolved
    if arm and not isinstance(cond, str):
        return Finding(
            rule="RUN-003",
            path=path,
            message=f"jobs.{job_id} runs on native arm64 Linux ({', '.join(arm)}) "
            f"and is reachable from {events} with no `if:` excluding it",
            fix="move the native lane to a nightly `schedule` (or a test-running "
            "release flow / the `ci-full` tag) and keep the RUST-008 cross-target "
            "`check` on the PR/push path (docs/policy-general.md, #150)",
        )
    what = (
        f"runs on native arm64 Linux ({', '.join(arm)})"
        if arm
        else f"has a dynamic runs-on {unresolved}"
    )
    detail = (
        f"its `if: {cond}` cannot be proven to exclude {events}"
        if isinstance(cond, str)
        else f"it is reachable from {events} if that resolves to an arm64 Linux label"
    )
    return Finding(
        rule="RUN-003",
        path=path,
        status=Status.NEEDS_REVIEW,
        message=f"jobs.{job_id} {what}; {detail}",
        fix="confirm the job never runs a native arm64 Linux lane on "
        "pull_request/default-branch push, or confine it with an "
        "`if: github.event_name == 'schedule'`-style guard",
    )


def check_run_003(ci: CiToml, workflows: list[ParsedYamlFile]) -> list[Finding]:
    """RUN-003 (#150): a job on a native arm64 Linux runner must not be
    reachable from `pull_request` or a default-branch `push`. Its job `if:`
    must confine it to `schedule`/`workflow_dispatch`/`release` (or a tag ref,
    or the `ci-full` tag). Platform-lanes matrix legs are resolved against
    ci.toml's [platforms].*.runs-on, as RUN-001 does; other dynamic labels
    and unprovable `if:` expressions are `needs_review`."""
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        events = _pr_push_events(doc)
        if not events:
            continue
        for job_id, job in jobs_of(doc).items():
            labels = _run_003_labels(ci, job)
            cond = job.get("if")
            if (labels.arm or labels.unresolved) and _run_003_confined(cond) is not True:
                findings.append(_run_003_finding(wf.path, job_id, "/".join(events), labels, cond))
    return findings


def check_wf_001(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            if "uses" in job:
                continue  # a reusable-workflow-call job has no timeout-minutes of its own
            if "timeout-minutes" not in job:
                findings.append(
                    Finding(
                        rule="WF-001",
                        path=wf.path,
                        message=f"job '{job_id}' has no timeout-minutes",
                        fix=f"add 'timeout-minutes: <n>' to jobs.{job_id} in {wf.path}",
                    )
                )
    return findings


def check_wf_002(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        if Path(wf.path).name == "ci-pre.yml":
            # zackees/ci.yml#23 §3: ci-pre.yml's concurrency is per job (only
            # the cache-janitor job is serialized); a workflow-level group is
            # GEN-015 there (ci_lint.rules.ci_pre), not a WF-002 requirement.
            continue
        if "concurrency" not in as_dict(wf.document):
            findings.append(
                Finding(
                    rule="WF-002",
                    path=wf.path,
                    message="workflow has no top-level 'concurrency' key",
                    fix="add a top-level 'concurrency: { group: <expr>, cancel-in-progress: true }' to "
                    f"{wf.path}",
                )
            )
    return findings


def check_wf_003(  # noqa: C901
    workflows: list[ParsedYamlFile], actions: list[ParsedYamlFile]
) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            if "continue-on-error" in job:
                findings.append(
                    Finding(
                        rule="WF-003",
                        path=wf.path,
                        message=f"jobs.{job_id} sets continue-on-error",
                        fix=f"remove 'continue-on-error' from jobs.{job_id} in {wf.path}; fix or gate "
                        "the failing step instead",
                    )
                )
            for i, step in enumerate(steps_of(job)):
                if "continue-on-error" in step:
                    findings.append(
                        Finding(
                            rule="WF-003",
                            path=wf.path,
                            message=f"jobs.{job_id}.steps[{i}] sets continue-on-error",
                            fix=f"remove 'continue-on-error' from jobs.{job_id}.steps[{i}] in {wf.path}",
                        )
                    )
    for act in actions:
        if act.status != LoadStatus.OK:
            continue
        runs = as_dict(act.document).get("runs")
        if isinstance(runs, dict):
            for i, step in enumerate(as_list(runs.get("steps"))):
                if isinstance(step, dict) and "continue-on-error" in step:
                    findings.append(
                        Finding(
                            rule="WF-003",
                            path=act.path,
                            message=f"runs.steps[{i}] sets continue-on-error",
                            fix=f"remove 'continue-on-error' from runs.steps[{i}] in {act.path}",
                        )
                    )
    return findings


def _looks_non_linux(label: str) -> bool | None:
    if "${{" in label:
        return None
    if label in NON_LINUX_FLEET_LABELS:
        return True
    lower = label.lower()
    if "windows" in lower or "macos" in lower or "win-" in lower:
        return True
    if label.startswith("ubuntu") or "linux" in lower:
        return False
    return None


def check_gen_002(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            runs_on = job.get("runs-on")
            labels: list[str] = []
            if isinstance(runs_on, str):
                labels = [runs_on]
            elif isinstance(runs_on, list):
                labels = [x for x in runs_on if isinstance(x, str)]
            if not any(_looks_non_linux(label) for label in labels):
                continue
            if_expr = job.get("if")
            haystacks: list[str] = []
            if isinstance(if_expr, str):
                haystacks.append(if_expr)
            strategy = job.get("strategy")
            if isinstance(strategy, dict) and "matrix" in strategy:
                haystacks.append(json.dumps(strategy["matrix"], default=str))
            if any("needs.precheck.outputs" in h for h in haystacks):
                continue
            findings.append(
                Finding(
                    rule="GEN-002",
                    path=wf.path,
                    message=f"job '{job_id}' runs on non-Linux runner(s) {labels} without an "
                    "if:/matrix derived from needs.precheck.outputs",
                    fix=f"gate jobs.{job_id} with an 'if:' or 'strategy.matrix' expression that "
                    "references needs.precheck.outputs, so only the platforms the precheck plan "
                    "selected actually run",
                )
            )
    return findings


def check_ct_004(ci: CiToml, workflows: list[ParsedYamlFile]) -> list[Finding]:
    if ci.linter_sha is None:
        return []
    findings: list[Finding] = []
    found_checkout = False
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            for i, step in enumerate(steps_of(job)):
                uses = step.get("uses")
                if not isinstance(uses, str) or not uses.startswith("actions/checkout@"):
                    continue
                with_ = step.get("with")
                if not isinstance(with_, dict) or with_.get("repository") != "zackees/ci.yml":
                    continue
                found_checkout = True
                ref = with_.get("ref")
                if ref != ci.linter_sha:
                    findings.append(
                        Finding(
                            rule="CT-004",
                            path=wf.path,
                            message=f"jobs.{job_id}.steps[{i}] checks out zackees/ci.yml at "
                            f"ref={ref!r}, which does not match ci.toml's linter pin "
                            f"'{ci.linter_sha}'",
                            fix=f"set with.ref to '{ci.linter_sha}' in {wf.path} (jobs.{job_id}."
                            f"steps[{i}]), or update ci.toml's 'linter' field and re-run precheck",
                        )
                    )
    if not found_checkout:
        findings.append(
            Finding(
                rule="CT-004",
                message="no workflow step checks out zackees/ci.yml (needed to run ci-lint's precheck)",
                fix="add an actions/checkout step with 'repository: zackees/ci.yml' and "
                f"'ref: {ci.linter_sha}' to the workflow that runs precheck",
            )
        )
    return findings


def check_group2(ci: CiToml, repo_root: Path) -> list[Finding]:
    workflows = load_workflows(repo_root)
    actions = load_composite_actions(repo_root)

    findings: list[Finding] = []
    findings.extend(_needs_review_for_unparsed(workflows, WORKFLOW_FILE_RULES))
    findings.extend(_needs_review_for_unparsed(actions, ACTION_FILE_RULES))

    findings.extend(check_gen_001(workflows))
    findings.extend(check_gen_008(ci, workflows))
    findings.extend(check_tag_003(workflows))
    findings.extend(check_sec_003(workflows))
    findings.extend(check_sec_004(ci, workflows, actions))
    findings.extend(check_run_001(ci, workflows))
    findings.extend(check_run_002(workflows))
    findings.extend(check_run_003(ci, workflows))
    findings.extend(check_wf_001(workflows))
    findings.extend(check_wf_002(workflows))
    findings.extend(check_wf_003(workflows, actions))
    findings.extend(check_wf_004(workflows))
    findings.extend(check_gen_002(workflows))
    findings.extend(check_ct_004(ci, workflows))
    return findings


# WF-004: a job that `needs:` a conditionally-skipped job needs `always()`.
#
# GitHub Actions: when job B declares `needs: [A]` and A is SKIPPED, B is
# SKIPPED too -- regardless of B's own `if:`. A bare
# `if: !cancelled() && !failure() && ...` does NOT override that; only
# `always()` does. So putting an event-restricted job in another job's `needs:`
# silently disables the dependent on every other event.
#
# Measured 2026-10-05 across the fleet:
#   zackees/clud#1805     `cache-maint` (push-to-main only) in the `needs:` of
#                         `dylint` and `build-linux-x64` -> both skipped on
#                         every PR, so `CI OK` reported "a minimal lane did not
#                         run to success" across a 20-job replay.
#   template-python-rust-cmd#67  `reuse-decision` + `cache-maint` in the
#                         `needs:` of `fast` -- the PR QUICK GATE, skipped on
#                         every pull_request, in the canonical template other
#                         repositories copy.
#   zackees/mimalloc-pprof  `memory-gate-decide` (PR-only) -> `memory-gate`,
#                         which therefore dies on every push to main instead.
#
# The failure is silent: a skipped required check reads as green-adjacent to
# anything that inspects only conclusions.
#
# This is `needs_review` rather than a violation because the restricting
# condition may be one that never evaluates false in practice (a `needs:` whose
# target is itself always-run), and the judgement of "does this dependent
# actually need to run when its dependency skips" belongs to the repository.
_EVENT_RESTRICTED = re.compile(
    r"github\.event_name|github\.ref\b|github\.event\.action|github\.head_ref"
)


def _needs_of(job: dict[str, YamlValue]) -> tuple[str, ...]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(x) for x in raw if isinstance(x, str))
    return ()


def _is_conditionally_skipped(job: dict[str, YamlValue]) -> bool:
    """True when this job can be SKIPPED by its own `if:`.

    Only an event-restricted condition is treated as such. A mode/output gate
    (`needs.x.outputs.mode == 'full'`) is also a skip, but those are the
    repository's own selection logic and are far too common to flag here --
    flagging them would bury the event-restricted case, which is the one that
    silently kills a PR gate.
    """

    condition = job.get("if")
    return isinstance(condition, str) and _EVENT_RESTRICTED.search(condition) is not None


def check_wf_004(workflows: list[ParsedYamlFile]) -> list[Finding]:
    findings: list[Finding] = []
    for wf in workflows:
        if wf.status != LoadStatus.OK:
            continue
        jobs = jobs_of(as_dict(wf.document))
        restricted = {jid for jid, job in jobs.items() if _is_conditionally_skipped(job)}
        if not restricted:
            continue
        for jid, job in jobs.items():
            blocking = [n for n in _needs_of(job) if n in restricted]
            if not blocking:
                continue
            condition = job.get("if")
            has_always = isinstance(condition, str) and "always()" in condition
            if has_always:
                continue
            findings.append(
                Finding(
                    rule="WF-004",
                    status=Status.NEEDS_REVIEW,
                    path=wf.path,
                    message=(
                        f"job '{jid}' needs {', '.join(repr(b) for b in blocking)}, which "
                        "is gated on the event, so it is SKIPPED on other events -- and a "
                        "skipped `needs:` skips this job too, whatever this job's own `if:` "
                        "says. Only `always()` overrides that cascade."
                    ),
                    fix=(
                        "if this job must still run when the dependency is skipped, use "
                        "`if: always() && !cancelled() && <existing condition>`. If skipping "
                        "is intended, say so in a comment -- a skipped required check reads "
                        "as green-adjacent to anything that inspects only conclusions"
                    ),
                )
            )
    return findings
