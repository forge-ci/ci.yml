"""WF-004: a skipped `needs:` skips its dependents unless the dependent calls a status function."""

from __future__ import annotations

import unittest

from ci_lint.rules.workflows import check_wf_004
from ci_lint.workflow_scan import ParsedYamlFile
from ci_lint.yaml_io import LoadStatus


def _wf(jobs: dict[str, dict]) -> ParsedYamlFile:
    return ParsedYamlFile(
        path=".github/workflows/ci.yml",
        document={"jobs": jobs},
        status=LoadStatus.OK,
        reason=None,
    )


class Wf004Test(unittest.TestCase):
    def test_event_restricted_need_without_status_function_is_flagged(self) -> None:
        wf = _wf(
            {
                "decide": {"if": "github.event_name == 'push'"},
                "gate": {"needs": ["decide"], "if": "github.event.action != 'closed'"},
            }
        )
        findings = check_wf_004([wf])
        self.assertEqual([f.rule for f in findings], ["WF-004"])
        self.assertEqual(findings[0].status.value, "needs_review")
        self.assertIn("'gate'", findings[0].message)
        self.assertIn("decide", findings[0].message)

    def test_always_overrides_the_cascade(self) -> None:
        wf = _wf(
            {
                "decide": {"if": "github.event_name == 'push'"},
                "gate": {"needs": ["decide"], "if": "always() && !cancelled()"},
            }
        )
        self.assertEqual(check_wf_004([wf]), [])

    def test_any_status_function_overrides_the_cascade(self) -> None:
        """template-python-rust-cmd run 40285095554: `fast` with `!cancelled()`
        succeeded while its `needs:` reuse-decision and cache-maint skipped."""

        for cond in (
            "${{ !cancelled() && !failure() && needs.x.outputs.reuse != 'true' }}",
            "!cancelled()",
            "cancelled()",
            "failure()",
            "always()",
        ):
            with self.subTest(cond=cond):
                wf = _wf(
                    {
                        "decide": {"if": "github.event_name == 'push'"},
                        "gate": {"needs": ["decide"], "if": cond},
                    }
                )
                self.assertEqual(check_wf_004([wf]), [])

    def test_bare_success_is_still_flagged(self) -> None:
        for cond in ("success()", "success() && github.event.action != 'closed'"):
            with self.subTest(cond=cond):
                wf = _wf(
                    {
                        "decide": {"if": "github.event_name == 'push'"},
                        "gate": {"needs": ["decide"], "if": cond},
                    }
                )
                self.assertEqual([f.rule for f in check_wf_004([wf])], ["WF-004"])

    def test_status_function_name_inside_identifier_does_not_count(self) -> None:
        wf = _wf(
            {
                "decide": {"if": "github.event_name == 'push'"},
                "gate": {"needs": ["decide"], "if": "needs.x.outputs.no_failure() == 'y'"},
            }
        )
        self.assertEqual([f.rule for f in check_wf_004([wf])], ["WF-004"])

    def test_no_event_restriction_is_clean(self) -> None:
        """A mode/output gate is a skip too, but it is the repository's own
        selection logic and far too common -- flagging it would bury the
        event-restricted case that silently kills a PR gate."""

        wf = _wf(
            {
                "build": {"if": "needs.precheck.outputs.mode == 'full'"},
                "test": {"needs": ["build"], "if": "!cancelled()"},
            }
        )
        self.assertEqual(check_wf_004([wf]), [])

    def test_needs_given_as_a_scalar_string(self) -> None:
        wf = _wf(
            {
                "decide": {"if": "github.ref == 'refs/heads/main'"},
                "gate": {"needs": "decide"},
            }
        )
        self.assertEqual([f.rule for f in check_wf_004([wf])], ["WF-004"])

    def test_unrelated_dependency_is_clean(self) -> None:
        wf = _wf(
            {
                "decide": {"if": "github.event_name == 'push'"},
                "gate": {"needs": ["static"]},
            }
        )
        self.assertEqual(check_wf_004([wf]), [])
