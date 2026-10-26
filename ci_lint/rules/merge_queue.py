"""GEN-022: merge queue and end-of-queue cache janitor (zackees/ci.yml#405).

Every repository merges to its default branch through a merge queue, so a
workflow must trigger on `merge_group`. When `[cache].budget` is declared,
that workflow ends with the cache janitor: a job that runs `cache janitor`,
depends (directly or transitively) on every other job of the workflow, runs
on `always()`, and holds the repo-wide non-cancelling `cache-janitor`
group. The queue serializes default-branch cache writes, so that job has
exclusive access and evicts by LRU itself before GitHub's eviction can.

The #409 `cancel-superseded` job has no `needs:` and nothing depends on it
except the janitor: the janitor must run after every job, so it is the one
sanctioned dependent; any other job waiting on the cancel step would delay
its own start for no benefit and is a violation.
"""

from __future__ import annotations

from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.cache_static import parse_size
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import as_dict, as_list, get_on_section, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "GEN-022"
JANITOR_GROUP = "cache-janitor"
CANCEL_JOB = "cancel-superseded"


def _needs(job: dict[str, YamlValue]) -> set[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return {needs}
    return {str(n) for n in as_list(needs)}


def _upstream(job_id: str, graph: dict[str, frozenset[str]]) -> set[str]:
    seen: set[str] = set()
    todo = list(graph.get(job_id, ()))
    while todo:
        cur = todo.pop()
        if cur not in seen:
            seen.add(cur)
            todo.extend(graph.get(cur, ()))
    return seen


def _runs_janitor(job: dict[str, YamlValue]) -> bool:
    return any("cache janitor" in str(step.get("run", "")) for step in steps_of(job))


def _janitor_problems(job_id: str, job: dict[str, YamlValue], graph: dict[str, frozenset[str]]) -> list[str]:
    problems = []
    missing = sorted(set(graph) - {job_id} - _upstream(job_id, graph))
    if missing:
        problems.append(f"does not run after every other job (missing needs: {', '.join(missing)})")
    if "always()" not in str(job.get("if", "")):
        problems.append("has no `always()` condition, so a failed job skips the sweep")
    concurrency = as_dict(job.get("concurrency"))
    if concurrency.get("group") != JANITOR_GROUP or concurrency.get("cancel-in-progress") is not False:
        problems.append(f"lacks `concurrency: {{group: {JANITOR_GROUP}, cancel-in-progress: false}}`")
    return problems


def _cancel_edge_findings(path: str, document: YamlValue) -> list[Finding]:
    jobs = jobs_of(as_dict(document))
    if CANCEL_JOB not in jobs:
        return []
    findings: list[Finding] = []
    if _needs(jobs[CANCEL_JOB]):
        findings.append(Finding(
            rule=RULE, path=path, message=f"job '{CANCEL_JOB}' has `needs:`, so it cannot run first",
            fix=f"drop `needs:` from '{CANCEL_JOB}'",
        ))
    for job_id, job in jobs.items():
        if CANCEL_JOB in _needs(job) and not _runs_janitor(job):
            findings.append(Finding(
                rule=RULE, path=path,
                message=f"job '{job_id}' needs '{CANCEL_JOB}'; only the end-of-queue cache janitor may depend on it",
                fix=f"remove '{CANCEL_JOB}' from '{job_id}'.needs",
            ))
    return findings


def check_gen_022(ci: CiToml, repo_root: Path) -> list[Finding]:
    workflows = [wf for wf in load_workflows(repo_root) if wf.status == LoadStatus.OK]
    if not workflows:
        return []
    queued = [wf for wf in workflows if "merge_group" in get_on_section(as_dict(wf.document))]
    if not queued:
        return [Finding(
            rule=RULE, path=".github/workflows",
            message="no workflow triggers on `merge_group`: the default branch must merge through a merge queue",
            fix="add `merge_group:` to the required CI workflow's triggers and enable the merge queue "
            "in the default branch's ruleset",
        )]
    findings = [f for wf in queued for f in _cancel_edge_findings(wf.path, wf.document)]
    if not parse_size(ci.cache.budget):
        return findings
    for wf in queued:
        jobs = jobs_of(as_dict(wf.document))
        janitors = [job_id for job_id, job in jobs.items() if _runs_janitor(job)]
        if not janitors:
            findings.append(Finding(
                rule=RULE, path=wf.path,
                message="the merge queue workflow does not end with the cache janitor, so GitHub picks "
                "what to evict once the repository is over [cache].budget",
                fix="add a final job that needs every other job, runs on `always()`, holds the "
                f"`{JANITOR_GROUP}` group without cancelling, and runs `ci-lint cache janitor`",
            ))
            continue
        graph = {job_id: frozenset(_needs(job)) for job_id, job in jobs.items()}
        for job_id in janitors:
            for problem in _janitor_problems(job_id, jobs[job_id], graph):
                findings.append(Finding(
                    rule=RULE, path=wf.path, message=f"cache janitor job '{job_id}' {problem}",
                    fix="make the janitor the merge queue run's final job: `needs:` every other job, "
                    f"`if: always()`, `concurrency: {{group: {JANITOR_GROUP}, cancel-in-progress: false}}`",
                ))
    return findings
