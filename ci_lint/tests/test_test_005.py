"""TEST-005 -- ci_lint/rules/sanitizer_lanes.py (sanitizer lanes are not PR gates)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.sanitizer_lanes import RULE, check_test_005

_ASAN_JOB = """jobs:
  asan:
    runs-on: ubuntu-24.04
    steps:
      - run: cmake -DCMAKE_CXX_FLAGS=-fsanitize=address ..
"""


def _repo(tmp: str, **workflows: str) -> Path:
    root = Path(tmp)
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in workflows.items():
        (wf / f"{name}.yml").write_text(text, encoding="utf-8")
    return root


class SanitizerLanesTest(unittest.TestCase):
    def test_a_asan_job_on_pull_request_is_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, ci="name: ci\non:\n  pull_request:\n    branches: [main]\n" + _ASAN_JOB)
            findings = check_test_005(root)
        self.assertEqual(1, len(findings))
        self.assertEqual(RULE, findings[0].rule)
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertEqual(".github/workflows/ci.yml", findings[0].path)
        self.assertEqual(6, findings[0].line)  # the `asan:` job id line
        self.assertIn("pull_request", findings[0].message)

    def test_a_merge_group_flow_list_is_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, ci="on: [push, merge_group]\n" + _ASAN_JOB)
            findings = check_test_005(root)
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])

    def test_b_asan_on_schedule_only_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, nightly="on:\n  schedule:\n    - cron: '0 3 * * *'\n  workflow_dispatch:\n" + _ASAN_JOB)
            self.assertEqual([], check_test_005(root))

    def test_c_asan_in_workflow_call_is_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, sanitizers="on:\n  workflow_call:\n" + _ASAN_JOB)
            findings = check_test_005(root)
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_c2_workflow_call_with_only_nightly_caller_is_silent(self) -> None:
        caller = (
            "on:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  s:\n    uses: ./.github/workflows/sanitizers.yml\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, sanitizers="on:\n  workflow_call:\n" + _ASAN_JOB, nightly=caller)
            self.assertEqual([], check_test_005(root))

    def test_c3_workflow_call_with_pr_caller_names_caller(self) -> None:
        caller = "on: pull_request\njobs:\n  s:\n    uses: ./.github/workflows/sanitizers.yml\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(tmp, sanitizers="on:\n  workflow_call:\n" + _ASAN_JOB, ci=caller)
            findings = check_test_005(root)
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])
        self.assertIn("ci.yml", findings[0].message)

    def test_d_sanitizer_token_only_in_comment_is_silent(self) -> None:
        text = (
            "on: pull_request\n"
            "# asan job: -fsanitize=address ASAN_OPTIONS=x\n"
            "jobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
            "      # - run: cmake -fsanitize=address\n"
            "      - run: make  # TSAN_OPTIONS later\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], check_test_005(_repo(tmp, ci=text)))

    def test_non_sanitizer_pr_workflow_and_word_boundary_are_silent(self) -> None:
        text = "on: pull_request\njobs:\n  casanova:\n    runs-on: x\n    steps:\n      - run: make\n"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], check_test_005(_repo(tmp, ci=text)))

    def test_only_the_sanitizer_job_is_flagged_in_mixed_workflow(self) -> None:
        text = (
            "on: pull_request\njobs:\n  test:\n    runs-on: x\n    steps:\n      - run: make\n"
            + _ASAN_JOB.split("jobs:\n", 1)[1]
        )
        with tempfile.TemporaryDirectory() as tmp:
            findings = check_test_005(_repo(tmp, ci=text))
        self.assertEqual(1, len(findings))
        self.assertIn("job `asan`", findings[0].message)

    def test_job_if_schedule_only_is_unreachable_from_pr(self) -> None:
        text = (
            "on: [pull_request, schedule]\njobs:\n  asan:\n    if: github.event_name == 'schedule'\n"
            "    runs-on: x\n    steps:\n      - run: make\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], check_test_005(_repo(tmp, ci=text)))


if __name__ == "__main__":
    unittest.main()
