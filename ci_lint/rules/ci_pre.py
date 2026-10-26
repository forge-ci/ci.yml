"""Group 12: the `ci-pre.yml` shape (zackees/ci.yml#23, implemented in #51).

`ci-pre.yml` is the fleet's fast first workflow (`on: workflow_call`, the
first job of `ci.yml`). Nothing is called "precheck" any more:

- GEN-014: a `ci-precheck.yml` workflow file (or `[allow].workflows` entry,
  or a `uses:` reference to one) -- wrong name, rename it to `ci-pre.yml`.
- GEN-015: a workflow-level `concurrency:` in `ci-pre.yml` -- concurrency is
  per job; a workflow-level cancel group cancels a janitor sweep mid-delete.
- GEN-016: a `ci-pre.yml` job other than `cache-janitor` with its own
  `concurrency:`, or with `needs: cache-janitor` -- check jobs take no lock,
  so a queued sweep never delays a check.
- GEN-017: the `cache-janitor` job without the repo-wide, non-cancelling
  group (`group: cache-janitor`, `cancel-in-progress: false`).
- GEN-018: an install step in `ci-pre.yml` (setup-uv, setup-python, `uv`,
  `uvx`, `pip`, `pipx`) -- ci-pre runs stdlib `python3` only.
- CACHE-013: a cache save reachable from a PR (a workflow triggered by
  `pull_request` or `workflow_call`, or a composite action) whose key does
  not carry a delimited `pr-<N>` component: setup-soldr `cache-key-suffix`,
  setup-uv `cache-suffix`, or the sanctioned `actions/cache` wrapper's
  `key`. The value must reference `github.event.pull_request.number` or the
  plan output that carries it (`cache_key_pr`, `ci-lint plan
  --github-output`); inside a composite action an `inputs.*` passthrough is
  accepted (its caller is checked instead).
"""

from __future__ import annotations

from ci_lint import repo_identity

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import (
    as_dict,
    as_list,
    get_on_section,
    jobs_of,
    load_composite_actions,
    load_workflows,
    steps_of,
)
from ci_lint.yaml_io import LoadStatus, YamlValue

CI_PRE = "ci-pre.yml"
LEGACY_CI_PRE = "ci-precheck.yml"
JANITOR_JOB = "cache-janitor"
JANITOR_GROUP = "cache-janitor"

INSTALL_ACTIONS: frozenset[str] = frozenset({"astral-sh/setup-uv", "actions/setup-python"})
INSTALL_RUN_RE = re.compile(r"(?:^|[\s;&|(])(?:uv|uvx|pip|pip3|pipx)(?=\s|$)|-m\s+pip\b")

@dataclass(frozen=True)
class PrKeyInput:
    """A cache action, the input carrying its key, and the inputs any one of
    which disables saving when "false"."""

    action: str
    key_input: str
    save_inputs: tuple[str, ...]


@dataclass(frozen=True)
class _Step:
    loc: str
    step: dict[str, YamlValue]


@dataclass(frozen=True)
class _ScanFile:
    path: str
    is_composite: bool
    doc: dict[str, YamlValue]


# setup-uv: `enable-cache: false` disables the cache outright; `save-cache: false`
# keeps restore but never saves -- either way nothing PR-scoped is written.
PR_KEY_INPUTS: tuple[PrKeyInput, ...] = (
    *(PrKeyInput(r, "cache-key-suffix", ("save-cache",)) for r in repo_identity.SETUP_SOLDR_REPOS),
    PrKeyInput("astral-sh/setup-uv", "cache-suffix", ("enable-cache", "save-cache")),
    PrKeyInput("actions/cache", "key", ()),
    PrKeyInput("actions/cache/save", "key", ()),
)
PR_NUMBER_REFS: tuple[str, ...] = ("github.event.pull_request.number", "outputs.cache_key_pr")


def _slug(uses: str) -> str:
    return uses.split("@", 1)[0]


def check_gen_012(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    fix = f"rename it to {CI_PRE} (git mv), and update ci.yml's 'uses:' and ci.toml's [allow].workflows to match"
    for wf in load_workflows(repo_root):
        if Path(wf.path).name == LEGACY_CI_PRE:
            findings.append(
                Finding(rule="GEN-014", path=wf.path, message=f"workflow file is named '{LEGACY_CI_PRE}'", fix=fix)
            )
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            uses = job.get("uses")
            if isinstance(uses, str) and uses.endswith(f"/{LEGACY_CI_PRE}"):
                findings.append(
                    Finding(
                        rule="GEN-014",
                        path=wf.path,
                        message=f"jobs.{job_id} calls '{uses}' (the old name)",
                        fix=f"call ./.github/workflows/{CI_PRE} instead; {fix}",
                    )
                )
    if LEGACY_CI_PRE in ci.allow.workflows:
        findings.append(
            Finding(
                rule="GEN-014",
                path="ci.toml",
                message=f"[allow].workflows declares '{LEGACY_CI_PRE}'",
                fix=f'rename the key to "{CI_PRE}" in ci.toml\'s [allow].workflows (and the file itself)',
            )
        )
    return findings


def _needs(job: dict[str, YamlValue]) -> list[str]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return [raw]
    return [n for n in as_list(raw) if isinstance(n, str)]


def _janitor_group_ok(value: YamlValue) -> bool:
    if isinstance(value, str):
        return value == JANITOR_GROUP  # string form: cancel-in-progress defaults to false
    if not isinstance(value, dict):
        return False
    cancel = value.get("cancel-in-progress", False)
    return value.get("group") == JANITOR_GROUP and (cancel is False or cancel == "false")


def check_ci_pre_shape(repo_root: Path) -> list[Finding]:  # noqa: C901
    """GEN-015/014/015/016 over `.github/workflows/ci-pre.yml`."""

    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if Path(wf.path).name != CI_PRE or wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        if "concurrency" in doc:
            findings.append(
                Finding(
                    rule="GEN-015",
                    path=wf.path,
                    message=f"{CI_PRE} declares a workflow-level 'concurrency:' ({doc['concurrency']!r})",
                    fix="delete the workflow-level 'concurrency:' block; only the cache-janitor job takes a "
                    "lock ('concurrency: { group: cache-janitor, cancel-in-progress: false }'), and the "
                    "caller ci.yml may keep its own per-ref cancel group",
                )
            )
        for job_id, job in jobs_of(doc).items():
            if job_id == JANITOR_JOB:
                if not _janitor_group_ok(job.get("concurrency")):
                    findings.append(
                        Finding(
                            rule="GEN-017",
                            path=wf.path,
                            message=f"jobs.{JANITOR_JOB}.concurrency = {job.get('concurrency')!r} is not the "
                            "repo-wide, non-cancelling janitor group",
                            fix=f"set 'concurrency: {{ group: {JANITOR_GROUP}, cancel-in-progress: false }}' on "
                            f"jobs.{JANITOR_JOB} (no ref in the group: sweeps are serialized repo-wide and "
                            "never cancelled mid-delete)",
                        )
                    )
            else:
                if "concurrency" in job:
                    findings.append(
                        Finding(
                            rule="GEN-016",
                            path=wf.path,
                            message=f"jobs.{job_id} declares 'concurrency:' in {CI_PRE}",
                            fix=f"delete 'concurrency:' from jobs.{job_id}; only {JANITOR_JOB} is serialized, "
                            "check jobs run immediately and in parallel",
                        )
                    )
                if JANITOR_JOB in _needs(job):
                    findings.append(
                        Finding(
                            rule="GEN-016",
                            path=wf.path,
                            message=f"jobs.{job_id} has 'needs: {JANITOR_JOB}'",
                            fix=f"remove {JANITOR_JOB} from jobs.{job_id}.needs; a queued janitor sweep must "
                            "never delay a check",
                        )
                    )
            for i, step in enumerate(steps_of(job)):
                uses = step.get("uses")
                run = step.get("run")
                what: str | None = None
                if isinstance(uses, str) and _slug(uses) in INSTALL_ACTIONS:
                    what = f"uses {_slug(uses)}"
                elif isinstance(run, str) and INSTALL_RUN_RE.search(run):
                    what = f"runs {run.strip()!r}"
                if what is not None:
                    findings.append(
                        Finding(
                            rule="GEN-018",
                            path=wf.path,
                            message=f"jobs.{job_id}.steps[{i}] {what}: {CI_PRE} must install nothing",
                            fix=f"run this step with stdlib 'python3' only (e.g. 'python3 -m ci_lint ...' from "
                            f"the zackees/ci.yml checkout), or move it out of {CI_PRE} into a later ci.yml job",
                        )
                    )
    return findings


def _is_pr_reachable(doc: dict[str, YamlValue]) -> bool:
    on = get_on_section(doc)
    return "pull_request" in on or "workflow_call" in on or "pull_request_target" in on


def _iter_steps(doc: dict[str, YamlValue], is_composite: bool) -> list[_Step]:
    if is_composite:
        runs = as_dict(doc.get("runs"))
        return [_Step(f"runs.steps[{i}]", s) for i, s in enumerate(as_list(runs.get("steps"))) if isinstance(s, dict)]
    out: list[_Step] = []
    for job_id, job in jobs_of(doc).items():
        out.extend(_Step(f"jobs.{job_id}.steps[{i}]", s) for i, s in enumerate(steps_of(job)))
    return out


def carries_pr_number(value: YamlValue, *, is_composite: bool) -> bool:
    if not isinstance(value, str):
        return False
    if any(ref in value for ref in PR_NUMBER_REFS):
        return True
    return is_composite and "inputs." in value


def check_cache_010(ci: CiToml, repo_root: Path) -> list[Finding]:  # noqa: C901
    del ci  # the rule is ci.toml-independent; kept for the group-function signature
    files: list[_ScanFile] = []
    for wf in load_workflows(repo_root):
        if wf.status == LoadStatus.OK and _is_pr_reachable(as_dict(wf.document)):
            files.append(_ScanFile(wf.path, False, as_dict(wf.document)))
    for act in load_composite_actions(repo_root):
        if act.status == LoadStatus.OK:
            files.append(_ScanFile(act.path, True, as_dict(act.document)))

    findings: list[Finding] = []
    for scan in files:
        path, is_composite = scan.path, scan.is_composite
        for item in _iter_steps(scan.doc, is_composite):
            loc, step = item.loc, item.step
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            slug = _slug(uses)
            for pr_key in PR_KEY_INPUTS:
                action, key_input, save_inputs = pr_key.action, pr_key.key_input, pr_key.save_inputs
                if slug != action:
                    continue
                with_ = as_dict(step.get("with"))
                if any(str(with_.get(i, "")).strip().lower() == "false" for i in save_inputs):
                    continue  # this step never saves
                value = with_.get(key_input)
                if carries_pr_number(value, is_composite=is_composite):
                    continue
                findings.append(
                    Finding(
                        rule="CACHE-013",
                        path=path,
                        message=f"{loc}: {slug} can save a cache in PR context but '{key_input}' = {value!r} "
                        "has no 'pr-<N>' component",
                        fix=f"set '{key_input}' so PR saves carry a delimited pr-<N> key component, e.g. "
                        "'${{ needs.precheck.outputs.cache_key_pr }}' (the plan's 'cache_key_pr' output: "
                        "'pr-<N>' on pull_request, empty elsewhere) or "
                        "\"${{ github.event.pull_request.number && format('pr-{0}', "
                        "github.event.pull_request.number) || '' }}\"; the janitor deletes a closed PR's "
                        "entries by that component",
                    )
                )
    return findings


def check_group12(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_gen_012(ci, repo_root))
    findings.extend(check_ci_pre_shape(repo_root))
    findings.extend(check_cache_010(ci, repo_root))
    return findings
