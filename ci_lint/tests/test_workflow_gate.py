"""#362: native required selection comes from source, never skip outputs."""

import tempfile
import unittest
import contextlib
import io
import json
from unittest.mock import patch
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.workflow_gate import build_plan
from ci_lint.cli import main
from ci_lint.tests.helpers import requires_yaml_tooling


class WorkflowGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        directory = self.repo / ".github/workflows"
        directory.mkdir(parents=True)
        (directory / "ci.yml").write_text(
            """name: CI
on: pull_request
jobs:
  mode:
    outputs: {tier: '${{ steps.plan.outputs.tier }}', skip_fast: '${{ steps.verify.outputs.skip_fast }}'}
    steps: [{run: 'resolve-mode'}]
  fast:
    needs: mode
    if: needs.mode.outputs.skip_fast != 'true'
    steps: [{run: 'test'}]
  windows:
    needs: mode
    if: needs.mode.outputs.tier != 'minimal'
    steps: [{run: 'test windows'}]
  macos:
    needs: mode
    if: needs.mode.outputs.tier == 'full'
    steps: [{run: 'test macos'}]
  ci-ok:
    needs: [mode, fast, windows, macos]
    steps: [{run: 'ci-lint gate'}]
""",
            encoding="utf-8",
        )

    def needs(self, tier: str) -> dict[str, JsonValue]:
        return {
            "mode": {"result": "success", "outputs": {"tier": tier, "skip_fast": "true"}},
            "fast": {"result": "skipped"},
            "windows": {"result": "skipped"},
            "macos": {"result": "skipped"},
        }

    def plan(self, needs: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return build_plan(self.repo, "ci.yml", "ci-ok", needs, event="pull_request")

    @requires_yaml_tooling
    def test_native_mode_selection_and_attested_skips_stay_required(self) -> None:
        for tier, required in (
            ("minimal", ["mode", "fast"]),
            ("extended", ["mode", "fast", "windows"]),
            ("windows", ["mode", "fast", "windows"]),
            ("full", ["mode", "fast", "windows", "macos"]),
        ):
            with self.subTest(tier=tier):
                self.assertEqual(self.plan(self.needs(tier))["required_jobs"], required)

    @requires_yaml_tooling
    def test_failed_or_missing_selection_producer_cannot_excuse_jobs(self) -> None:
        for result in ("failure", "skipped", "cancelled", None):
            needs = self.needs("minimal")
            if result is None:
                del needs["mode"]
            else:
                needs["mode"]["result"] = result
            with self.subTest(result=result):
                self.assertEqual(self.plan(needs)["required_jobs"], ["mode", "fast", "windows", "macos"])

    @requires_yaml_tooling
    def test_unselected_failure_still_fails_the_gate(self) -> None:
        needs = self.needs("minimal")
        needs["macos"]["result"] = "failure"
        self.assertIn("macos", self.plan(needs)["required_jobs"])

    def test_unknown_or_missing_source_never_creates_an_empty_plan(self) -> None:
        for workflow, gate in (("absent.yml", "ci-ok"), ("ci.yml", "absent"), ("../ci.yml", "ci-ok")):
            with self.subTest(workflow=workflow, gate=gate), self.assertRaises(ValueError):
                build_plan(self.repo, workflow, gate, self.needs("minimal"), event="pull_request")

    @requires_yaml_tooling
    def test_actual_cli_keeps_unexplained_skip_red(self) -> None:
        needs = self.repo / "needs.json"
        values = self.needs("minimal")
        needs.write_text(json.dumps(values), encoding="utf-8")
        args = ["gate", "--repo", str(self.repo), "--workflow-plan", "ci.yml", "--needs", str(needs), "--json"]
        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "pull_request"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 1)
            values["fast"]["result"] = "success"
            needs.write_text(json.dumps(values), encoding="utf-8")
            self.assertEqual(main(args), 0)

    @requires_yaml_tooling
    def test_missing_selection_output_keeps_every_job_required(self) -> None:
        needs = self.needs("minimal")
        needs["mode"]["outputs"] = {"skip_fast": "true"}
        self.assertEqual(self.plan(needs)["required_jobs"], ["mode", "fast", "windows", "macos"])

    @requires_yaml_tooling
    def test_default_dependency_skip_requires_source_exclusion(self) -> None:
        path = self.repo / ".github/workflows/ci.yml"
        text = (
            path.read_text(encoding="utf-8")
            .replace("  ci-ok:\n", "  macos-tests:\n    needs: macos\n    steps: [{run: 'test'}]\n  ci-ok:\n")
            .replace("needs: [mode, fast, windows, macos]", "needs: [mode, fast, windows, macos, macos-tests]")
        )
        path.write_text(text, encoding="utf-8")
        self.assertNotIn("macos-tests", self.plan(self.needs("minimal"))["required_jobs"])
        self.assertIn("macos-tests", self.plan(self.needs("full"))["required_jobs"])

    @requires_yaml_tooling
    def test_matrix_or_undeclared_outputs_cannot_excuse_coverage(self) -> None:
        path = self.repo / ".github/workflows/ci.yml"
        original = path.read_text(encoding="utf-8")
        for text in (
            original.replace("  mode:\n", "  mode:\n    strategy: {matrix: {axis: [a, b]}}\n"),
            original.replace("outputs: {tier:", "outputs: {undeclared-tier:"),
        ):
            path.write_text(text, encoding="utf-8")
            self.assertEqual(self.plan(self.needs("minimal"))["required_jobs"], ["mode", "fast", "windows", "macos"])

    @requires_yaml_tooling
    def test_trust_decision_and_its_output_alias_cannot_remove_required_jobs(self) -> None:
        path = self.repo / ".github/workflows/ci.yml"
        original = path.read_text(encoding="utf-8")
        for name in ("trusted", "alias"):
            text = (
                original.replace("skip_fast:", f"{name}:")
                .replace("steps.verify.outputs.skip_fast", "steps.verify.outputs.trusted")
                .replace("needs.mode.outputs.skip_fast", f"needs.mode.outputs.{name}")
            )
            path.write_text(text, encoding="utf-8")
            needs = self.needs("minimal")
            needs["mode"]["outputs"] = {"tier": "minimal", name: "true"}
            self.assertIn("fast", self.plan(needs)["required_jobs"])

    def test_unknown_event_refuses_native_planning(self) -> None:
        with self.assertRaises(ValueError):
            build_plan(self.repo, "ci.yml", "ci-ok", self.needs("minimal"), event="")

    @requires_yaml_tooling
    def test_every_decision_step_output_alias_keeps_validation_required(self) -> None:
        path = self.repo / ".github/workflows/ci.yml"
        original = path.read_text(encoding="utf-8")
        for reference in ("steps.verify.outputs['trusted']", "steps.verify.outputs.trust_reason"):
            text = (
                original.replace("skip_fast:", "alias:")
                .replace("'${{ steps.verify.outputs.skip_fast }}'", json.dumps("${{ " + reference + " }}"))
                .replace("needs.mode.outputs.skip_fast", "needs.mode.outputs.alias")
                .replace(
                    "steps: [{run: 'resolve-mode'}]",
                    "steps: [{run: 'resolve-mode'}, {id: verify, run: 'ci-lint local-gate verify --trust --github-output'}]",
                )
            )
            path.write_text(text, encoding="utf-8")
            needs = self.needs("minimal")
            needs["mode"]["outputs"] = {"tier": "minimal", "alias": "true"}
            self.assertEqual(self.plan(needs)["required_jobs"], ["mode", "fast"])
