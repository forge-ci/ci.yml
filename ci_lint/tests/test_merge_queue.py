"""GEN-022 -- ci_lint/rules/merge_queue.py (zackees/ci.yml#405)."""

from __future__ import annotations

import unittest

from ci_lint.rules.merge_queue import check_gen_022
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


def _messages(kind: str) -> list[str]:
    repo = fixture("GEN-022", kind)
    _loaded = load_ci_toml(repo)
    ci = _loaded.ci
    findings = _loaded.findings
    assert ci is not None, findings
    found = check_gen_022(ci, repo)
    assert {f.rule for f in found} <= {"GEN-022"}
    return [f.message for f in found]


class MergeQueueTest(unittest.TestCase):
    @requires_yaml_tooling
    def test_no_merge_group_trigger_fails(self) -> None:
        messages = _messages("red")
        self.assertEqual(1, len(messages))
        self.assertIn("merge queue", messages[0])

    @requires_yaml_tooling
    def test_janitor_before_other_jobs_fails(self) -> None:
        messages = _messages("red-early")
        self.assertTrue(any("missing needs: build" in m for m in messages), messages)
        self.assertTrue(any("always()" in m for m in messages), messages)

    @requires_yaml_tooling
    def test_final_janitor_is_green(self) -> None:
        self.assertEqual([], _messages("green"))

    @requires_yaml_tooling
    def test_merge_queue_without_budget_needs_no_janitor(self) -> None:
        self.assertEqual([], _messages("green-no-budget"))

    @requires_yaml_tooling
    def test_janitor_may_need_cancel_superseded(self) -> None:
        self.assertEqual([], _messages("green-cancel"))

    @requires_yaml_tooling
    def test_other_job_needing_cancel_superseded_fails(self) -> None:
        messages = _messages("red-cancel")
        self.assertEqual(1, len(messages), messages)
        self.assertIn("job 'test' needs 'cancel-superseded'", messages[0])
