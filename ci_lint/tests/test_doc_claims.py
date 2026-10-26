"""GEN-010 static half -- ci_lint/rules/doc_claims.py (M2-23, issue #5).

RED: a doc claims native Windows/macOS Dylint, but the repo's own
workflow only runs Dylint on Linux (offline-checkable -- a VIOLATION).
GREEN: the doc's claim matches an actual native Dylint job.

The merge-queue/branch-protection claim shapes are exercised here only for
detection (they always come back NEEDS_REVIEW from the static-only path,
since confirming them needs a live API read); their live resolution is
covered by ci_lint.tests.test_settings_audit's GEN-010 cases.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.doc_claims import check_gen_010_static, scan_text
from ci_lint.tests.helpers import requires_yaml_tooling

RED_WORKFLOW = """\
name: CI
on: [push]
jobs:
  dylint:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - run: python3 ci/dylint.py
"""

GREEN_WORKFLOW = """\
name: CI
on: [push]
jobs:
  dylint-windows:
    runs-on: windows-2022
    timeout-minutes: 10
    steps:
      - run: python3 ci/dylint.py
"""

CLAIM_DOC = "Every PR runs native Linux, Windows, and macOS Dylint.\n"


def _write_repo(tmp: Path, workflow: str) -> None:
    (tmp / ".github" / "workflows").mkdir(parents=True)
    (tmp / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
    (tmp / "docs" / "architecture").mkdir(parents=True)
    (tmp / "docs" / "architecture" / "ci.md").write_text(CLAIM_DOC, encoding="utf-8")


class Gen010StaticTest(unittest.TestCase):
    def test_red_native_dylint_claim_without_native_job_is_violation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_repo(root, RED_WORKFLOW)
            gen010 = check_gen_010_static(root)
            findings, claims = gen010.findings, gen010.claims
            native = [f for f in findings if f.rule == "GEN-010" and "native" in f.message]
            self.assertTrue(native, findings)
            self.assertEqual(native[0].status, Status.VIOLATION)
            self.assertTrue(any(c.kind == "native_dylint" for c in claims))

    @requires_yaml_tooling  # the native job is only visible in parsed workflow YAML
    def test_green_native_dylint_claim_with_matching_job_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_repo(root, GREEN_WORKFLOW)
            findings = check_gen_010_static(root).findings
            native = [f for f in findings if f.rule == "GEN-010" and "native" in f.message]
            self.assertEqual(native, [])

    def test_no_claim_no_workflow_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "README.md").write_text("Just a normal readme.\n", encoding="utf-8")
            gen010 = check_gen_010_static(root)
            findings, claims = gen010.findings, gen010.claims
            self.assertEqual(findings, [])
            self.assertEqual(claims, [])

    def test_merge_queue_claim_is_needs_review_offline(self) -> None:
        text = "This repo enforces merge queue protection on main.\n"
        claims = scan_text("AGENTS.md", text)
        self.assertTrue(any(c.kind == "merge_queue" for c in claims))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text(text, encoding="utf-8")
            findings = check_gen_010_static(root).findings
            mq = [f for f in findings if f.rule == "GEN-010"]
            self.assertTrue(mq)
            self.assertTrue(all(f.status == Status.NEEDS_REVIEW for f in mq))

    def test_branch_protection_claim_is_needs_review_offline(self) -> None:
        text = "Branch protection is enabled and required status checks are enforced.\n"
        claims = scan_text("CLAUDE.md", text)
        self.assertTrue(any(c.kind == "branch_protection" for c in claims))

    def test_discussing_the_rule_itself_is_not_a_claim(self) -> None:
        """This repository's own policy docs discuss GEN-010's mechanics
        at length without asserting THIS repo's CI has a merge queue --
        the narrow affirmative-claim regexes must not fire on that prose."""

        text = (
            "GEN-010: a repo's documentation or CI code asserts that full "
            "validation happens at a merge queue or protected branch, but "
            "the repo has no corresponding branch protection rule or merge "
            "queue configured.\n"
        )
        claims = scan_text("docs/policy-general.md", text)
        self.assertEqual(claims, [])


if __name__ == "__main__":
    unittest.main()
