"""Adversarial structured evidence for GATE-001 workflow replay (#289).

These fixtures test validation; they are not evidence of an executed gate.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from dataclasses import dataclass, replace

from ci_lint.cargo_messages import JsonValue
from ci_lint.workflow_replay import ReplayExpectation, ReplayInput, ReplayJob, prove_replay
from ci_lint.workflow_replay_identity import identity_part
from ci_lint.workflow_replay_checks import derive_checks
from ci_lint.workflow_replay_expansion import ExpandedJob


@dataclass(frozen=True)
class MetadataChange:
    field: str
    value: JsonValue


class WorkflowReplayTest(unittest.TestCase):
    def test_outputs_are_available_only_after_the_qualified_job_is_proved(self) -> None:
        expected = replace(self.expected, required_jobs=(replace(self.expected.required_jobs[0],
                           identity=(identity_part("tests"),)),))
        job = self.raw["tree"]["groups"][0]["jobs"][0]
        job.update(job_id="tests", matrix=None, identity=[{"jobID": "tests", "matrix": None}],
                   output_evidence={"schema_version": 1, "seq": 21,
                                    "values": {"matrix": "[]"}, "error": None})
        proof = prove_replay(self.raw, expected)
        self.assertEqual(len(proof.outputs), 1)
        self.assertEqual(proof.outputs[0].identity, expected.required_jobs[0].identity)
        self.assertEqual(proof.outputs[0].name, "matrix")
        self.assertEqual(proof.outputs[0].value, "[]")
        self.raw["act_version"] = "0.2.89-act2.12"
        with self.assertRaises(ValueError):
            prove_replay(self.raw, expected)
        self.raw["act_version"] = "0.2.89-act2.13"
        job["sections"][0]["conclusion"] = "skipped"
        with self.assertRaises(ValueError):
            prove_replay(self.raw, expected)

    def test_output_evidence_refuses_invalid_shape_and_unproved_sequence(self) -> None:
        expected = replace(self.expected, required_jobs=(replace(self.expected.required_jobs[0],
                           identity=(identity_part("tests"),)),))
        job = self.raw["tree"]["groups"][0]["jobs"][0]
        job.update(job_id="tests", matrix=None, identity=[{"jobID": "tests", "matrix": None}])
        for change in (
            MetadataChange("schema_version", True), MetadataChange("schema_version", 2),
            MetadataChange("seq", True), MetadataChange("seq", 20),
            MetadataChange("error", "duplicate-event"), MetadataChange("values", {}),
            MetadataChange("values", {"matrix": True}), MetadataChange("values", {"1matrix": "[]"}),
            MetadataChange("values", {"matrix": "x" * 65537}), MetadataChange("extra", "unknown"),
        ):
            evidence = {"schema_version": 1, "seq": 21, "values": {"matrix": "[]"}, "error": None}
            evidence[change.field] = change.value
            job["output_evidence"] = evidence
            with self.subTest(field=change.field, value=str(change.value)[:80]):
                with self.assertRaises(ValueError):
                    prove_replay(self.raw, expected)

    def test_unqualified_output_evidence_cannot_be_exposed(self) -> None:
        job = self.raw["tree"]["groups"][0]["jobs"][0]
        job["output_evidence"] = {"schema_version": 1, "seq": 21,
                                  "values": {"matrix": "[]"}, "error": None}
        with self.assertRaises(ValueError):
            prove_replay(self.raw, self.expected)

    def test_qualified_identity_never_matches_display_name_alone(self) -> None:
        expected = replace(self.expected, required_jobs=(ReplayJob("diagnostic", ("Run tests",), identity=(
            identity_part("caller", {"target": "linux"}), identity_part("tests", {"shard": 1}))),))
        raw = copy.deepcopy(self.raw)
        job = raw["tree"]["groups"][0]["jobs"][0]
        job.update(key="Same display name", job_id="tests", matrix={"shard": 1}, identity=[
            {"jobID": "caller", "matrix": {"target": "linux"}},
            {"jobID": "tests", "matrix": {"shard": 1}},
        ])
        self.assertEqual(prove_replay(raw, expected).jobs, ("diagnostic",))
        for change in (
            'job.pop("identity")',
            'job["identity"][0]["jobID"] = "other"',
            'job["identity"][0]["matrix"] = {"target": "windows"}',
            'job["identity"][1]["matrix"] = {"shard": 2}',
            'job["job_id"] = "other"',
            'job["matrix"] = {"shard": 2}',
        ):
            changed = copy.deepcopy(raw)
            job = changed["tree"]["groups"][0]["jobs"][0]
            with self.subTest(change=change), self.assertRaises(ValueError):
                exec(change)
                prove_replay(changed, expected)

    def test_qualified_same_names_require_every_execution(self) -> None:
        expected = replace(self.expected, required_jobs=tuple(
            ReplayJob("Same", ("Run tests",), identity=(identity_part(caller), identity_part("tests")))
            for caller in ("first", "second")))
        raw = copy.deepcopy(self.raw)
        jobs = raw["tree"]["groups"][0]["jobs"]
        jobs.append(copy.deepcopy(jobs[0]))
        for job, caller in zip(jobs, ("first", "second"), strict=True):
            job.update(key="Same", job_id="tests", matrix=None, identity=[
                {"jobID": caller, "matrix": None}, {"jobID": "tests", "matrix": {}},
            ])
        self.assertEqual(prove_replay(raw, expected).jobs, ("Same", "Same"))
        jobs[1]["identity"] = copy.deepcopy(jobs[0]["identity"])
        with self.assertRaises(ValueError):
            prove_replay(raw, expected)
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.workspace = Path(self.scratch.name)
        self.expected = ReplayExpectation(
            repository="owner/repo", workspace=self.workspace,
            sha="a" * 40, git_tree="b" * 40,
            workflow=".github/workflows/ci.yml", selected_job=None,
            mode="minimal", required_jobs=(ReplayJob("CI/Tests", ("Run tests",)),),
        )
        self.raw: dict[str, JsonValue] = {
            "schema_version": 1, "provider": "github", "repository": "owner/repo", "engine": "act",
            "workspace": str(self.workspace), "sha": "a" * 40,
            "git_tree": "b" * 40, "tree_digest": "c" * 64, "dirty": None,
            "workflow": ".github/workflows/ci.yml", "job": None,
            "trigger": "pr", "event": "pull_request", "mode": "minimal", "state": "done",
            "conclusion": "success", "exit_code": 0, "act_exit_code": 0,
            "cleanup": "removed", "act_version": "0.2.89-act2.13",
            "tree": {"malformed_lines": 0, "groups": [{"jobs": [{
                "key": "CI/Tests", "status": "completed", "conclusion": "success",
                "sections": [{"name": "Run tests", "stage": "Main", "status": "completed",
                              "conclusion": "success", "first_seq": 10, "last_seq": 20}],
            }]}]},
        }

    def test_repository_accepts_only_declared_former_names(self) -> None:
        moved = replace(self.expected, repository="new/repo", repository_aliases=("owner/repo",))
        self.assertEqual(prove_replay(self.raw, moved).jobs, ("CI/Tests",))
        for expected in (replace(self.expected, repository="new/repo"),
                         replace(self.expected, repository="new/repo", repository_aliases=("other/repo",))):
            with self.assertRaisesRegex(ValueError, "metadata mismatch: repository"):
                prove_replay(self.raw, expected)
        raw = copy.deepcopy(self.raw)
        del raw["repository"]
        with self.assertRaisesRegex(ValueError, "metadata mismatch: repository"):
            prove_replay(raw, moved)

    def test_public_proof_requires_every_source_excluded_job(self) -> None:
        executed = replace(self.expected.required_jobs[0], identity=(identity_part("tests"),))
        excluded = derive_checks(ExpandedJob(
            "ci.yml:writer", "Writer", self.expected.workflow, {},
            {"if": "github.event_name == 'push'", "steps": [{"run": "write-cache"}]},
            identity=(identity_part("writer"),)), mode="minimal", event="pull_request")
        expected = replace(self.expected, required_jobs=(executed, excluded))
        jobs = self.raw["tree"]["groups"][0]["jobs"]
        jobs[0].update(job_id="tests", matrix=None, identity=[{"jobID": "tests", "matrix": None}])
        jobs.append({"key": "Writer", "job_id": "writer", "matrix": None,
                     "identity": [{"jobID": "writer", "matrix": None}],
                     "status": "completed", "conclusion": "skipped", "sections": []})
        self.assertEqual(prove_replay(self.raw, expected).jobs, (executed.key, excluded.key))
        for change in (MetadataChange("conclusion", "success"), MetadataChange("status", "pending"),
                       MetadataChange("sections", [{"stage": "Main", "conclusion": "success"}])):
            altered = copy.deepcopy(self.raw)
            altered["tree"]["groups"][0]["jobs"][1][change.field] = change.value
            with self.assertRaises(ValueError):
                prove_replay(altered, expected)
        with self.assertRaises(ValueError):
            prove_replay(self.raw, replace(expected, required_jobs=(excluded,)))
        with self.assertRaises(ValueError):
            prove_replay(self.raw, replace(expected, required_jobs=(executed, replace(excluded, steps=("Fake",)))))
        jobs.pop()
        with self.assertRaisesRegex(ValueError, "required job is missing"):
            prove_replay(self.raw, expected)

    def test_valid_evidence_names_required_jobs(self) -> None:
        proof = prove_replay(self.raw, self.expected)
        self.assertEqual(proof.jobs, ("CI/Tests",))

    def test_declared_exact_hit_cache_save_may_be_skipped(self) -> None:
        expected = replace(self.expected, required_jobs=(ReplayJob(
            "CI/Tests", ("Run tests", "Save compile cache"),
            cache_save_steps=("Save compile cache",)),))
        tree = self.raw["tree"]
        assert isinstance(tree, dict)
        groups = tree["groups"]
        assert isinstance(groups, list) and isinstance(groups[0], dict)
        jobs = groups[0]["jobs"]
        assert isinstance(jobs, list) and isinstance(jobs[0], dict)
        sections = jobs[0]["sections"]
        assert isinstance(sections, list)
        sections.append({"name": "Save compile cache", "stage": "Main",
                         "status": "completed", "conclusion": "skipped",
                         "first_seq": None, "last_seq": None})
        self.assertEqual(prove_replay(self.raw, expected).jobs, ("CI/Tests",))
        save = sections[-1]
        assert isinstance(save, dict)
        for conclusion in ("failure", "cancelled"):
            save["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                prove_replay(self.raw, expected)
        save["conclusion"] = "skipped"
        sections.pop()
        with self.assertRaises(ValueError):
            prove_replay(self.raw, expected)
        sections.append(save)
        # The save concession never admits a skipped validation step.
        assert isinstance(sections[0], dict)
        sections[0]["conclusion"] = "skipped"
        with self.assertRaises(ValueError):
            prove_replay(self.raw, expected)

    def test_metadata_failures_cannot_prove_a_gate(self) -> None:
        for change in (
            MetadataChange("schema_version", True), MetadataChange("engine", "native"),
            MetadataChange("provider", "other"), MetadataChange("event", "push"),
            MetadataChange("workspace", str(self.workspace / "other")), MetadataChange("sha", "d" * 40),
            MetadataChange("git_tree", "d" * 40), MetadataChange("tree_digest", "bad"),
            MetadataChange("dirty", "c" * 64), MetadataChange("job", "other"), MetadataChange("mode", "full"),
            MetadataChange("state", "running"), MetadataChange("conclusion", "failure"),
            MetadataChange("exit_code", False), MetadataChange("act_exit_code", 1), MetadataChange("cleanup", "failed"),
            MetadataChange("act_version", "0.2.89-act2.2"),
        ):
            with self.subTest(field=change.field):
                raw = copy.deepcopy(self.raw)
                raw[change.field] = change.value
                with self.assertRaises(ValueError):
                    prove_replay(raw, self.expected)

    def test_missing_duplicate_failed_and_skipped_job_steps_fail(self) -> None:
        for change in (
            'raw["tree"]["groups"] = []',
            'raw["tree"]["malformed_lines"] = True',
            'jobs.append(copy.deepcopy(jobs[0]))',
            'jobs[0]["key"] = "CI/Minimal placeholder"',
            'jobs[0]["conclusion"] = "failure"',
            'steps[0]["stage"] = "Post"',
            'steps[0]["conclusion"] = "skipped"',
            'steps[0]["first_seq"] = None',
            'steps[0]["last_seq"] = 9',
            'steps.append(copy.deepcopy(steps[0]))',
        ):
            with self.subTest(change=change):
                raw = copy.deepcopy(self.raw)
                tree = raw["tree"]
                assert isinstance(tree, dict)
                groups = tree["groups"]
                assert isinstance(groups, list)
                group = groups[0]
                assert isinstance(group, dict)
                jobs = group["jobs"]
                assert isinstance(jobs, list)
                job = jobs[0]
                assert isinstance(job, dict)
                steps = job["sections"]
                assert isinstance(steps, list)
                exec(change)
                with self.assertRaises(ValueError):
                    prove_replay(raw, self.expected)

    def test_empty_expectation_cannot_turn_a_placeholder_into_proof(self) -> None:
        for expected in (
            replace(self.expected, required_jobs=()),
            replace(self.expected, required_jobs=(ReplayJob("CI/Tests", ()),)),
            replace(self.expected, required_jobs=(ReplayJob("CI/Tests", ("Run tests", "Run tests")),)),
            replace(self.expected, sha="HEAD"),
            replace(self.expected, git_tree=""),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError):
                    prove_replay(self.raw, expected)

    def test_dispatch_tier_is_bound_to_executed_inputs(self) -> None:
        expected = replace(self.expected, trigger="workflow_dispatch", event="workflow_dispatch",
                           inputs=(ReplayInput("tier", "test"),))
        raw = copy.deepcopy(self.raw)
        raw.update(trigger="workflow_dispatch", event="workflow_dispatch", params={"inputs": {"tier": "test"}})
        self.assertEqual(prove_replay(raw, expected).jobs, ("CI/Tests",))
        for inputs in ({"tier": "minimal"}, {}, {"tier": "test", "other": "unexpected"}):
            with self.subTest(inputs=inputs):
                changed = copy.deepcopy(raw)
                changed["params"] = {"inputs": inputs}
                with self.assertRaises(ValueError):
                    prove_replay(changed, expected)
        with self.assertRaises(ValueError):
            prove_replay(raw, replace(expected, inputs=(ReplayInput("tier", "test"), ReplayInput("tier", "test"))))
