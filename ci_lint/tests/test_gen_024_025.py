"""GEN-024/GEN-025 -- ci_lint/rules/needs_graph.py (zackees/ci.yml#83, #86)."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.needs_graph import check_gen_024, check_gen_025
from pathlib import Path

from ci_lint.rules.needs_graph import check_needs_graph
from ci_lint.workflow_scan import ParsedYamlFile
from ci_lint.yaml_io import LoadStatus
from ci_lint.tests.helpers import requires_yaml_tooling

GATE = {"runs-on": "ubuntu-latest", "outputs": {"mode": "x"}, "steps": [{"run": "python ci/ci_mode.py"}]}


def _wf(jobs: dict) -> list[ParsedYamlFile]:
    return [ParsedYamlFile(path=".github/workflows/ci.yml", document={"jobs": jobs}, status=LoadStatus.OK, reason=None)]


def _job(runs_on: str, steps: list, **extra: object) -> dict:
    return {"runs-on": runs_on, "needs": ["gate"], "steps": steps, **extra}


class Gen024Test(unittest.TestCase):
    def test_linux_job_using_only_mode_is_violation(self) -> None:
        job = _job("ubuntu-24.04", [{"run": "test ${{ needs.gate.outputs.mode }} = full"}])
        f = check_gen_024(_wf({"gate": GATE, "test": job}))
        self.assertEqual([(x.rule, x.status) for x in f], [("GEN-024", Status.VIOLATION)])

    def test_non_linux_and_arm_are_exempt(self) -> None:
        steps = [{"run": "echo ${{ needs.gate.outputs.sha }}"}]
        jobs = {"gate": GATE, "mac": _job("macos-latest", steps), "arm": _job("ubuntu-24.04-arm", steps)}
        self.assertEqual(check_gen_024(_wf(jobs)), [])

    def test_artifact_or_other_output_is_exempt(self) -> None:
        dl = _job("ubuntu-latest", [{"uses": "actions/download-artifact@v4"}, {"run": "echo ${{ needs.gate.outputs.mode }}"}])
        other = _job("ubuntu-latest", [{"run": "echo ${{ needs.gate.outputs.matrix }}"}])
        self.assertEqual(check_gen_024(_wf({"gate": GATE, "dl": dl, "other": other})), [])

    def test_cache_or_no_reference_is_needs_review(self) -> None:
        cache = _job("ubuntu-latest", [{"uses": "actions/cache@v4"}, {"run": "echo ${{ needs.gate.outputs.mode }}"}])
        bare = _job("ubuntu-latest", [{"run": "make"}])
        f = check_gen_024(_wf({"gate": GATE, "cache": cache, "bare": bare}))
        self.assertEqual([x.status for x in f], [Status.NEEDS_REVIEW, Status.NEEDS_REVIEW])

    def test_cache_writer_after_preprune_job_is_exempt(self) -> None:
        maint = {"runs-on": "ubuntu-latest", "steps": [{"run": "python3 -m ci_lint cache preprune --repo ."}]}
        writer = {**_job("ubuntu-latest", [{"uses": "actions/cache@v4"}, {"run": "make"}]), "needs": ["maint"]}
        self.assertEqual(check_gen_024(_wf({"maint": maint, "writer": writer})), [])

    def test_job_named_cache_maint_without_prune_command_is_not_exempt(self) -> None:
        maint = {"runs-on": "ubuntu-latest", "steps": [{"run": "echo hi"}]}
        writer = {**_job("ubuntu-latest", [{"uses": "actions/cache@v4"}, {"run": "make"}]), "needs": ["cache-maint"]}
        f = check_gen_024(_wf({"cache-maint": maint, "writer": writer}))
        self.assertEqual([x.status for x in f], [Status.NEEDS_REVIEW])

    def test_end_of_queue_janitor_is_exempt(self) -> None:
        janitor = {"runs-on": "ubuntu-latest", "needs": ["gate"], "steps": [{"run": "ci-lint cache janitor --repo ."}]}
        self.assertEqual(check_gen_024(_wf({"gate": GATE, "janitor": janitor})), [])

    @requires_yaml_tooling
    def test_fixture_repo_flags_only_the_cache_less_ordering_edge(self) -> None:
        root = Path(__file__).parent / "fixtures" / "GEN-024" / "legit-edges"
        f = [x for x in check_needs_graph(root) if x.rule == "GEN-024"]
        self.assertEqual(len(f), 1)
        self.assertIn("'ordering-only'", f[0].message)


def _agg(needs: list[str], run: str = "test '${{ needs.a.result }}' = success") -> dict:
    return {"runs-on": "ubuntu-latest", "needs": needs, "if": "always()", "steps": [{"run": run}]}


WORK = {"runs-on": "ubuntu-latest", "steps": [{"uses": "actions/checkout@v4"}, {"run": "make"}]}


class Gen025Test(unittest.TestCase):
    def test_sentinel_over_plain_jobs_is_needs_review(self) -> None:
        f = check_gen_025(_wf({"a": WORK, "b": WORK, "sentinel": _agg(["a", "b"])}))
        self.assertEqual([(x.rule, x.status) for x in f], [("GEN-025", Status.NEEDS_REVIEW)])

    def test_ci_lint_gate_aggregator_is_exempt(self) -> None:
        agg = _agg(["a"], run="python3 -m ci_lint gate --needs '${{ toJSON(needs) }}'")
        self.assertEqual(check_gen_025(_wf({"a": WORK, "ci-ok": agg})), [])

    def test_plan_dependent_set_is_exempt(self) -> None:
        cond = {**WORK, "if": "needs.plan.outputs.full == 'true'"}
        matrix = {**WORK, "strategy": {"matrix": {"os": ["x", "y"]}}}
        self.assertEqual(check_gen_025(_wf({"a": cond, "sentinel": _agg(["a"])})), [])
        self.assertEqual(check_gen_025(_wf({"a": matrix, "sentinel": _agg(["a"])})), [])

    def test_work_job_or_non_trailing_is_not_aggregate(self) -> None:
        work = {**WORK, "needs": ["a"]}
        mid = _agg(["a"])
        self.assertEqual(check_gen_025(_wf({"a": WORK, "w": work})), [])
        self.assertEqual(check_gen_025(_wf({"a": WORK, "mid": mid, "z": {**WORK, "needs": ["mid"]}})), [])


if __name__ == "__main__":
    unittest.main()
