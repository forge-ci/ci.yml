"""GEN-001, GEN-002, GEN-008, TAG-003, SEC-003, SEC-004, RUN-001, WF-001..003,
CT-004 -- ci_lint/rules/workflows.py. These all need a real YAML parser."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.workflows import (
    check_ct_004,
    check_gen_001,
    check_gen_002,
    check_gen_008,
    check_run_001,
    check_run_002,
    check_sec_003,
    check_sec_004,
    check_tag_003,
    check_wf_001,
    check_wf_002,
    check_wf_003,
)
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling
from ci_lint.workflow_scan import as_dict, load_workflows


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]


@requires_yaml_tooling
class WorkflowFixtureTest(unittest.TestCase):
    def test_gen_001_missing_or_incomplete_ci_yml(self) -> None:
        wfs = load_workflows(fixture("GEN-001", "red"))
        self.assertIn("GEN-001", rule_ids(check_gen_001(wfs)))
        wfs = load_workflows(fixture("GEN-001", "green"))
        self.assertNotIn("GEN-001", rule_ids(check_gen_001(wfs)))

    def test_gen_002_ungated_non_linux_job(self) -> None:
        wfs = load_workflows(fixture("GEN-002", "red"))
        self.assertIn("GEN-002", rule_ids(check_gen_002(wfs)))
        wfs = load_workflows(fixture("GEN-002", "green"))
        self.assertNotIn("GEN-002", rule_ids(check_gen_002(wfs)))

    def test_gen_008_undeclared_workflow_file(self) -> None:
        repo = fixture("GEN-008", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("GEN-008", rule_ids(check_gen_008(ci, load_workflows(repo))))
        repo = fixture("GEN-008", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("GEN-008", rule_ids(check_gen_008(ci, load_workflows(repo))))

    def test_tag_003_missing_edited(self) -> None:
        wfs = load_workflows(fixture("TAG-003", "red"))
        self.assertIn("TAG-003", rule_ids(check_tag_003(wfs)))
        wfs = load_workflows(fixture("TAG-003", "green"))
        self.assertNotIn("TAG-003", rule_ids(check_tag_003(wfs)))

    def test_sec_003_pull_request_target(self) -> None:
        wfs = load_workflows(fixture("SEC-003", "red"))
        self.assertIn("SEC-003", rule_ids(check_sec_003(wfs)))
        wfs = load_workflows(fixture("SEC-003", "green"))
        self.assertNotIn("SEC-003", rule_ids(check_sec_003(wfs)))

    def test_sec_004_unpinned_action(self) -> None:
        repo = fixture("SEC-004", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("SEC-004", rule_ids(check_sec_004(ci, load_workflows(repo), [])))
        repo = fixture("SEC-004", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("SEC-004", rule_ids(check_sec_004(ci, load_workflows(repo), [])))

    # ── SEC-004 x setup-soldr@v0 (M2-29, ci.yml#33) ────────────────────────
    # zackees/setup-soldr@v0 is the ONE sanctioned first-party floating ref;
    # everything else must still SHA-pin. Uses the SEC-004 green fixture's
    # ci.toml, which already lists "zackees/setup-soldr" in [allow].actions.

    def test_sec_004_setup_soldr_v0_float_is_sanctioned(self) -> None:
        from ci_lint.rules.workflows import _check_one_use

        allowed = frozenset(["zackees/setup-soldr"])
        findings = _check_one_use(
            "zackees/setup-soldr@v0", "wf.yml", "jobs.build.steps[0]", allowed
        )
        self.assertEqual([], findings)

    def test_sec_004_setup_soldr_other_float_still_flagged(self) -> None:
        from ci_lint.rules.workflows import _check_one_use

        allowed = frozenset(["zackees/setup-soldr"])
        # A branch or a version tag other than "v0" is not the sanctioned
        # float -- only the gated v0 promotion is exempt.
        for ref in ("main", "v0.9.82"):
            findings = _check_one_use(
                f"zackees/setup-soldr@{ref}", "wf.yml", "jobs.build.steps[0]", allowed
            )
            self.assertIn("SEC-004", rule_ids(findings), ref)

    def test_sec_004_other_action_v0_style_float_still_flagged(self) -> None:
        from ci_lint.rules.workflows import _check_one_use

        allowed = frozenset(["actions/checkout"])
        findings = _check_one_use(
            "actions/checkout@v0", "wf.yml", "jobs.build.steps[0]", allowed
        )
        self.assertIn("SEC-004", rule_ids(findings))

    def test_sec_004_setup_soldr_cook_v0_float_is_sanctioned(self) -> None:
        from ci_lint.rules.workflows import _check_one_use

        allowed = frozenset(["zackees/setup-soldr"])
        findings = _check_one_use(
            "zackees/setup-soldr/cook@v0", "wf.yml", "jobs.build.steps[0]", allowed
        )
        self.assertEqual([], findings)

    def test_run_001_bad_runner_label(self) -> None:
        repo = fixture("RUN-001", "red")
        ci = load_ci_toml(repo).ci
        findings = check_run_001(ci, load_workflows(repo))
        self.assertIn("RUN-001", rule_ids(findings))
        # The literal 'ubuntu-latest' on jobs.build.
        self.assertTrue(
            any("jobs.build.runs-on" in f.message for f in findings), msg=[f.message for f in findings]
        )
        repo = fixture("RUN-001", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("RUN-001", rule_ids(check_run_001(ci, load_workflows(repo))))

    def test_run_001_matrix_over_platform_lanes_is_resolved_not_needs_review(self) -> None:
        """Round-6C: a `runs-on: ${{ matrix.lane.runs_on }}` job whose
        `strategy.matrix.lane` is built from the precheck plan's
        `platform_lanes_todo_json` output (which enumerates ci.toml's
        [platforms]) is resolvable. RUN-001 must validate ci.toml's own
        [platforms].*.runs-on values instead of reporting needs_review."""
        repo = fixture("RUN-001", "red")
        ci = load_ci_toml(repo).ci
        findings = check_run_001(ci, load_workflows(repo))
        self.assertNotIn(Status.NEEDS_REVIEW, [f.status for f in findings])
        # windows-x64's runs-on is 'windows-latest' in the red ci.toml --
        # resolved through the matrix, that must surface as RUN-001 against
        # ci.toml, not against the workflow file.
        self.assertTrue(
            any(f.rule == "RUN-001" and f.path == "ci.toml" and "platforms.windows-x64" in f.message
                for f in findings),
            msg=[f.message for f in findings],
        )

        repo = fixture("RUN-001", "green")
        ci = load_ci_toml(repo).ci
        findings = check_run_001(ci, load_workflows(repo))
        self.assertEqual([], [f for f in findings if f.status == Status.NEEDS_REVIEW])
        self.assertEqual([], [f for f in findings if f.path == "ci.toml"])

    def test_run_001_literal_matrix_is_resolved_not_needs_review(self) -> None:
        """template-python-rust-cmd's musl jobs: `runs-on: ${{ matrix.lane.runs_on }}`
        over the job's own literal `strategy.matrix` validates each value
        against the fleet list; a bare `${{ matrix.os }}` list and `include`
        entries resolve the same way."""
        repo = fixture("RUN-001", "green")
        ci = load_ci_toml(repo).ci
        wfs = load_workflows(repo)
        doc = as_dict(wfs[0].document)
        doc["jobs"]["musl"] = {
            "runs-on": "${{ matrix.lane.runs_on }}",
            "strategy": {"matrix": {"lane": [
                {"id": "x64", "runs_on": "ubuntu-24.04"},
                {"id": "arm", "runs_on": "ubuntu-24.04-arm"},
            ]}},
        }
        doc["jobs"]["os"] = {
            "runs-on": "${{ matrix.os }}",
            "strategy": {"matrix": {"include": [{"os": "macos-15"}, {"os": "windows-2025"}]}},
        }
        self.assertEqual([], check_run_001(ci, wfs))
        doc["jobs"]["os"]["strategy"]["matrix"]["include"].append({"os": "ubuntu-latest"})
        findings = check_run_001(ci, wfs)
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])
        self.assertIn("ubuntu-latest", findings[0].message)
        # One dynamic matrix value keeps the whole label needs_review.
        doc["jobs"]["os"]["strategy"]["matrix"]["include"][-1] = {"os": "${{ vars.R }}"}
        findings = check_run_001(ci, wfs)
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_run_001_unresolvable_expression_stays_needs_review(self) -> None:
        """An expression that is NOT sourced from a precheck plan
        platform-lanes output can't be statically resolved and must stay
        needs_review, exactly as before round-6C."""
        repo = fixture("RUN-001", "green")
        ci = load_ci_toml(repo).ci
        wfs = load_workflows(repo)
        doc = as_dict(wfs[0].document)
        doc["jobs"]["unresolvable"] = {
            "runs-on": "${{ matrix.lane.runs_on }}",
            "strategy": {"matrix": {"lane": "${{ fromJSON(vars.CUSTOM_MATRIX_JSON) }}"}},
        }
        findings = check_run_001(ci, wfs)
        needs_review = [f for f in findings if f.status == Status.NEEDS_REVIEW]
        self.assertTrue(
            any("jobs.unresolvable.runs-on" in f.message for f in needs_review),
            msg=[f.message for f in findings],
        )

    def test_run_002_cross_build_job_name_missing_build_host(self) -> None:
        """ci.yml#12: a platform-lanes matrix leg that always builds on a
        fixed runner (soldr cross-compile) must say so in its `name:`."""
        wfs = load_workflows(fixture("RUN-002", "red"))
        findings = check_run_002(wfs)
        rules = rule_ids(findings)
        self.assertIn("RUN-002", rules)
        self.assertTrue(
            any("jobs.platform-build" in f.message and "builds on a fixed host" in f.message
                for f in findings),
            msg=[f.message for f in findings],
        )
        self.assertTrue(
            any("jobs.platform-run" in f.message and "executes natively" in f.message
                for f in findings),
            msg=[f.message for f in findings],
        )
        wfs = load_workflows(fixture("RUN-002", "green"))
        self.assertNotIn("RUN-002", rule_ids(check_run_002(wfs)))

    def test_wf_001_missing_timeout(self) -> None:
        wfs = load_workflows(fixture("WF-001", "red"))
        self.assertIn("WF-001", rule_ids(check_wf_001(wfs)))
        wfs = load_workflows(fixture("WF-001", "green"))
        self.assertNotIn("WF-001", rule_ids(check_wf_001(wfs)))

    def test_wf_002_missing_concurrency(self) -> None:
        wfs = load_workflows(fixture("WF-002", "red"))
        self.assertIn("WF-002", rule_ids(check_wf_002(wfs)))
        wfs = load_workflows(fixture("WF-002", "green"))
        self.assertNotIn("WF-002", rule_ids(check_wf_002(wfs)))

    def test_wf_003_continue_on_error(self) -> None:
        wfs = load_workflows(fixture("WF-003", "red"))
        self.assertIn("WF-003", rule_ids(check_wf_003(wfs, [])))
        wfs = load_workflows(fixture("WF-003", "green"))
        self.assertNotIn("WF-003", rule_ids(check_wf_003(wfs, [])))

    def test_ct_004_linter_pin_mismatch(self) -> None:
        repo = fixture("CT-004", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("CT-004", rule_ids(check_ct_004(ci, load_workflows(repo))))
        repo = fixture("CT-004", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CT-004", rule_ids(check_ct_004(ci, load_workflows(repo))))


if __name__ == "__main__":
    unittest.main()
