"""RUN-004: forge-quick jobs are small, pinned, container-free and behind the
kill switch -- ci_lint/rules/forge_quick.py."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.precheck import YAML_DEPENDENT_RULES
from ci_lint.rules.forge_quick import ALLOWLIST_PATH, check_run_004, load_allowlist, parse_allowlist
from ci_lint.tests.helpers import requires_yaml_tooling

KILL = "${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'ubuntu-latest' }}"
POOL_SELECT = "forge-ci/ci.yml/actions/pool-select@ed73926bc2de1b5d71f50907cd93920daeecbe78"
APP_TOKEN = "actions/create-github-app-token@f418cff399f7aef4c2f0ad47e25ca7a6abbdd6fc"
README = Path(__file__).resolve().parents[2] / "actions" / "pool-select" / "README.md"


def _check(jobs: str):
    with tempfile.TemporaryDirectory() as tmp:
        wf_dir = Path(tmp) / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(f"name: ci\non: [push]\njobs:\n{jobs}", encoding="utf-8")
        return check_run_004(Path(tmp))


def _job(runs_on: str = KILL, timeout: str = "2", steps: str = "", extra: str = "") -> str:
    steps = steps or "      - run: echo ok\n"
    quoted = runs_on.replace('"', '\\"')
    return f"  quick:\n    runs-on: \"{quoted}\"\n    timeout-minutes: {timeout}\n{extra}    steps:\n{steps}"


@requires_yaml_tooling
class Run004Test(unittest.TestCase):
    def test_green_kill_switch_pinned_allowlisted(self) -> None:
        steps = f"      - uses: {APP_TOKEN} # v3.4.0\n        id: t\n      - uses: {POOL_SELECT}\n      - run: echo ok\n"
        self.assertEqual([], _check(_job(steps=steps)))

    def test_green_trusted_event_guard_in_kill_switch(self) -> None:
        runs_on = (
            "${{ (vars.FORGE_QUICK_LABELS != '' && github.event_name != 'pull_request') "
            "&& fromJSON(vars.FORGE_QUICK_LABELS) || 'ubuntu-24.04' }}"
        )
        self.assertEqual([], _check(_job(runs_on=runs_on, timeout="1")))

    def test_jobs_not_on_forge_quick_are_ignored(self) -> None:
        jobs = (
            "  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 30\n    container: node:22\n"
            "    steps:\n      - uses: actions/checkout@v4\n"
            "  pool:\n    runs-on: \"${{ vars.FORGE_POOL_LABELS != '' && fromJSON(vars.FORGE_POOL_LABELS) || 'ubuntu-latest' }}\"\n"
            "    timeout-minutes: 60\n    steps:\n      - run: echo hi\n"
        )
        self.assertEqual([], _check(jobs))

    def test_red_literal_label_long_timeout_unpinned_container_services(self) -> None:
        jobs = (
            "  quick:\n    runs-on: [self-hosted, forge-quick]\n    timeout-minutes: 5\n"
            "    container: ubuntu:24.04\n    services:\n      redis:\n        image: redis\n"
            "    steps:\n      - uses: actions/checkout@v4\n      - uses: some/other-action@0123456789012345678901234567890123456789\n"
        )
        findings = _check(jobs)
        messages = "\n".join(f.message for f in findings)
        self.assertEqual(6, len(findings), messages)
        self.assertTrue(all(f.rule == "RUN-004" and f.status == Status.VIOLATION for f in findings))
        self.assertIn("timeout-minutes", messages)
        self.assertIn("actions/checkout@v4", messages)
        self.assertIn("some/other-action", messages)
        self.assertIn("`container:`", messages)
        self.assertIn("`services:`", messages)
        self.assertIn("kill switch", messages)
        checkout = next(f for f in findings if "actions/checkout@v4" in f.message)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", checkout.fix)
        self.assertEqual(".github/workflows/ci.yml", findings[0].path)
        self.assertEqual(4, findings[0].line)

    def test_red_allowlisted_action_at_another_sha(self) -> None:
        steps = "      - uses: forge-ci/ci.yml/actions/pool-select@30bb7510000000000000000000000000000000aa\n"
        findings = _check(_job(steps=steps))
        self.assertEqual(1, len(findings))
        self.assertIn(POOL_SELECT, findings[0].fix)

    def test_red_local_and_docker_actions(self) -> None:
        steps = "      - uses: ./.github/actions/x\n      - uses: docker://alpine:3\n"
        self.assertEqual(2, len(_check(_job(steps=steps))))

    def test_red_kill_switch_shapes(self) -> None:
        bad = (
            "${{ fromJSON(vars.FORGE_QUICK_LABELS) }}",  # no unset test, no fallback
            "${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'self-hosted' }}",
            "${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'forge-quick' }}",
            "${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'windows-latest' }}",
            "${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || fromJSON('[\"self-hosted\"]') }}",
        )
        for runs_on in bad:
            with self.subTest(runs_on=runs_on):
                findings = _check(_job(runs_on=runs_on))
                self.assertEqual(["kill switch"], [m for f in findings for m in ("kill switch",) if m in f.message])

    def test_red_timeout_expression_or_missing(self) -> None:
        self.assertEqual(1, len(_check(_job(timeout="${{ inputs.t }}"))))
        jobs = f"  quick:\n    runs-on: \"{KILL}\"\n    steps:\n      - run: echo ok\n"
        findings = _check(jobs)
        self.assertEqual(1, len(findings))
        self.assertIn("timeout-minutes", findings[0].message)

    def test_matrix_value_naming_forge_quick_is_reached(self) -> None:
        jobs = (
            "  quick:\n    strategy:\n      matrix:\n        os: [forge-quick]\n"
            "    runs-on: ${{ matrix.os }}\n    timeout-minutes: 2\n    steps:\n      - run: echo ok\n"
        )
        findings = _check(jobs)
        self.assertEqual(1, len(findings))
        self.assertIn("matrix", findings[0].message)

    def test_needs_output_from_forge_quick_labels_is_needs_review(self) -> None:
        jobs = (
            "  pick:\n    runs-on: ubuntu-latest\n    timeout-minutes: 2\n"
            "    outputs:\n      labels: ${{ vars.FORGE_QUICK_LABELS }}\n    steps:\n      - run: echo\n"
            "  work:\n    needs: pick\n    runs-on: ${{ fromJSON(needs.pick.outputs.labels) }}\n"
            "    timeout-minutes: 30\n    steps:\n      - run: echo\n"
        )
        findings = _check(jobs)
        self.assertEqual(1, len(findings))
        self.assertEqual(Status.NEEDS_REVIEW, findings[0].status)
        self.assertIn("'work'", findings[0].message)

    def test_pool_select_readme_example_passes(self) -> None:
        text = README.read_text(encoding="utf-8")
        block = re.search(r"## Usage\n\n```yaml\n(.*?)```", text, re.DOTALL)
        self.assertIsNotNone(block)
        assert block is not None
        example = block.group(1)
        self.assertIn("vars.FORGE_QUICK_LABELS", example)
        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            body = example.replace("<40-hex sha>", POOL_SELECT.partition("@")[2])
            (wf_dir / "ci.yml").write_text(f"name: ci\non: [push]\n{body}", encoding="utf-8")
            self.assertEqual([], check_run_004(Path(tmp)))


class Run004AllowlistTest(unittest.TestCase):
    def test_shipped_allowlist_parses_and_pins_pool_select(self) -> None:
        allowlist = load_allowlist()
        self.assertTrue(allowlist.allows(POOL_SELECT))
        self.assertTrue(allowlist.allows(APP_TOKEN))
        self.assertTrue(allowlist.allows("astral-sh/setup-uv@1db35dd8eee11c5894872e0b383ae6c406cf738d"))
        self.assertFalse(allowlist.allows("actions/checkout@v7.0.1"))
        self.assertTrue(ALLOWLIST_PATH.is_file())

    def test_allowlist_rejects_unpinned_or_malformed(self) -> None:
        with self.assertRaises(ValueError):
            parse_allowlist('schema = 1\n[[action]]\nrepo = "a/b"\npath = ""\nsha = "v1"\n')
        with self.assertRaises(ValueError):
            parse_allowlist('schema = 2\n[[action]]\nrepo = "a/b"\nsha = "' + "0" * 40 + '"\n')
        with self.assertRaises(ValueError):
            parse_allowlist("schema = 1\n")

    def test_several_pins_allowed_and_the_last_is_suggested(self) -> None:
        old, new = "1" * 40, "2" * 40
        allowlist = parse_allowlist(
            "schema = 1\n"
            f'[[action]]\nrepo = "a/b"\npath = "x"\nsha = "{old}"\n'
            f'[[action]]\nrepo = "a/b"\npath = "x"\nsha = "{new}"\n'
        )
        self.assertTrue(allowlist.allows(f"a/b/x@{old}") and allowlist.allows(f"a/b/x@{new}"))
        self.assertEqual(allowlist.pinned_for(f"a/b/x@{'3' * 40}"), f"a/b/x@{new}")

    def test_run_004_is_yaml_dependent(self) -> None:
        self.assertIn("RUN-004", YAML_DEPENDENT_RULES)


if __name__ == "__main__":
    unittest.main()
