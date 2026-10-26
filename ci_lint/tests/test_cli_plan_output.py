"""`ci_lint.cli._plan_github_output_lines`'s round-4B `cache_save` output --
`"true"`/`"false"` = `(plan.cache_mode == "write")`, so a writer-flow step
(e.g. `astral-sh/setup-uv`'s `save-cache`, or setup-soldr's wrapper) can bind
its plan-driven input straight to `needs.precheck.outputs.cache_save`
(`ci_lint.rules.tools._is_plan_expr` / `ci_lint.rules.cache_static
.check_cache_003_setup_uv` look for exactly that string in the input)."""

from __future__ import annotations

import unittest

from ci_lint.cli import _plan_github_output_lines
from ci_lint.plan import compute_plan
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture


def load_ci():
    _loaded = load_ci_toml(fixture("TAG-001", "green"))
    ci = _loaded.ci
    findings = _loaded.findings
    assert ci is not None, findings
    return ci


def _output_dict(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        key, _, value = line.partition("=")
        out[key] = value
    return out


class CacheSaveOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ci = load_ci()

    def test_read_mode_writes_cache_save_false(self) -> None:
        plan = compute_plan(self.ci, event_name="pull_request", title="fix a bug")
        self.assertEqual("read", plan.cache_mode)
        out = _output_dict(_plan_github_output_lines(plan, None))
        self.assertEqual("false", out["cache_save"])

    def test_write_mode_writes_cache_save_true(self) -> None:
        plan = compute_plan(self.ci, event_name="push", ref="refs/heads/main")
        self.assertEqual("write", plan.cache_mode)
        out = _output_dict(_plan_github_output_lines(plan, None))
        self.assertEqual("true", out["cache_save"])

    def test_cache_key_pr_output(self) -> None:
        """#23 §5 / CACHE-013: 'pr-<N>' in PR context, empty elsewhere."""

        plan = compute_plan(self.ci, event_name="pull_request", title="fix a bug")
        self.assertEqual("pr-42", _output_dict(_plan_github_output_lines(plan, None, 42))["cache_key_pr"])
        plan = compute_plan(self.ci, event_name="push", ref="refs/heads/main")
        self.assertEqual("", _output_dict(_plan_github_output_lines(plan, None))["cache_key_pr"])


if __name__ == "__main__":
    unittest.main()
