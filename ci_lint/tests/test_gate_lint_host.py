"""GATE-016: lint never runs under bosn -- ci_lint/gate_lint_host.py.

The fixtures are the real fleet shapes measured 2026-11-21: clud and
template-python-rust-cmd run `bosn ci run ... --job dylint`, zccache and
fbuild declare `tools = ["bosn"]` on their clippy/dylint lanes.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from ci_lint.cli import main
from ci_lint.finding import Status
from ci_lint.gate_lint_host import check_gate_016, lint_like, runs_lint_command
from ci_lint.gate_lint_replay import lint_command_in
from ci_lint.lane_cache import LaneConfig
from ci_lint.tests.helpers import requires_yaml_tooling

BOSN_RUN = ("bosn", "ci", "run", "--workspace", ".", "--trigger", "pr", "--wait", "--json")


def _lane(lane_id: str, run: tuple[str, ...], tools: tuple[str, ...] = ()) -> LaneConfig:
    return LaneConfig(id=lane_id, run=run, tools=tools)


class LintLikeTest(unittest.TestCase):
    def test_words(self) -> None:
        for name in ("lint", "dylint", "clippy", "fmt", "format", "lint-linux-x64", "rust_fmt-check", "dylint_gate"):
            self.assertTrue(lint_like(name), name)
        for name in ("ci_lint", "static-checks", "unit-tests", "build", "check", "linter-pin", ""):
            self.assertFalse(lint_like(name), name)

    def test_lint_commands(self) -> None:
        for cmd in ("cargo clippy --all-targets", "soldr cargo clippy -- -D warnings", "cargo +nightly fmt --check",
                    "cargo dylint --all", "soldr lint --check", "cd /w && cargo-clippy", "soldr dylint --all"):
            self.assertTrue(runs_lint_command(cmd), cmd)
        for cmd in ("cargo test", "soldr cargo nextest run", "soldr linter", "bosn run --task test", "soldr dylint prepare"):
            self.assertFalse(runs_lint_command(cmd), cmd)


class Gate016Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, text: str) -> None:
        (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.tmp / rel).write_text(text, encoding="utf-8")

    def test_bosn_ci_run_lint_jobs(self) -> None:
        """Clause 2 (clud / template-python-rust-cmd shape): any lane, whatever
        its name, that runs a lint job through `bosn ci run`."""

        lanes = (
            _lane("static", (*BOSN_RUN, "--job", "static-checks"), ("bosn", "docker")),
            _lane("dylint", (*BOSN_RUN, "--job", "dylint"), ("bosn", "docker")),
            _lane("clippy", (*BOSN_RUN, "--job", "lint-linux-x64"), ("bosn", "docker")),
            _lane("other", (*BOSN_RUN, "--job=fmt")),
            _lane("unit", (*BOSN_RUN, "--job", "test-linux-x64-unit"), ("bosn", "docker")),
        )
        msgs = [f.message for f in check_gate_016(self.tmp, (), lanes)]
        self.assertEqual(msgs, [
            "lane 'dylint' runs lint job 'dylint' through `bosn ci run`",
            "lane 'clippy' runs lint job 'lint-linux-x64' through `bosn ci run`",
            "lane 'other' runs lint job 'fmt' through `bosn ci run`",
        ])
        self.assertTrue(all(f.rule == "GATE-016" and "soldr lint" in f.fix for f in check_gate_016(self.tmp, (), lanes)))

    def test_gate_argv_job(self) -> None:
        found = check_gate_016(self.tmp, (*BOSN_RUN, "--job", "lint"))
        self.assertEqual([f.message for f in found], ["the gate runs lint job 'lint' through `bosn ci run`"])

    def test_lint_lane_with_bosn_tool(self) -> None:
        """Clause 1 (zccache / fbuild shape): a lint lane declaring bosn."""

        script = ("uv", "run", "--no-project", "python", "ci/local_gate.py", "--lane")
        lanes = (
            _lane("check", (*script, "check"), ("bosn",)),
            _lane("dylint", (*script, "dylint"), ("bosn",)),
            _lane("lint", (*script, "lint"), ("soldr",)),
            _lane("quick", ("python3", "ci/dylint_gate.py"), ("bosn", "docker")),
            _lane("tests", (*script, "tests"), ("bosn",)),
        )
        msgs = [f.message for f in check_gate_016(self.tmp, (), lanes)]
        self.assertEqual(msgs, [
            "lint lane 'dylint' declares 'bosn' in tools",
            "lint lane 'quick' declares 'bosn' in tools",
        ])

    def test_lint_lane_run_invokes_bosn(self) -> None:
        lanes = (
            _lane("rust-lint", ("bosn", "run", "--task", "clippy")),
            _lane("x", ("bosn", "exec", "--", "soldr", "lint", "--check"), ("bosn",)),
            _lane("tests", ("bosn", "run", "--task", "test"), ("bosn",)),
        )
        msgs = [f.message for f in check_gate_016(self.tmp, (), lanes)]
        self.assertEqual(msgs, [
            "lint lane 'rust-lint' its run invokes bosn",
            "lint lane 'x' declares 'bosn' in tools and its run invokes bosn",
        ])

    def test_host_soldr_lint_is_clean(self) -> None:
        lanes = (
            _lane("lint", ("soldr", "lint", "--check"), ("soldr",)),
            _lane("tests", (*BOSN_RUN, "--job", "test"), ("bosn", "docker")),
            _lane("build", ("bosn", "run", "--task", "build"), ("bosn",)),
        )
        self.assertEqual(check_gate_016(self.tmp, ("python3", "-m", "ci_lint", "selftest"), lanes), [])

    def test_bosn_tasks(self) -> None:
        self._write("bosn.toml", (
            '[task.test]\ncmd = "soldr cargo nextest run"\n'
            '[task.lint]\ncmd = "echo hi"\n'
            '[task."rust-checks"]\ncmd = "soldr cargo clippy --all-targets -- -D warnings"\n'
            '[task.all]\ncmd = ["sh", "-c", "soldr lint --check"]\n'
            '[task.build]\ncmd = "soldr cargo build --release"\n'
            '[task.migration-lint-test]\ncmd = "pytest tests/test_lint.py"\n'
            '[task.dylint-ui]\ncmd = "cd dylints/x && soldr cargo test"\n'
            '[task.snapshot-test]\ncmd = "soldr cargo test && soldr cargo clippy -p a"\n'
        ))
        found = check_gate_016(self.tmp)
        self.assertEqual([(f.path, f.line) for f in found], [("bosn.toml", 3), ("bosn.toml", 5), ("bosn.toml", 7), ("bosn.toml", 15)])
        self.assertIn("task 'lint' runs lint in a bosn container (its name is lint-like)", found[0].message)
        self.assertIn("task 'rust-checks' runs lint in a bosn container (its cmd runs lint: `soldr cargo clippy", found[1].message)

    def test_unparseable_bosn_toml_is_ignored(self) -> None:
        self._write("bosn.toml", "[task.lint\n")
        self.assertEqual(check_gate_016(self.tmp), [])


class Gate016CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _lint(self, gate_toml: str) -> str:
        (self.tmp / "local-gate.toml").write_text(gate_toml, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(["local-gate", "lint", "--repo", str(self.tmp)])
        return out.getvalue()

    def test_local_gate_lint_reports_gate_016(self) -> None:
        out = self._lint(
            '[gate]\nmode = "shadow"\nrun = ["bosn", "ci", "run", "--wait"]\n'
            '[gate.lanes.dylint]\nrun = ["bosn", "ci", "run", "--wait", "--job", "dylint"]\ntools = ["bosn", "docker"]\n'
        )
        self.assertIn("[GATE-016]", out)
        self.assertIn("lint job 'dylint'", out)

    def test_local_gate_lint_host_lint_clean(self) -> None:
        out = self._lint(
            '[gate]\nmode = "shadow"\nrun = ["bosn", "ci", "run", "--wait"]\n'
            '[gate.lanes.lint]\nrun = ["soldr", "lint", "--check"]\ntools = ["soldr"]\n'
        )
        self.assertNotIn("[GATE-016]", out)


# The ghd / template-python-rust-cmd shape (2026-11-21): the `fast` job's
# steps run fmt/clippy/ruff/pylint, one of them behind an ACT-keyed `if:`;
# `precheck` calls a local reusable workflow whose job runs ruff.
CI_YML = """\
name: ci
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-24.04
    steps:
      - run: python3 -P -m ci_lint local-gate verify --repo . --trust
  precheck:
    needs: verify
    uses: ./.github/workflows/ci-pre.yml
  fast:
    needs: precheck
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
      - name: Format
        if: ${{ env.ACT != 'true' }}
        run: ./ci.py fmt
      - name: Clippy
        run: ./ci.py clippy
      - name: Ruff
        run: uv run --no-project --with ruff==0.15.18 ruff check src
      - name: Tests
        run: python3 ci/fast.py rust-test
  unit:
    needs: precheck
    runs-on: ubuntu-24.04
    steps:
      - run: pip install ruff pylint
      - run: python3 ci/fast.py python-test
"""
CI_PRE_YML = """\
name: ci-pre
on: {workflow_call: {}}
jobs:
  precheck:
    runs-on: ubuntu-24.04
    steps:
      - run: python3 -m ci_lint precheck --repo .
      - run: uvx ruff format --check .
"""


class LintCommandTest(unittest.TestCase):
    def test_step_lint_commands(self) -> None:
        for run in ("./ci.py fmt", "python ci.py clippy --all", "uv run ruff check .", "uvx ruff format --check",
                    "python3 -m pylint src", "pyright", "npx eslint .", "clang-tidy -p build a.cc",
                    "set -e\nsoldr lint --check", "cargo \\\n  clippy --all-targets"):
            self.assertIsNotNone(lint_command_in(run), run)
        for run in ("pip install ruff pylint", "uv run --with ruff==1 pytest", "./ci.py test", "python3 -m ci_lint precheck",
                    "python3 ci/fast.py rust-test", "# ruff check", "npm install eslint"):
            self.assertIsNone(lint_command_in(run), run)


@requires_yaml_tooling
class Gate016ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".github/workflows").mkdir(parents=True)
        (self.tmp / ".github/workflows/ci.yml").write_text(CI_YML, encoding="utf-8")
        (self.tmp / ".github/workflows/ci-pre.yml").write_text(CI_PRE_YML, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lane_job_replay_of_a_linting_job(self) -> None:
        """`--job fast` is not lint-named, but the job runs `./ci.py fmt`
        behind an ACT `if:` -- the guard does not exempt it."""

        lanes = (_lane("fast", (*BOSN_RUN, "--workflow", ".github/workflows/ci.yml", "--job", "fast"), ("bosn",)),
                 _lane("unit", (*BOSN_RUN, "--job", "unit"), ("bosn",)))
        found = check_gate_016(self.tmp, (), lanes)
        self.assertEqual([(f.path, f.line) for f in found], [(".github/workflows/ci.yml", 18)])
        self.assertIn("job 'fast' runs lint (`./ci.py fmt`)", found[0].message)
        self.assertIn("lane 'fast'", found[0].message)

    def test_replay_jobs_declared_in_gate_replay(self) -> None:
        """ghd/template today: the lane runs a wrapper script, the job is
        named only by `[[gate.replay.jobs]] source-job`."""

        lanes = (_lane("fast", ("uv", "run", "python", "ci/local_gate.py", "--lane", "fast"), ("bosn", "docker")),)
        found = check_gate_016(self.tmp, ("uv", "run", "python", "ci/local_gate.py"), lanes,
                               ("ci.yml:verify", "ci.yml:fast", ".github/workflows/ci.yml:fast"))
        self.assertEqual(len(found), 1)
        self.assertIn("[gate.replay] ci.yml:fast", found[0].message)

    def test_whole_workflow_replay_follows_reusable_workflows(self) -> None:
        """No `--job` (the gate's `run`, the `[gate.full-run]` command):
        every job is replayed, and `precheck`'s callee runs ruff."""

        found = check_gate_016(self.tmp, (*BOSN_RUN,))
        self.assertEqual(sorted((f.path, f.line) for f in found),
                         [(".github/workflows/ci-pre.yml", 8), (".github/workflows/ci.yml", 18)])
        self.assertTrue(all("by the gate" in f.message for f in found))

    def test_default_workflow_and_unknown_job(self) -> None:
        def run(*extra: str) -> list[str]:
            return [f.message for f in check_gate_016(self.tmp, (), (_lane("x", (*BOSN_RUN, *extra)),))]

        self.assertEqual(len(run("--job", "fast")), 1)
        self.assertEqual(run("--job", "nope"), [])
        self.assertEqual(run("--workflow", "gone.yml"), [])

    def test_unparseable_workflow_needs_review(self) -> None:
        (self.tmp / ".github/workflows/ci.yml").write_text("jobs: [unclosed\n", encoding="utf-8")
        found = check_gate_016(self.tmp, (*BOSN_RUN,))
        self.assertEqual([f.status for f in found], [Status.NEEDS_REVIEW])


if __name__ == "__main__":
    unittest.main()
