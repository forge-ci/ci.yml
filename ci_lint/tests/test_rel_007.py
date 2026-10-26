"""REL-007 (talos-hq/clud#2202): a release recomputes; no earlier evidence."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.release_recompute import check_rel_007, reachable_from_release
from ci_lint.tests.helpers import requires_yaml_tooling
from ci_lint.tests.test_complexity import _Repo

RELEASE_ON = "on:\n  push:\n    tags: ['v*']\n"
CI_ON = "on:\n  pull_request:\n  push:\n    branches: [main]\n  workflow_call:\n"

LOOKUP_SCRIPT = """\
import json, subprocess
runs = json.loads(subprocess.run(["gh", "run", "list", "--json", "conclusion"]).stdout)
assert runs[0]["conclusion"] == "success"
"""


def _job(run: str, if_line: str = "") -> str:
    return (
        "jobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - name: step\n{if_line}        run: {run}\n"
    )


def _run(files: dict[str, str]):
    with _Repo(files) as root:
        return check_rel_007(root)


class Rel007ConditionTest(unittest.TestCase):
    def test_pr_or_main_push_conditions_are_unreachable(self) -> None:
        for cond in (
            "github.event_name == 'pull_request'",
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
            "github.event_name == 'pull_request' || (github.event_name == 'push' && github.ref == 'refs/heads/main')",
        ):
            self.assertFalse(reachable_from_release(cond), cond)

    def test_other_conditions_are_reachable(self) -> None:
        for cond in (None, "", "always()", "github.event_name == 'pull_request' || always()"):
            self.assertTrue(reachable_from_release(cond), cond)


class Rel007Test(unittest.TestCase):
    @requires_yaml_tooling
    def test_attestation_verify_in_a_release_workflow_is_violation(self) -> None:
        findings = _run({".github/workflows/release.yml": RELEASE_ON + _job("ci-lint local-gate verify --repo .")})
        self.assertEqual(["REL-007"], [f.rule for f in findings])
        self.assertEqual(Status.VIOLATION, findings[0].status)

    @requires_yaml_tooling
    def test_called_ci_reaches_ungated_reuse_check(self) -> None:
        release = RELEASE_ON + "jobs:\n  full:\n    uses: ./.github/workflows/ci.yml\n"
        ci = CI_ON + _job("ci-lint reuse-check --mode enforce")
        findings = _run({".github/workflows/release.yml": release, ".github/workflows/ci.yml": ci})
        self.assertEqual([".github/workflows/ci.yml"], [f.path for f in findings])

    @requires_yaml_tooling
    def test_called_ci_with_pr_gated_attestation_is_clean(self) -> None:
        # The clud pilot shape: ci.yml's waivers key on PR / main push.
        release = RELEASE_ON + "jobs:\n  full:\n    uses: ./.github/workflows/ci.yml\n"
        ci = CI_ON + _job("ci-lint local-gate verify", "        if: github.event_name == 'pull_request'\n")
        self.assertEqual([], _run({".github/workflows/release.yml": release, ".github/workflows/ci.yml": ci}))

    @requires_yaml_tooling
    def test_earlier_run_lookup_script_is_violation(self) -> None:
        findings = _run({
            ".github/workflows/release.yml": RELEASE_ON + _job("python -m ci.release_gate"),
            "ci/release_gate.py": LOOKUP_SCRIPT,
        })
        self.assertEqual(1, len(findings))
        self.assertIn("ci/release_gate.py", findings[0].message)

    @requires_yaml_tooling
    def test_artifact_download_by_run_id_is_clean(self) -> None:
        script = 'import subprocess\nsubprocess.run(["gh", "run", "download", "123"])\n'
        files = {".github/workflows/release.yml": RELEASE_ON + _job("python ci/fetch.py"), "ci/fetch.py": script}
        self.assertEqual([], _run(files))

    @requires_yaml_tooling
    def test_a_reuse_document_file_name_is_not_the_command(self) -> None:
        # clud's CI OK step: `ci-lint gate ... --default-branch-reuse reuse-check.json`.
        run = "ci-lint gate --needs needs.json --default-branch-reuse reuse-check.json"
        self.assertEqual([], _run({".github/workflows/release.yml": RELEASE_ON + _job(run)}))

    @requires_yaml_tooling
    def test_non_release_workflow_is_out_of_scope(self) -> None:
        self.assertEqual([], _run({".github/workflows/ci.yml": CI_ON + _job("ci-lint reuse-check")}))


if __name__ == "__main__":
    unittest.main()
