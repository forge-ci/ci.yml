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
from ci_lint.gate_lint_host import check_gate_016, lint_like, runs_lint_command
from ci_lint.lane_cache import LaneConfig

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


if __name__ == "__main__":
    unittest.main()
