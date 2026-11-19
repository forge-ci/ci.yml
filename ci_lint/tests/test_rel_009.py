"""REL-009 (talos-hq/clud 2.8.67/2.8.68): no external package channel in a release."""

from __future__ import annotations

import unittest

from ci_lint.exceptions import UNWAIVABLE_RULES
from ci_lint.finding import Status
from ci_lint.rules.release_channels import channels_in, check_rel_009
from ci_lint.tests.helpers import requires_yaml_tooling
from ci_lint.tests.test_complexity import _Repo

TAG_ON = "on:\n  push:\n    tags: ['v*']\n  workflow_dispatch:\n"
CHANNEL_ON = (
    "on:\n  release:\n    types: [released]\n  workflow_run:\n    workflows: [Release]\n"
    "    types: [completed]\n  workflow_dispatch:\n"
)
TAP_PUSH = 'git clone "https://x-access-token:${T}@github.com/acme/homebrew-tap.git" tap'


def _job(name: str, run: str, extra: str = "") -> str:
    return f"  {name}:\n    runs-on: ubuntu-latest\n{extra}    steps:\n      - run: {run}\n"


def _run(files: dict[str, str]):
    with _Repo(files) as root:
        return check_rel_009(root)


class Rel009PatternTest(unittest.TestCase):
    def test_publication_steps_match(self) -> None:
        for text, channel in (
            (TAP_PUSH, "Homebrew"),
            ("brew bump-formula-pr --url x acme", "Homebrew"),
            ("uses: mislav/bump-homebrew-formula-action@v3", "Homebrew"),
            ("wingetcreate update Acme.Tool", "winget"),
            ("uses: vedantmgoyal9/winget-releaser@v2", "winget"),
            ("git push https://github.com/acme/scoop-bucket", "Scoop"),
            ("choco push acme.nupkg --source https://push.chocolatey.org/", "Chocolatey"),
            ("dput ppa:acme/tools acme_1.0_source.changes", "apt PPA"),
            ("git clone ssh://aur@aur.archlinux.org/acme.git", "AUR"),
            ("snapcraft upload --release=stable acme.snap", "Snap"),
        ):
            self.assertIn(channel, channels_in(text), text)

    def test_runner_tooling_does_not_match(self) -> None:
        for text in (
            "brew install fish",
            "sudo apt-get install -y zsh",
            "choco install ninja",
            "scoop install 7zip",
            "nix-build ci/nixos/test.nix",
        ):
            self.assertEqual([], channels_in(text), text)


class Rel009Test(unittest.TestCase):
    @requires_yaml_tooling
    def test_tap_job_in_a_tag_release_is_violation(self) -> None:
        release = TAG_ON + "jobs:\n" + _job("publish", "gh release create v1 dist/*") + _job(
            "homebrew", TAP_PUSH, "    needs: publish\n"
        )
        findings = _run({".github/workflows/release.yml": release})
        self.assertEqual(["REL-009"], [f.rule for f in findings])
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertIn("'homebrew'", findings[0].message)

    @requires_yaml_tooling
    def test_dispatch_only_release_that_publishes_is_in_scope(self) -> None:
        pypi = (
            "  pypi:\n    runs-on: ubuntu-latest\n    environment: pypi\n    steps:\n"
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
        )
        release = "on:\n  workflow_dispatch:\njobs:\n" + pypi + _job("winget", "wingetcreate update Acme")
        findings = _run({".github/workflows/release.yml": release})
        self.assertEqual(["REL-009"], [f.rule for f in findings])

    @requires_yaml_tooling
    def test_called_workflow_is_scanned(self) -> None:
        release = TAG_ON + "jobs:\n  channels:\n    uses: ./.github/workflows/channels.yml\n"
        channels = "on:\n  workflow_call:\njobs:\n" + _job("tap", TAP_PUSH)
        findings = _run({".github/workflows/release.yml": release, ".github/workflows/channels.yml": channels})
        self.assertEqual([".github/workflows/channels.yml"], [f.path for f in findings])

    @requires_yaml_tooling
    def test_separate_channel_workflow_is_compliant(self) -> None:
        release = TAG_ON + "jobs:\n" + _job("publish", "gh release create v1 dist/*")
        channel = CHANNEL_ON + "jobs:\n" + _job("tap", "gh release download v1 --pattern SHA256SUMS && " + TAP_PUSH)
        self.assertEqual(
            [], _run({".github/workflows/release.yml": release, ".github/workflows/homebrew-tap.yml": channel})
        )

    @requires_yaml_tooling
    def test_tooling_install_in_release_is_clean(self) -> None:
        release = TAG_ON + "jobs:\n" + _job("accept", "brew install fish && pytest")
        self.assertEqual([], _run({".github/workflows/release.yml": release}))

    def test_rule_is_unwaivable(self) -> None:
        self.assertIn("REL-009", UNWAIVABLE_RULES)


if __name__ == "__main__":
    unittest.main()
