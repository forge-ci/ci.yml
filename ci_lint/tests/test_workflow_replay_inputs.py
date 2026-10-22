"""Reusable job identities bind only statically known string inputs."""

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_lint.tests.test_workflow_replay_expansion import workflow
from ci_lint.workflow_replay import ReplayJob
from ci_lint.workflow_replay_config import DeclaredReplayJob, ReplayConfig, ReplaySelection
from ci_lint.workflow_replay_expansion import expand_selection
from ci_lint.workflow_replay_static import check_replay_static
from ci_lint.workflow_replay_inputs import BoundInput, bind_call_inputs


class ReplayInputIdentityTest(unittest.TestCase):
    def setUp(self):
        self.entry = workflow(".github/workflows/ci.yml", {"name": "CI", "jobs": {
            "linux": {"uses": "./.github/workflows/check.yml", "with": {"target": "linux-x64"}},
        }})
        self.called = workflow(".github/workflows/check.yml", {"name": "Check", "on": {
            "workflow_call": {"inputs": {"target": {"type": "string", "required": True}}}},
            "jobs": {"test": {"name": "Test (${{ inputs.target }})", "steps": []}}})

    def proof(self):
        return expand_selection((self.entry, self.called), self.entry.path, "linux")

    def test_literal_caller_input_binds_concrete_identity(self):
        proof = self.proof()
        self.assertIsNone(proof.problem)
        self.assertEqual(proof.jobs[0].key, "linux/Check/Test (linux-x64)")

    def test_boolean_profiles_preserve_false_defaults_and_forwarding(self):
        self.called.document["on"]["workflow_call"]["inputs"]["compile"] = {"type": "boolean"}
        bound = bind_call_inputs(self.called.document, self.entry.document["jobs"]["linux"], ())
        self.assertIn(BoundInput("compile", False), bound)
        caller = self.entry.document["jobs"]["linux"]
        caller["with"]["compile"] = "${{ inputs.compile }}"
        bound = bind_call_inputs(self.called.document, caller, (BoundInput("compile", True),))
        self.assertIn(BoundInput("compile", True), bound)
        for value in ("false", 0, "${{ inputs.missing }}"):
            caller["with"]["compile"] = value
            with self.assertRaises(ValueError):
                bind_call_inputs(self.called.document, caller, ())
        caller["with"]["compile"] = "${{ github.ref }}"
        self.assertNotIn("compile", {item.name for item in bind_call_inputs(self.called.document, caller, ())})

    def test_event_expression_binds_only_known_boolean_scalars(self):
        caller = self.entry.document["jobs"]["linux"]
        self.called.document["on"]["workflow_call"]["inputs"]["compile"] = {"type": "boolean"}
        cases = (
            ("github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'ci-integration')", "workflow_dispatch", False),
            ("github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'ci-integration')", "pull_request", None),
            ("github.event_name == 'workflow_dispatch' || github.ref == 'main'", "workflow_dispatch", True),
            ("github.ref == 'main' || true", "workflow_dispatch", None),
            ("github.ref", "workflow_dispatch", None),
            ("github.event_name == 'push'", None, None),
            ("github.event_name == 'push' + 1", "workflow_dispatch", None),
        )
        for expression, event, expected in cases:
            with self.subTest(expression=expression, event=event):
                caller["with"]["compile"] = "${{ " + expression + " }}"
                proof = expand_selection((self.entry, self.called), self.entry.path, "linux", event=event)
                self.assertIsNone(proof.problem)
                found = next((item.value for item in proof.jobs[0].inputs if item.name == "compile"), None)
                self.assertIs(found, expected)

    def test_qualified_caller_matrix_binds_each_reusable_profile(self):
        caller = self.entry.document["jobs"]["linux"]
        caller["strategy"] = {"matrix": {"target": ["linux-x64", "linux-arm64"], "compile": [False, True]}}
        caller["with"] = {"target": "${{ matrix.target }}", "compile": "${{ matrix.compile }}"}
        self.called.document["on"]["workflow_call"]["inputs"]["compile"] = {"type": "boolean"}
        proof = expand_selection((self.entry, self.called), self.entry.path, "linux", qualified=True)
        self.assertIsNone(proof.problem)
        self.assertEqual(len(proof.jobs), 4)
        self.assertEqual({tuple(item.value for item in job.inputs) for job in proof.jobs}, {
            ("linux-x64", False), ("linux-x64", True), ("linux-arm64", False), ("linux-arm64", True)})
        caller["with"]["compile"] = "${{ matrix.absent }}"
        self.assertIsNotNone(expand_selection((self.entry, self.called), self.entry.path, "linux", qualified=True).problem)

    def test_default_and_explicit_empty_override_are_distinct(self):
        self.entry.document["jobs"]["linux"]["with"] = {}
        self.called.document["on"]["workflow_call"]["inputs"]["target"].update(required=False, default="default")
        self.assertEqual(self.proof().jobs[0].key, "linux/Check/Test (default)")
        self.entry.document["jobs"]["linux"]["with"]["target"] = ""
        self.assertEqual(self.proof().jobs[0].key, "linux/Check/Test ()")

    def test_unknown_expression_missing_required_and_wrong_type_fail_closed(self):
        for value in ("${{ matrix.target }}", "${{ github.ref }}", True, 17, "${{ inputs.missing }}"):
            self.entry.document["jobs"]["linux"]["with"]["target"] = value
            self.assertIsNotNone(self.proof().problem)
        self.entry.document["jobs"]["linux"]["with"] = {}
        self.assertIsNotNone(self.proof().problem)

    def test_nested_forwarding_and_dependency_names_use_same_binding(self):
        inner = copy.deepcopy(self.called)
        inner = workflow(".github/workflows/inner.yml", inner.document)
        self.called.document["jobs"] = {
            "nested": {"uses": "./.github/workflows/inner.yml", "with": {"target": "${{ inputs.target }}"}}}
        inner.document["jobs"]["helper"] = {"name": "Prepare ${{ inputs.target }}", "steps": []}
        inner.document["jobs"]["test"]["needs"] = "helper"
        proof = expand_selection((self.entry, self.called, inner), self.entry.path, "linux")
        self.assertIsNone(proof.problem)
        self.assertEqual({job.key for job in proof.jobs}, {
            "linux/nested/Check/Test (linux-x64)", "linux/nested/Check/Prepare linux-x64"})

    def test_nested_caller_display_name_uses_parent_input_binding(self):
        inner = workflow(".github/workflows/inner.yml", copy.deepcopy(self.called.document))
        self.entry.document["jobs"]["linux"]["name"] = "Linux x64"
        self.called.document["jobs"] = {
            "nested": {"name": "Prepare ${{ inputs.target }}",
                       "uses": "./.github/workflows/inner.yml",
                       "with": {"target": "${{ inputs.target }}"}}}
        proof = expand_selection((self.entry, self.called, inner), self.entry.path, "linux")
        self.assertIsNone(proof.problem)
        self.assertEqual(proof.jobs[0].key,
                         "Linux x64/Prepare linux-x64/Check/Test (linux-x64)")

    def test_duplicate_caller_display_names_never_hide_different_inputs(self):
        first = self.entry.document["jobs"]["linux"]
        first["name"] = "Same"
        second = copy.deepcopy(first)
        second["with"]["target"] = "another-target"
        self.entry.document["jobs"]["other"] = second
        proof = expand_selection((self.entry, self.called), self.entry.path, None)
        self.assertIsNotNone(proof.problem)

    def test_unknown_name_syntax_and_undeclared_inputs_are_not_resolved(self):
        self.called.document["jobs"]["test"]["name"] = "${{ inputs.target || 'fallback' }}"
        self.assertIsNotNone(self.proof().problem)
        self.called.document["jobs"]["test"]["name"] = "${{ inputs.target }}"
        self.entry.document["jobs"]["linux"]["with"]["unknown"] = "x"
        self.assertIsNotNone(self.proof().problem)

    def test_static_binding_checks_expanded_identity_and_caller_changes(self):
        self.called.document["jobs"]["test"]["steps"] = [{"name": "Run tests", "run": "pytest"}]
        declared = DeclaredReplayJob("check.yml:test", ReplayJob(
            "linux/Check/Test (linux-x64)", ("Run tests",)), ("tests",))
        config = ReplayConfig("owner/repo", self.entry.path, "minimal", (declared,),
                              (ReplaySelection("tests", "linux", "pull_request", ()),))
        with patch("ci_lint.workflow_replay_static.load_workflows", return_value=(self.entry, self.called)):
            self.assertEqual(check_replay_static(config, Path("/unused")), [])
            self.entry.document["jobs"]["linux"]["with"]["target"] = "another-target"
            self.assertTrue(check_replay_static(config, Path("/unused")))
            self.entry.document["jobs"]["linux"]["with"]["target"] = "${{ matrix.target }}"
            self.assertTrue(check_replay_static(config, Path("/unused")))
