"""#362: native reuse proves every source-derived leaf, including shards."""

import tempfile
import unittest
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from ci_lint.cli import main
from ci_lint.runtime.gate import compute_gate
from ci_lint.workflow_gate import build_plan
from ci_lint.workflow_job_names import WorkflowJobNames
from ci_lint.tests.helpers import requires_yaml_tooling


class WorkflowJobNamesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.directory = self.repo / ".github/workflows"
        self.directory.mkdir(parents=True)
        (self.directory / "ci.yml").write_text("""name: CI
on: [pull_request, push]
jobs:
  static:
    name: Static checks
    steps: [{run: 'check'}]
  unit:
    name: Unit tests
    needs: static
    strategy:
      matrix: {shard: [rust, py1of2, py2of2]}
    uses: ./.github/workflows/test.yml
    with: {target: linux, suite: unit, shard: '${{ matrix.shard }}'}
  ci-ok:
    needs: [static, unit]
    steps: [{run: 'ci-lint gate'}]
""", encoding="utf-8")
        (self.directory / "test.yml").write_text("""name: Tests
on:
  workflow_call:
    inputs:
      target: {type: string, required: true}
      suite: {type: string, required: true}
      shard: {type: string, required: true}
jobs:
  test:
    name: ${{ inputs.target }} ${{ inputs.suite }}
    steps: [{run: 'test'}]
""", encoding="utf-8")
        self.needs = {"static": {"result": "success"}, "unit": {"result": "skipped"}}
        self.names = tuple(f"Unit tests ({shard}) / linux unit" for shard in ("rust", "py1of2", "py2of2"))
        self.doc = {
            "schema": 1, "reuse": True, "event_name": "push", "sha": "push",
            "pr_head_sha": "head", "run_id": 555, "pr": 7,
            "jobs": [{"name": name, "job_id": 9000 + index} for index, name in enumerate(self.names)],
        }
        self.live = {9000 + index: {"name": name, "conclusion": "success", "head_sha": "head", "run_id": 555}
                     for index, name in enumerate(self.names)}

    def source(self) -> WorkflowJobNames:
        return WorkflowJobNames.from_gate(self.repo, "ci.yml", "ci-ok", self.needs)

    def report(self):
        def fetch(url, _token):
            return self.live[int(url.rsplit("/", 1)[1])]
        return compute_gate(
            build_plan(self.repo, "ci.yml", "ci-ok", self.needs, event="push"), self.needs,
            default_branch_reuse=self.doc, push_sha="push", fetch=fetch, token="t", repo="owner/repo",
            workflow_jobs=self.source(),
        )

    @requires_yaml_tooling
    def test_native_names_and_all_three_shards_reuse(self) -> None:
        self.assertEqual(self.source().for_job("static"), ("Static checks",))
        self.assertEqual(set(self.source().for_job("unit")), set(self.names))
        self.assertTrue(self.report().ok)

    @requires_yaml_tooling
    def test_incomplete_or_duplicate_shard_proof_fails(self) -> None:
        original = self.doc["jobs"]
        for jobs in (original[:-1], original + [original[0]], [original[0]] * 3):
            with self.subTest(jobs=jobs):
                self.doc["jobs"] = jobs
                self.assertFalse(self.report().ok)

    @requires_yaml_tooling
    def test_distinct_names_cannot_reuse_one_job_id(self) -> None:
        self.doc["jobs"][2]["job_id"] = self.doc["jobs"][0]["job_id"]
        self.assertFalse(self.report().ok)

    @requires_yaml_tooling
    def test_native_source_never_falls_back_to_a_bare_caller_name(self) -> None:
        self.doc["jobs"] = [{"name": "unit", "job_id": 9000}]
        self.assertFalse(self.report().ok)

    @requires_yaml_tooling
    def test_every_live_shard_checks_name_head_run_and_conclusion(self) -> None:
        for field, value in (("name", "another job"), ("head_sha", "other"), ("run_id", 556),
                             ("conclusion", "failure")):
            with self.subTest(field=field):
                self.live[9002][field] = value
                self.assertFalse(self.report().ok)
                self.live[9002][field] = {"name": self.names[2], "head_sha": "head", "run_id": 555,
                                        "conclusion": "success"}[field]

    def test_ambiguous_source_display_names_fail(self) -> None:
        path = self.directory / "ci.yml"
        path.write_text(path.read_text().replace("  ci-ok:\n", "  duplicate:\n    name: Static checks\n"
                        "    steps: [{run: 'different'}]\n  ci-ok:\n"))
        with self.assertRaises(ValueError):
            self.source().for_job("unit")

    @requires_yaml_tooling
    def test_unknown_name_expression_never_uses_a_head_supplied_alias(self) -> None:
        path = self.directory / "test.yml"
        path.write_text(path.read_text().replace("${{ inputs.target }}", "${{ github.ref }}"))
        self.assertFalse(self.report().ok)

    @requires_yaml_tooling
    def test_actual_cli_derives_proofs_from_workflow_source(self) -> None:
        needs = self.repo / "needs.json"
        document = self.repo / "reuse.json"
        needs.write_text(json.dumps(self.needs))
        document.write_text(json.dumps(self.doc))
        def fetch(url, _token):
            return self.live[int(url.rsplit("/", 1)[1])]
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "push", "GITHUB_TOKEN": "t",
               "GITHUB_REPOSITORY": "owner/repo"}
        args = ["gate", "--repo", str(self.repo), "--workflow-plan", "ci.yml", "--needs", str(needs),
                "--default-branch-reuse", str(document), "--json"]
        with patch.dict("os.environ", env), patch("ci_lint.cli.default_fetch", fetch), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            del self.live[9002]["run_id"]
            self.assertEqual(main(args), 1)


if __name__ == "__main__":
    unittest.main()
