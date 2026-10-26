"""REL-006 static clauses (#462): macOS signing secrets never reach PR jobs."""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.macos_signing import check_rel_006
from ci_lint.tests.test_complexity import _Repo

RELEASE_JOB = """\
  sign:
    {if_line}
    runs-on: macos-latest
    {env_line}
    steps:
      - uses: zackees/ci.yml/actions/app-signing-macos@0123456789abcdef0123456789abcdef01234567
        with:
          identity: ${{{{ secrets.MACOS_SIGNING_IDENTITY }}}}
"""


def _wf(on: str, if_line: str = "", env_line: str = "environment: release") -> str:
    return f"on: {on}\njobs:\n" + RELEASE_JOB.format(if_line=if_line, env_line=env_line)


def _run(text: str, extra: dict[str, str] | None = None):
    files = {".github/workflows/ci.yml": text, **(extra or {})}
    with _Repo(files) as root:
        return check_rel_006(root)


class Rel006Test(unittest.TestCase):
    def test_green_tag_confined_in_pr_workflow(self) -> None:
        self.assertEqual([], _run(_wf("[pull_request, push]", "if: startsWith(github.ref, 'refs/tags/v')")))

    def test_green_release_only_workflow(self) -> None:
        self.assertEqual([], _run(_wf("{push: {tags: ['v*']}}")))

    def test_green_dispatch_or_release_if(self) -> None:
        cond = "if: github.event_name == 'release' || github.event_name == 'workflow_dispatch'"
        self.assertEqual([], _run(_wf("[merge_group, release, workflow_dispatch]", cond)))

    def test_unconfined_pr_job_is_violation(self) -> None:
        findings = _run(_wf("[pull_request]"))
        self.assertEqual(1, len(findings))
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertEqual("REL-006", findings[0].rule)

    def test_merge_group_unconfined_is_violation(self) -> None:
        self.assertEqual([Status.VIOLATION], [f.status for f in _run(_wf("[merge_group]"))])

    def test_pull_request_target_is_violation_regardless(self) -> None:
        findings = _run(_wf("[pull_request_target]", "if: startsWith(github.ref, 'refs/tags/v')"))
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])
        self.assertIn("pull_request_target", findings[0].message)

    def test_pr_exclusion_only_is_needs_review(self) -> None:
        findings = _run(_wf("[pull_request, push]", "if: github.event_name != 'pull_request'"))
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_bare_secret_reference_without_action(self) -> None:
        text = (
            "on: pull_request\njobs:\n  build:\n    runs-on: macos-latest\n    environment: release\n"
            "    steps:\n      - run: echo \"$K\"\n        env:\n          K: ${{ secrets.MACOS_NOTARY_API_KEY }}\n"
        )
        findings = _run(text)
        self.assertEqual(1, len(findings))
        self.assertIn("MACOS_NOTARY_API_KEY", findings[0].message)

    def test_workflow_level_env_counts_for_every_job(self) -> None:
        text = (
            "on: pull_request\nenv:\n  P: ${{ secrets.MACOS_NOTARIZATION_PASSWORD }}\njobs:\n"
            "  a:\n    runs-on: ubuntu-latest\n    environment: release\n    steps:\n      - run: true\n"
        )
        self.assertEqual(1, len(_run(text)))

    def test_unrelated_secrets_and_jobs_pass(self) -> None:
        text = "on: pull_request\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ${{ secrets.GITHUB_TOKEN }}\n"
        self.assertEqual([], _run(text))

    def test_missing_environment_is_needs_review(self) -> None:
        findings = _run(_wf("{push: {tags: ['v*']}}", env_line=""))
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])
        self.assertIn("environment", findings[0].message)

    def test_local_reusable_called_from_pr_workflow(self) -> None:
        caller = "on: pull_request\njobs:\n  rel:\n    uses: ./.github/workflows/sign.yml\n"
        callee = _wf("workflow_call")
        findings = _run(caller, {".github/workflows/sign.yml": callee})
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])
        self.assertEqual(".github/workflows/sign.yml", findings[0].path)
        confined = caller.replace("    uses:", "    if: github.ref_type == 'tag'\n    uses:")
        self.assertEqual([], _run(confined, {".github/workflows/sign.yml": callee}))


if __name__ == "__main__":
    unittest.main()
