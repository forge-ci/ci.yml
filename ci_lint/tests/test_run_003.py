"""RUN-003 (#150): native arm64 Linux off the PR/push path -- ci_lint/rules/workflows.py."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.workflows import check_run_003, is_native_arm_linux_label
from ci_lint.schema import Platform, load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling
from ci_lint.workflow_scan import load_workflows

_PR = "on:\n  pull_request:\n  push:\n    branches: [main]\n"


def _run(on: str, jobs: str, platforms: dict[str, Platform] | None = None):
    ci = load_ci_toml(fixture("RUN-001", "green")).ci
    if platforms is not None:
        ci = dataclasses.replace(ci, platforms=platforms)
    with tempfile.TemporaryDirectory() as tmp:
        wf_dir = Path(tmp) / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(f"name: ci\n{on}jobs:\n{jobs}", encoding="utf-8")
        return check_run_003(ci, load_workflows(Path(tmp)))


def _job(runs_on: str, extra: str = "") -> str:
    return f"  arm:\n    runs-on: {runs_on}\n{extra}    steps:\n      - run: echo\n"


@requires_yaml_tooling
class Run003Test(unittest.TestCase):
    def test_label_detection(self) -> None:
        self.assertTrue(is_native_arm_linux_label("ubuntu-24.04-arm"))
        self.assertTrue(is_native_arm_linux_label("ubuntu-22.04-arm"))
        self.assertFalse(is_native_arm_linux_label("windows-11-arm"))
        self.assertFalse(is_native_arm_linux_label("ubuntu-24.04"))

    def test_unguarded_arm_on_pr_is_violation(self) -> None:
        f = _run(_PR, _job("ubuntu-24.04-arm"))
        self.assertEqual([x.status for x in f], [Status.VIOLATION])

    def test_x64_is_clean(self) -> None:
        self.assertEqual(_run(_PR, _job("ubuntu-24.04")), [])

    def test_schedule_only_workflow_is_clean(self) -> None:
        on = "on:\n  schedule:\n    - cron: '0 3 * * *'\n  workflow_dispatch:\n"
        self.assertEqual(_run(on, _job("ubuntu-24.04-arm")), [])

    def test_tags_only_push_and_feature_branch_push_are_clean(self) -> None:
        self.assertEqual(_run("on:\n  push:\n    tags: ['v*']\n", _job("ubuntu-24.04-arm")), [])
        self.assertEqual(
            _run("on:\n  push:\n    branches: [release/x]\n", _job("ubuntu-24.04-arm")), []
        )

    def test_confining_if_is_clean(self) -> None:
        for cond in (
            "github.event_name == 'schedule'",
            "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'",
            "github.event_name != 'pull_request' && github.event_name != 'push'",
            "startsWith(github.ref, 'refs/tags/')",
            "contains(needs.plan.outputs.tags, 'ci-full')",
        ):
            with self.subTest(cond=cond):
                self.assertEqual(_run(_PR, _job("ubuntu-24.04-arm", f'    if: "{cond}"\n')), [])

    def test_unprovable_if_is_needs_review(self) -> None:
        f = _run(_PR, _job("ubuntu-24.04-arm", "    if: needs.plan.outputs.arm == 'true'\n"))
        self.assertEqual([x.status for x in f], [Status.NEEDS_REVIEW])

    def test_literal_matrix_resolved(self) -> None:
        extra = "    strategy:\n      matrix:\n        os: [ubuntu-24.04, ubuntu-24.04-arm]\n"
        f = _run(_PR, _job("${{ matrix.os }}", extra))
        self.assertEqual([x.status for x in f], [Status.VIOLATION])
        extra = "    strategy:\n      matrix:\n        os: [ubuntu-24.04, macos-15]\n"
        self.assertEqual(_run(_PR, _job("${{ matrix.os }}", extra)), [])

    def test_platform_lanes_matrix_uses_ci_toml(self) -> None:
        extra = (
            "    needs: plan\n    strategy:\n      matrix:\n"
            "        lane: ${{ fromJSON(needs.plan.outputs.platform_lanes_json) }}\n"
        )
        jobs = _job("${{ matrix.lane.runs_on }}", extra)
        arm = {"linux-arm": Platform("linux-arm", "aarch64-unknown-linux-gnu",
                                     "ubuntu-24.04-arm", "linux")}
        x64 = {"linux": Platform("linux", "x86_64-unknown-linux-gnu", "ubuntu-24.04", "linux")}
        f = _run(_PR, jobs, arm)
        self.assertEqual([x.status for x in f], [Status.VIOLATION])
        self.assertIn("[platforms.linux-arm]", f[0].message)
        self.assertEqual(_run(_PR, jobs, x64), [])

    def test_unresolvable_runs_on_is_needs_review(self) -> None:
        f = _run(_PR, _job("${{ vars.RUNNER }}"))
        self.assertEqual([x.status for x in f], [Status.NEEDS_REVIEW])


if __name__ == "__main__":
    unittest.main()
