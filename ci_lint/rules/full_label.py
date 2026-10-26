"""GEN-031: a full-matrix PR label fires once per application (forge-ci/ci.yml#364).

Evidence: FastLED/FastLED, 2026-10-05..07 -- one agent session applied the
`ci-full` label (90 workflows, ~121 checks per head SHA) to almost every PR.
Because 95 of FastLED's 97 label-gated workflows trigger on `synchronize`
as well as `labeled`, the sticky label re-ran the full matrix on every push,
and label toggling "to re-trigger" fired it again: >= 6,678 runs and
>= 443 runner-hours on 11 PRs in ~30 h, 1,820 of the runs cancelled.

Static signal, per workflow whose `pull_request`/`pull_request_target`
trigger lists `labeled` in `types:` and whose text gates on a full-matrix
label (a label name matching `ci-full*` or `full-ci`, read from a
`labels.*.name, '<label>'` / `label.name == '<label>'` expression):

- violation: neither the workflow nor any job declares a `concurrency`
  group with `cancel-in-progress` (absent or literally false). A label
  re-add or a new push must cancel the superseded matrix.
- violation (one per label per repository): the trigger also lists
  `synchronize` and no workflow in the repository removes that label
  (`--remove-label`, `remove-label(s)` actions, `removeLabel`, or a
  `labels/<label>` REST call). A sticky label re-runs the full matrix on
  every push; the label must be consumed by the run it triggered.

Out of scope (decided policy, not mechanical): whether the PR's diff
qualifies for full coverage ("Selecting full PR coverage"), and the per-PR
cap on full-matrix runs across label applications.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.workflow_scan import as_dict, get_on_section, jobs_of, load_workflows
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "GEN-031"
_LABEL_EXPR_RE = re.compile(r"label(?:s\.\*)?\.name\s*(?:,|==)\s*['\"]([^'\"]+)['\"]")
_FULL_LABEL_RE = re.compile(r"^(ci-full[a-z0-9:_-]*|full-ci)$", re.IGNORECASE)
_PR_EVENTS = ("pull_request", "pull_request_target")


@dataclass(frozen=True)
class LabelGatedWorkflow:
    path: str
    labels: tuple[str, ...]
    on_synchronize: bool
    cancels: bool


@dataclass(frozen=True)
class LabelScan:
    gated: tuple[LabelGatedWorkflow, ...]
    corpus: str


def _types(on: dict[str, YamlValue]) -> frozenset[str]:
    out: set[str] = set()
    for event in _PR_EVENTS:
        if event not in on:
            continue
        spec = on[event]
        types = spec.get("types") if isinstance(spec, dict) else None
        if isinstance(types, list):
            out.update(str(t) for t in types)
        elif isinstance(types, str):
            out.add(types)
        else:
            out.update(("opened", "synchronize", "reopened"))
    return frozenset(out)


def _cancels(concurrency: YamlValue) -> bool:
    if not isinstance(concurrency, dict):
        return False
    value = concurrency.get("cancel-in-progress")
    if value is None or value is False:
        return False
    return str(value).strip().lower() != "false"


def _gated(repo_root: Path) -> LabelScan:
    gated: list[LabelGatedWorkflow] = []
    corpus: list[str] = []
    for wf in load_workflows(repo_root):
        try:
            text = (repo_root / wf.path).read_text(encoding="utf-8")
        except OSError:
            continue
        corpus.append(text)
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        types = _types(get_on_section(doc))
        if "labeled" not in types:
            continue
        labels = tuple(sorted({m for m in _LABEL_EXPR_RE.findall(text) if _FULL_LABEL_RE.match(m)}))
        if not labels:
            continue
        cancels = _cancels(doc.get("concurrency")) or any(
            _cancels(job.get("concurrency")) for job in jobs_of(doc).values()
        )
        gated.append(LabelGatedWorkflow(wf.path, labels, "synchronize" in types, cancels))
    return LabelScan(tuple(gated), "\n".join(corpus))


def _removes(corpus: str, label: str) -> bool:
    if re.search(r"remove-labels?\b|removeLabel", corpus) is None:
        return re.search(rf"labels/{re.escape(label)}\b", corpus) is not None
    return True


def check_gen_031(repo_root: Path) -> list[Finding]:
    scan = _gated(repo_root)
    gated, corpus = scan.gated, scan.corpus
    findings: list[Finding] = []
    for wf in gated:
        if not wf.cancels:
            findings.append(
                Finding(
                    rule=RULE,
                    path=wf.path,
                    message=f"workflow gated on full-matrix label {', '.join(wf.labels)} runs on `labeled` with "
                    "no `concurrency` group that cancels in progress; a label re-add or push stacks a second "
                    "full matrix",
                    fix="add `concurrency: {group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: "
                    "${{ github.event_name == 'pull_request' }}}`",
                )
            )
    sticky: dict[str, str] = {}
    for wf in gated:
        if wf.on_synchronize:
            for label in wf.labels:
                sticky.setdefault(label, wf.path)
    for label, path in sorted(sticky.items()):
        if _removes(corpus, label):
            continue
        count = sum(1 for wf in gated if wf.on_synchronize and label in wf.labels)
        findings.append(
            Finding(
                rule=RULE,
                path=path,
                message=f"full-matrix label '{label}' gates {count} workflow(s) that also run on `synchronize`, "
                "and no workflow removes the label; it re-runs the full matrix on every push",
                fix=f"remove '{label}' once its run starts (e.g. a step running `gh pr edit <n> --remove-label "
                f"{label}`), or drop `synchronize` from the label-gated triggers so one application = one run",
            )
        )
    return findings
