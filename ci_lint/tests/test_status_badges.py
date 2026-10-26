"""GEN-026 static clauses -- ci_lint/rules/status_badges.py (#124)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.status_badges import check_gen_026, resolve_default_branch, scan_badges
from ci_lint.tests.helpers import requires_yaml_tooling

BASE = "https://github.com/o/r/actions/workflows/build.yml/badge.svg"

DISPATCH_WF = """\
name: build
on:
  workflow_dispatch:
  workflow_call:
jobs:
  b:
    runs-on: ubuntu-24.04
    steps:
      - run: echo hi
"""

SWEEP_WF = """\
name: sweep
on:
  schedule:
    - cron: "0 3 * * *"
jobs:
  s:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0123456789012345678901234567890123456789
        with:
          ref: {ref}
"""


def _repo(root: Path, readme: str, *, origin_head: str | None = "main", workflows: dict[str, str] | None = None) -> None:
    (root / "README.md").write_text(readme, encoding="utf-8")
    if origin_head is not None:
        d = root / ".git" / "refs" / "remotes" / "origin"
        d.mkdir(parents=True)
        (d / "HEAD").write_text(f"ref: refs/remotes/origin/{origin_head}\n", encoding="utf-8")
    for name, text in (workflows or {}).items():
        wd = root / ".github" / "workflows"
        wd.mkdir(parents=True, exist_ok=True)
        (wd / name).write_text(text, encoding="utf-8")


def _run(readme: str, **kw: object) -> list[Finding]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _repo(root, readme, **kw)  # type: ignore[arg-type]
        return check_gen_026(root)


class Gen026Test(unittest.TestCase):
    def test_scan_parses_query(self) -> None:
        (b,) = scan_badges("README.md", f"[![b]({BASE}?branch=main&amp;event=push)](x)")
        self.assertEqual((b.workflow, b.branch, b.event), ("build.yml", "main", "push"))

    def test_badge_without_branch_is_violation(self) -> None:
        f = _run(f"![b]({BASE})\n")
        self.assertEqual([x.status for x in f], [Status.VIOLATION])
        self.assertIn("branch=main", f[0].fix)

    def test_badge_on_default_branch_is_clean(self) -> None:
        self.assertEqual(_run(f"![b]({BASE}?branch=main)\n"), [])

    def test_badge_on_other_branch_is_violation(self) -> None:
        f = _run(f"![b]({BASE}?branch=dev)\n")
        self.assertEqual([x.status for x in f], [Status.VIOLATION])

    def test_unknown_default_is_needs_review(self) -> None:
        f = _run(f"![b]({BASE}?branch=dev)\n", origin_head=None)
        self.assertEqual([x.status for x in f], [Status.NEEDS_REVIEW])

    def test_live_default_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _repo(root, f"![b]({BASE}?branch=master)\n", origin_head=None)
            self.assertEqual(check_gen_026(root, default_branch="master"), [])

    @requires_yaml_tooling
    def test_event_push_on_dispatch_only_workflow_is_violation(self) -> None:
        f = _run(f"![b]({BASE}?branch=main&event=push)\n", workflows={"build.yml": DISPATCH_WF})
        self.assertEqual(len(f), 1)
        self.assertIn("event=push", f[0].message)

    @requires_yaml_tooling
    def test_scheduled_sweep_on_branch_ref(self) -> None:
        f = _run("x\n", workflows={"sweep.yml": SWEEP_WF.format(ref="feature/x")})
        self.assertEqual([x.status for x in f], [Status.VIOLATION])
        self.assertEqual(_run("x\n", workflows={"sweep.yml": SWEEP_WF.format(ref="main")}), [])
        self.assertEqual(_run("x\n", workflows={"sweep.yml": SWEEP_WF.format(ref="${{ inputs.ref }}")}), [])

    def test_resolve_default_branch_worktree_gitfile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common = root / "main.git"
            (common / "refs" / "remotes" / "origin").mkdir(parents=True)
            (common / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/remotes/origin/trunk\n")
            wt_git = common / "worktrees" / "w"
            wt_git.mkdir(parents=True)
            (wt_git / "commondir").write_text("../..\n")
            wt = root / "wt"
            wt.mkdir()
            (wt / ".git").write_text(f"gitdir: {wt_git}\n")
            self.assertEqual(resolve_default_branch(wt), "trunk")


if __name__ == "__main__":
    unittest.main()


TEMPLATE_SWEEP_WF = """\
name: ci
on:
  schedule:
    - cron: "0 3 * * *"
  merge_group:
jobs:
  verify:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0123456789012345678901234567890123456789
      - uses: actions/checkout@0123456789012345678901234567890123456789
        with:
          repository: {linter}
          ref: 928cbbd0123456789012345678901234567890ab
          path: .ci-lint
  queue-only:
    if: ${{{{ github.event_name == 'merge_group' }}}}
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0123456789012345678901234567890123456789
        with:
          ref: feature/x
"""


class SweepTemplateShapeTest(unittest.TestCase):
    """forge-ci/ci.yml#480: template-python-rust-cmd's pinned ci-lint checkout."""

    @requires_yaml_tooling
    def test_pinned_ci_lint_checkout_and_queue_only_job_are_clean(self) -> None:
        for linter in ("zackees/ci.yml", "forge-ci/ci.yml"):
            wf = TEMPLATE_SWEEP_WF.format(linter=linter)
            self.assertEqual(_run("x\n", workflows={"ci.yml": wf}), [], linter)

    @requires_yaml_tooling
    def test_own_repo_checkout_with_branch_ref_still_fails(self) -> None:
        wf = SWEEP_WF.replace("ref: {ref}", "repository: ${{{{ github.repository }}}}\n          ref: {ref}")
        f = _run("x\n", workflows={"sweep.yml": wf.format(ref="feature/x")})
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].status, Status.VIOLATION)

    @requires_yaml_tooling
    def test_job_admitting_schedule_still_checked(self) -> None:
        wf = SWEEP_WF.replace("  s:\n", "  s:\n    if: ${{{{ github.event_name == 'schedule' }}}}\n")
        self.assertEqual(len(_run("x\n", workflows={"sweep.yml": wf.format(ref="feature/x")})), 1)
