"""`ci-lint fleet scan` / `ci-lint sync-issues --dry-run` (zackees/ci.yml#38).

Fixtures under fixtures/FLEET/{green,red}/api.json are a live `fleet scan
--record` of zackees/template-python-rust-cmd (+ zackees/setup-soldr's
release list), trimmed; RED edits the recorded responses so every FLEET-*,
GEN-008, RUN-001, RUST-014 (a real v0.9.63 setup-soldr SHA) and SEC-007 path fires. No network.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from ci_lint import cli
from ci_lint.finding import Finding, Status
from ci_lint.fleet import (
    CiTomlCheck,
    RepoScan,
    burn_share,
    check_ci_toml,
    check_fleet_003,
    check_workflows,
    load_report_json,
    run_fleet_scan,
    to_json_dict,
)
from ci_lint.fleet_cli import replay_fetch
from ci_lint.settings_audit import SettingsReply
from ci_lint.sync_issues import ExistingIssue, fingerprint, plan_repo, plan_sync
from ci_lint.tests.helpers import FIXTURES
from ci_lint.yaml_io import yaml_tooling_available

T = "zackees/template-python-rust-cmd"
GREEN = FIXTURES / "FLEET" / "green" / "api.json"
RED = FIXTURES / "FLEET" / "red" / "api.json"


def _fetch(path):
    return replay_fetch(json.loads(path.read_text(encoding="utf-8")))


@unittest.skipUnless(yaml_tooling_available(), "needs PyYAML or yq to parse workflows")
class FleetScanTest(unittest.TestCase):
    def test_green_recorded_template_is_clean(self) -> None:
        report = run_fleet_scan(_fetch(GREEN), "t", owners=("zackees",), repos=("template-python-rust-cmd",))
        self.assertEqual(report.errors, ())
        (scan,) = report.repos
        self.assertEqual(scan.ci_toml_schema, 3)
        self.assertEqual(scan.pr_entrypoints, (".github/workflows/ci.yml",))
        self.assertEqual([f.render() for f in scan.findings], [])

    def test_red_fires_every_rule(self) -> None:
        report = run_fleet_scan(_fetch(RED), "t", owners=("zackees",), repos=(T,))
        (scan,) = report.repos
        rules = {f.rule for f in scan.findings if f.status == Status.VIOLATION}
        self.assertEqual(rules, {"FLEET-001", "FLEET-002", "FLEET-003", "GEN-008", "RUN-001", "RUST-014", "SEC-007"})
        self.assertIsNone(scan.ci_toml_schema)
        self.assertEqual(scan.score, 7 * 10 + scan.violations)

    def test_ranking_worst_first_and_json_roundtrip(self) -> None:
        red = run_fleet_scan(_fetch(RED), "t", repos=(T,)).repos[0]
        green = run_fleet_scan(_fetch(GREEN), "t", repos=(T,)).repos[0]
        from ci_lint.fleet import FleetReport

        report = FleetReport(owners=("zackees",), repos=(red, green), errors=())
        again = load_report_json(json.dumps(to_json_dict(report)))
        self.assertEqual(again.repos, report.repos)

    def test_unknown_repo_is_an_error_not_a_pass(self) -> None:
        report = run_fleet_scan(_fetch(GREEN), "t", owners=("zackees",), repos=("nope",))
        self.assertEqual(report.repos, ())
        self.assertEqual(len(report.errors), 1)

    def test_cli_replay_exit_codes(self) -> None:
        for path, code in ((GREEN, 0), (RED, 1)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["fleet", "scan", "--repos", T, "--replay", str(path), "--json"])
            self.assertEqual(rc, code)
            self.assertEqual(json.loads(buf.getvalue())["repos"][0]["repo"], T)


class FleetUnitTest(unittest.TestCase):
    def test_unreadable_ci_workflow_does_not_claim_missing_pr_trigger(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from ci_lint.yaml_io import LoadResult, LoadStatus

        for reason in ("no YAML parser available", "PyYAML parse error"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = root / ".github" / "workflows" / "ci.yml"
                workflow.parent.mkdir(parents=True)
                workflow.write_text("on: [pull_request]\njobs: {}\n", encoding="utf-8")
                unreadable = LoadResult(LoadStatus.NEEDS_REVIEW, None, reason)
                with patch("ci_lint.workflow_scan.load_yaml_file", return_value=unreadable):
                    check = check_workflows(root)
                found = check.findings
                self.assertEqual(check.workflow_count, 1)
                self.assertEqual(check.pr_entrypoints, ())
                trigger_findings = [finding for finding in found if finding.rule == "GEN-001"]
                self.assertEqual(len(trigger_findings), 1)
                self.assertEqual(trigger_findings[0].status, Status.NEEDS_REVIEW)
                self.assertNotIn("no .github/workflows/ci.yml", trigger_findings[0].message)
                self.assertEqual(plan_repo(_scan(tuple(found)), []), [])

    def test_ci_toml_schema_states(self) -> None:
        self.assertEqual(check_ci_toml("o/r", 200, "schema = 3\n"), CiTomlCheck(3, ()))
        one = check_ci_toml("o/r", 200, "schema = 1\n")
        self.assertEqual((one.schema, one.findings[0].rule), (1, "FLEET-001"))
        self.assertEqual(check_ci_toml("o/r", 0, None).findings[0].status, Status.NEEDS_REVIEW)

    def test_gen_001_without_ci_yml_entrypoint(self) -> None:
        import tempfile
        from pathlib import Path

        if not yaml_tooling_available():
            self.skipTest("needs PyYAML or yq")
        with tempfile.TemporaryDirectory() as tmp:
            wf = Path(tmp) / ".github" / "workflows" / "test.yml"
            wf.parent.mkdir(parents=True)
            wf.write_text("on:\n  push:\njobs:\n  a:\n    runs-on: ubuntu-24.04\n", encoding="utf-8")
            check = check_workflows(Path(tmp))
        self.assertEqual((check.workflow_count, check.pr_entrypoints, [f.rule for f in check.findings]), (1, (), ["GEN-001"]))


_RUNS = "https://api.github.com/repos/o/r/actions/runs?per_page=100"


def _run(event: str, branch: str, minutes: int) -> dict[str, str]:
    return {
        "event": event,
        "head_branch": branch,
        "status": "completed",
        "run_started_at": "2026-10-20T00:00:00Z",
        "updated_at": f"2026-10-20T{minutes // 60:02d}:{minutes % 60:02d}:00Z",
    }


_CLUD_SHAPED = {_RUNS: [200, {"workflow_runs": [_run("push", "main", 22), _run("pull_request", "x", 78)]}]}
_QUEUE = SettingsReply(
    200,
    [
        {
            "enforcement": "active",
            "target": "branch",
            "rules": [{"type": "merge_queue"}],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
        }
    ],
)


class Fleet003Test(unittest.TestCase):
    def _check(self, table, reuse=False, merge_group=False, rulesets=None):
        return check_fleet_003(replay_fetch(table), "t", "o/r", "main", reuse, merge_group, rulesets)

    def test_red_clud_shaped_burn_is_a_violation(self) -> None:
        (f,) = self._check(_CLUD_SHAPED, rulesets=SettingsReply(200, []))
        self.assertEqual((f.rule, f.status), ("FLEET-003", Status.VIOLATION))
        self.assertIn("22%", f.message)

    def test_green_with_reuse_check_wiring(self) -> None:
        self.assertEqual(self._check({}, reuse=True), [])

    def test_green_with_live_merge_queue(self) -> None:
        self.assertEqual(self._check({}, merge_group=True, rulesets=_QUEUE), [])

    def test_queue_ruleset_without_merge_group_trigger_is_not_live(self) -> None:
        (f,) = self._check(_CLUD_SHAPED, rulesets=_QUEUE)
        self.assertEqual(f.status, Status.VIOLATION)

    def test_unreadable_history_needs_review(self) -> None:
        (f,) = self._check({_RUNS: [403, {}]}, rulesets=SettingsReply(200, []))
        self.assertEqual(f.status, Status.NEEDS_REVIEW)
        (f,) = self._check({}, rulesets=SettingsReply(200, []))
        self.assertEqual(f.status, Status.NEEDS_REVIEW)

    def test_merge_group_with_unreadable_rulesets_needs_review(self) -> None:
        (f,) = self._check(_CLUD_SHAPED, merge_group=True, rulesets=SettingsReply(403, []))
        self.assertEqual(f.status, Status.NEEDS_REVIEW)

    def test_low_share_is_clean(self) -> None:
        table = {_RUNS: [200, {"workflow_runs": [_run("push", "main", 1), _run("pull_request", "x", 30)]}]}
        self.assertEqual(self._check(table, rulesets=SettingsReply(200, [])), [])
        self.assertEqual(burn_share([_run("push", "dev", 5)], "main").push_runs, 0)


def _scan(findings: tuple[Finding, ...]) -> RepoScan:
    return RepoScan(repo=T, default_branch="main", ci_toml_schema=None, workflow_count=1,
                    pr_entrypoints=(), cache_bytes=None, findings=findings)


class SyncIssuesTest(unittest.TestCase):
    def test_one_issue_per_repo_rule_subject_and_needs_review_skipped(self) -> None:
        f1 = Finding(rule="RUN-001", path="a.yml", message="x", fix="y")
        f2 = Finding(rule="RUN-001", path="a.yml", message="z", fix="y")
        f3 = Finding(rule="FLEET-001", path=None, message="m", fix="f")
        f4 = Finding(rule="FLEET-002", path="b.yml", message="m", fix="f", status=Status.NEEDS_REVIEW)
        actions = plan_repo(_scan((f1, f2, f3, f4)), [])
        self.assertEqual([(a.action, a.rule, a.subject) for a in actions],
                         [("create", "FLEET-001", f"repo:{T}"), ("create", "RUN-001", "a.yml")])
        self.assertIn(f"<!-- ci-lint-fingerprint: {fingerprint(T, 'RUN-001', 'a.yml')} -->", actions[1].body)

    def test_update_unchanged_close(self) -> None:
        f1 = Finding(rule="RUN-001", path="a.yml", message="x", fix="y")
        (created,) = plan_repo(_scan((f1,)), [])
        same = ExistingIssue(7, created.title, created.body, created.fingerprint)
        stale = ExistingIssue(8, created.title, "old body", created.fingerprint)
        gone = ExistingIssue(9, "ci-lint GEN-008: x", "b", "0" * 16)
        self.assertEqual([a.action for a in plan_repo(_scan((f1,)), [same])], ["unchanged"])
        self.assertEqual([(a.action, a.number) for a in plan_repo(_scan((f1,)), [stale, gone])],
                         [("update", 8), ("close", 9)])

    def test_existing_issues_read_via_get_only(self) -> None:
        body = f"<!-- ci-lint-fingerprint: {fingerprint(T, 'RUN-001', 'a.yml')} -->\nold"
        url = f"https://api.github.com/repos/{T}/issues?state=open&labels=ci-lint&per_page=100"
        fetch = replay_fetch({url: [200, [{"number": 3, "title": "t", "body": body}, {"number": 4, "pull_request": {}}]]})
        from ci_lint.fleet import FleetReport

        report = FleetReport(("zackees",), (_scan((Finding(rule="RUN-001", path="a.yml", message="x", fix="y"),)),), ())
        actions, errors = plan_sync(report, fetch, "t")
        self.assertEqual(errors, [])
        self.assertEqual([(a.action, a.number) for a in actions], [("update", 3)])

    def test_cli_refuses_without_dry_run(self) -> None:
        self.assertEqual(cli.main(["sync-issues", "--replay", str(GREEN)]), 2)


if __name__ == "__main__":
    unittest.main()
