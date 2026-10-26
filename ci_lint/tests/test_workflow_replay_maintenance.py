"""Remote cache maintenance is a dependency, never executed test proof."""

import copy
import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

from ci_lint.tests import test_workflow_replay as fixtures
from ci_lint.tests import test_gate_trust as trust_fixtures
from ci_lint.attestations import make
from ci_lint.local_gate_cli import _job_decisions
from ci_lint.gate_trust import TrustInput
from ci_lint.workflow_replay import prove_replay
from ci_lint.workflow_replay_config import DeclaredReplayJob, ReplayConfig
from ci_lint.workflow_replay_static import check_replay_static
from ci_lint.workflow_replay_maintenance import non_attestable_jobs, prove_maintenance
from ci_lint.tests.test_workflow_replay_expansion import workflow
from ci_lint.workflow_replay_checks import derive_checks
from ci_lint.workflow_replay_expansion import ExpandedJob
from ci_lint.workflow_replay_identity import identity_part
from ci_lint.workflow_replay_runtime import run_checked_command
from ci_lint.tests.helpers import requires_yaml_tooling


class MaintenanceDependencyTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkflowReplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.raw = fixture.raw
        self.workspace = fixture.workspace
        executed = replace(fixture.expected.required_jobs[0], identity=(identity_part("tests"),))
        jobs = self.raw["tree"]["groups"][0]["jobs"]
        jobs[0].update(job_id="tests", matrix=None, identity=[{"jobID": "tests", "matrix": None}])
        self.source = {
            "env": {"CI_REMOTE_ONLY": "Audits live GitHub caches"},
            "steps": [
                {"name": "Checkout", "uses": "actions/checkout@" + "a" * 40},
                {"name": "Checkout policy", "uses": "actions/checkout@" + "a" * 40,
                 "with": {"repository": "zackees/ci.yml", "ref": "b" * 40, "path": ".ci-lint",
                          "persist-credentials": False}},
                {"name": "Cache budget", "env": {"PYTHONPATH": ".ci-lint"},
                 "run": "python3 -P -m ci_lint cache budget --repo ."},
            ],
        }
        self.expanded = ExpandedJob("ci.yml:budget", "Budget", fixture.expected.workflow, {}, self.source,
                                    identity=(identity_part("budget"),))
        maintenance = derive_checks(self.expanded, mode="minimal", event="pull_request")
        self.expected = replace(fixture.expected, required_jobs=(executed, maintenance))
        self.maintenance = {
            "key": "Budget", "job_id": "budget", "matrix": None,
            "identity": [{"jobID": "budget", "matrix": None}],
            "status": "completed", "conclusion": "remote_only",
            "sections": [{"id": "0", "name": "Remote-only: runs on GitHub, not under act (GATE-012)",
                          "stage": "Main", "status": "completed", "conclusion": "success",
                          "first_seq": 21, "last_seq": 22}],
        }
        jobs.append(self.maintenance)

    @requires_yaml_tooling
    def test_deferred_child_validates_maintenance_in_the_complete_graph(self):
        workflows = self.workspace / ".github/workflows"
        workflows.mkdir(parents=True)
        document = {"jobs": {
            "budget": self.source,
            "plan": {"outputs": {"test": "${{ steps.plan.outputs.test }}"},
                     "steps": [{"name": "Plan", "run": "plan"}]},
            "tests": {"needs": ["plan", "budget"], "steps": [
                {"name": "Run tests", "run": "tests", "if": "needs.plan.outputs.test == 'true'"}]},
        }}
        (workflows / "ci.yml").write_text(json.dumps(document))
        jobs = self.raw["tree"]["groups"][0]["jobs"]
        planner = copy.deepcopy(jobs[0])
        planner.update(job_id="plan", identity=[{"jobID": "plan", "matrix": None}],
                       output_evidence={"schema_version": 1, "seq": 21,
                                        "values": {"test": "true"}, "error": None})
        planner["sections"][0]["name"] = "Plan"
        jobs.append(planner)
        config = ReplayConfig("owner/repo", self.expected.workflow, "minimal", tuple(
            DeclaredReplayJob("ci.yml:" + name, self.expected.required_jobs[0], (), True)
            for name in ("plan", "budget", "tests")), qualified=True, report_source="stdout")
        fixture = self.workspace / "deferred.json"
        child = self.workspace / "child.py"
        child.write_text("from pathlib import Path\nprint(Path(" + repr(str(fixture)) + ").read_text())\n")
        fixture.write_text(json.dumps(self.raw))
        outcome = run_checked_command(self.workspace, (sys.executable, str(child)), config,
                                      head=self.expected.sha, tree=self.expected.git_tree)
        self.assertEqual(outcome.returncode, 0, outcome.error)
        for change in ({"sections": []}, {"conclusion": "success"},
                       {"output_evidence": {"schema_version": 1, "seq": 23,
                                            "values": {"test": "true"}, "error": None}}):
            with self.subTest(change=change):
                refused = copy.deepcopy(self.raw)
                refused["tree"]["groups"][0]["jobs"][1].update(change)
                fixture.write_text(json.dumps(refused))
                result = run_checked_command(self.workspace, (sys.executable, str(child)), config,
                                             head=self.expected.sha, tree=self.expected.git_tree)
                self.assertEqual(result.returncode, 1, "final proof must validate maintenance")

    def test_source_bound_maintenance_is_not_returned_as_test_proof(self):
        proof = prove_replay(self.raw, self.expected)
        self.assertEqual(proof.jobs, (self.expected.required_jobs[0].key,))
        self.assertEqual(proof.outputs, ())

    def test_published_provider_retains_unexecuted_original_steps(self):
        path = Path(__file__).parent / "fixtures/replay-remote-maintenance-sections.json"
        fixture = json.loads(path.read_text())
        prove_maintenance(fixture["job"], "precheck/ci-pre.yml/cache-budget",
                          tuple(fixture["source_steps"]))

    def test_original_step_skip_requires_source_and_no_execution_evidence(self):
        path = Path(__file__).parent / "fixtures/replay-remote-maintenance-sections.json"
        fixture = json.loads(path.read_text())
        with self.assertRaises(ValueError):
            prove_maintenance(fixture["job"], "budget")
        for field, value in (("name", "Unrelated test"), ("id", "0"),
                             ("status", "running"), ("conclusion", "success"),
                             ("first_seq", 100), ("last_seq", 101), ("exit_code", 0),
                             ("started_at", "2026-10-07T14:31:01Z"), ("duration_ms", 1)):
            with self.subTest(field=field):
                job = copy.deepcopy(fixture["job"])
                job["sections"][2][field] = value
                with self.assertRaises(ValueError):
                    prove_maintenance(job, "budget", tuple(fixture["source_steps"]))
        for field in ("id", "name"):
            job = copy.deepcopy(fixture["job"])
            job["sections"][3][field] = job["sections"][2][field]
            with self.subTest(duplicate=field), self.assertRaises(ValueError):
                prove_maintenance(job, "budget", tuple(fixture["source_steps"]))

    def test_original_skips_do_not_supply_test_or_output_credit(self):
        job = copy.deepcopy(self.maintenance)
        job["sections"].append({"id": "1", "name": self.expected.required_jobs[1].steps[1],
                                "stage": "Main", "status": "completed", "conclusion": "skipped"})
        self.raw["tree"]["groups"][0]["jobs"][1] = job
        proof = prove_replay(self.raw, self.expected)
        self.assertEqual(proof.jobs, (self.expected.required_jobs[0].key,))
        self.assertEqual(proof.outputs, ())

    def test_maintenance_alone_cannot_qualify(self):
        with self.assertRaises(ValueError):
            prove_replay(self.raw, replace(self.expected, required_jobs=self.expected.required_jobs[1:]))

    def test_maintenance_cannot_produce_planner_outputs(self):
        self.maintenance["output_evidence"] = {
            "schema_version": 1, "seq": 23, "values": {"matrix": "[]"}, "error": None,
        }
        with self.assertRaises(ValueError):
            prove_replay(self.raw, self.expected)

    def test_missing_failed_or_ambiguous_stub_refuses(self):
        for field, value in (
            ("conclusion", "success"), ("conclusion", "failure"), ("status", "running"),
            ("sections", []), ("sections", self.maintenance["sections"] * 2),
        ):
            with self.subTest(field=field, value=value):
                raw = copy.deepcopy(self.raw)
                raw["tree"]["groups"][0]["jobs"][1][field] = value
                with self.assertRaises(ValueError):
                    prove_replay(raw, self.expected)

    def test_a_marker_cannot_waive_an_ordinary_test(self):
        self.source["steps"][2]["run"] = "python3 -m pytest"
        required = derive_checks(self.expanded, mode="minimal", event="pull_request")
        with self.assertRaises(ValueError):
            prove_replay(self.raw, replace(self.expected, required_jobs=(self.expected.required_jobs[0], required)))

    def test_source_output_declaration_is_not_maintenance(self):
        self.source["outputs"] = {"matrix": "${{ steps.cache.outputs.matrix }}"}
        required = derive_checks(self.expanded, mode="minimal", event="pull_request")
        with self.assertRaises(ValueError):
            prove_replay(self.raw, replace(self.expected, required_jobs=(self.expected.required_jobs[0], required)))

    def test_a_second_shell_command_cannot_be_hidden_in_maintenance(self):
        self.source["steps"][2]["run"] = "python3 -P -m ci_lint cache budget --repo .\npython3 -m pytest"
        required = derive_checks(self.expanded, mode="minimal", event="pull_request")
        with self.assertRaises(ValueError):
            prove_replay(self.raw, replace(self.expected, required_jobs=(self.expected.required_jobs[0], required)))

    def test_only_declared_cache_commands_and_checkout_setup_are_maintenance(self):
        for alteration in (
            {"run": "python3 -P -m ci_lint cache audit --repo ."},
            {"run": "python3 -P -m ci_lint cache budget --repo . && pytest"},
            {"run": "python3 -P -m ci_lint cache budget --repo $(pwd)"},
            {"run": "python3 -P -m ci_lint cache budget --repo .", "shell": "python"},
            {"uses": "actions/cache@" + "a" * 40},
        ):
            with self.subTest(alteration=alteration):
                source = {**self.source, "steps": [self.source["steps"][0], {"name": "Cache budget", **alteration}]}
                required = derive_checks(replace(self.expanded, job=source), mode="minimal", event="pull_request")
                with self.assertRaises(ValueError):
                    prove_replay(self.raw, replace(self.expected, required_jobs=(self.expected.required_jobs[0], required)))

    def test_executed_stub_sequence_stage_and_identity_remain_required(self):
        for field, value in (("id", "other"), ("name", "Cache budget"), ("stage", "Post"),
                             ("first_seq", True), ("last_seq", 20), ("conclusion", "failure")):
            with self.subTest(field=field):
                raw = copy.deepcopy(self.raw)
                raw["tree"]["groups"][0]["jobs"][1]["sections"][0][field] = value
                with self.assertRaises(ValueError):
                    prove_replay(raw, self.expected)
        self.maintenance["identity"][0]["jobID"] = "other"
        with self.assertRaises(ValueError):
            prove_replay(self.raw, self.expected)

    def test_remote_maintenance_and_its_reusable_callers_cannot_be_attested(self):
        document = workflow(self.expected.workflow, {"jobs": {
            "tests": {"steps": [{"name": "Test", "run": "tests"}]}, "budget": self.source,
        }})
        config = ReplayConfig("owner/repo", document.path, "minimal", tuple(
            DeclaredReplayJob("ci.yml:" + job, self.expected.required_jobs[index], (), derive_checks=True)
            for index, job in enumerate(("tests", "budget"))), qualified=True)
        with tempfile.TemporaryDirectory() as scratch:
            repo = Path(scratch)
            for mapped, refuses in (("ci.yml:tests", False), ("ci.yml:budget", True)):
                (repo / "ci-attestations.yml").write_text(
                    "version: 1\ngates:\n  general/all/test: {lane: unit}\njobs:\n  " + mapped + ": [general/all/test]\n")
                with patch("ci_lint.workflow_replay_static.load_workflows", return_value=(document,)):
                    self.assertEqual(bool(check_replay_static(config, repo)), refuses)
        child = workflow(".github/workflows/child.yml", {"jobs": {"budget": self.source}})
        middle = workflow(".github/workflows/middle.yml", {"jobs": {"call": {"uses": "./.github/workflows/child.yml"}}})
        parent = workflow(self.expected.workflow, {"jobs": {"precheck": {"uses": "./.github/workflows/middle.yml"}}})
        self.assertEqual(non_attestable_jobs((parent, middle, child)),
                         frozenset({"ci.yml:precheck", "middle.yml:call", "child.yml:budget"}))

    def test_interpreter_and_module_provenance_are_bound(self):
        for change in (
            'source["env"]["PYTHONPATH"] = "repository-shim"',
            'source["env"]["PATH"] = "repository-bin"',
            'source["steps"][2]["env"]["PYTHONPATH"] = "repository-shim"',
            'source["steps"][2]["working-directory"] = "repository-shim"',
            'source["steps"][1]["with"]["repository"] = "owner/shim"',
            'source["steps"][1]["with"]["ref"] = "main"',
            'source["steps"][1]["with"]["path"] = "repository-shim"',
        ):
            with self.subTest(change=change):
                source = copy.deepcopy(self.source)
                exec(change)
                required = derive_checks(replace(self.expanded, job=source), mode="minimal", event="pull_request")
                with self.assertRaises(ValueError):
                    prove_replay(self.raw, replace(self.expected, required_jobs=(self.expected.required_jobs[0], required)))

    @requires_yaml_tooling
    def test_hosted_decision_refuses_base_remote_job_even_if_local_checks_bypassed(self):
        fixture = trust_fixtures.TrustCase()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        definition = "version: 1\ngates:\n  rust/all/lint: {lane: lint}\njobs:\n  ci.yml:lint: [rust/all/lint]\n"
        marked = trust_fixtures.WORKFLOW.replace("  lint:\n", "  lint:\n    env: {CI_REMOTE_ONLY: live service}\n")
        base = fixture.commit("base remote mapping", {
            "local-gate.toml": trust_fixtures._gate(trust_fixtures.TRUST, lanes=True),
            "ci-attestations.yml": definition, ".github/workflows/ci.yml": marked,
        }, lanes=None)
        fixture.commit("feature", {"src.txt": "changed"})
        tree = trust_fixtures._git(fixture.repo, "rev-parse", "HEAD^{tree}")
        trailer = make("rust/all/lint", tree=tree, parents=(base,), lane="lint", key="k", via="run", secs=1).trailer()
        trust_fixtures._git(fixture.repo, "commit", "-q", "--amend", "-m", "feature\n\n" + trailer)
        head = trust_fixtures._git(fixture.repo, "rev-parse", "HEAD")
        # The checkout cannot hide the marker committed on the trusted base.
        (fixture.repo / ".github/workflows/ci.yml").write_text(trust_fixtures.WORKFLOW)
        inp = TrustInput("pull_request", head, base, "OWNER", "o/r", "o/r", ())
        decisions = {item.job: item for item in _job_decisions(fixture.repo, inp, True)}
        self.assertFalse(decisions["ci.yml:lint"].skip)
        self.assertIn("remote-only work", decisions["ci.yml:lint"].reason)

    def test_python_safe_path_and_workflow_environment_are_required(self):
        for source, document in (
            ({**self.source, "steps": [*self.source["steps"][:2],
                {**self.source["steps"][2], "run": "python3 -m ci_lint cache budget --repo ."}]}, {}),
            (self.source, {"env": {"PATH": "repository-bin"}}),
            (self.source, {"defaults": {"run": {"working-directory": "repository-shim"}}}),
        ):
            required = derive_checks(replace(self.expanded, job=source, document=document),
                                     mode="minimal", event="pull_request")
            with self.assertRaises(ValueError):
                prove_replay(self.raw, replace(self.expected, required_jobs=(self.expected.required_jobs[0], required)))
