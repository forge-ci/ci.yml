"""GATE-007 -- ci_lint/lane_cache.py and the laned `local-gate run` (zackees/ci.yml#177)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_lint.attestations import parse_trailers
from ci_lint.finding import Status
from ci_lint.full_run_receipt import load_receipt
from ci_lint.lane_cache import (
    LaneConfig,
    ToolVersion,
    ToolVersions,
    audit_trace,
    cache_dir,
    check_lanes_static,
    lane_key,
    lookup,
    record,
    run_audit,
    tree_entries,
    tree_file,
)
from ci_lint.local_gate import (
    Attestation,
    check_commit,
    load_gate_config,
    parse_attestation,
    run_gate,
    run_lanes,
)
from ci_lint.proc import run_captured

TREE = "a" * 40


def _git(repo: Path, *args: str) -> str:
    proc = run_captured(["git", "-C", str(repo), *args])
    if not proc.ok:
        raise subprocess.CalledProcessError(proc.returncode, ["git", *args], proc.stdout, proc.stderr)
    return proc.stdout.strip()


GATE_PY = """\
import pathlib, sys
lane = sys.argv[sys.argv.index("--lane") + 1]
log = pathlib.Path(sys.argv[sys.argv.index("--log") + 1])
with log.open("a") as fh:  # append: concurrent lanes must not lose a line
    fh.write(lane + "\\n")
import time
if pathlib.Path("sleep").exists():
    time.sleep(float(pathlib.Path("sleep").read_text()))
if pathlib.Path("fail-" + lane).exists():
    raise SystemExit(4)
if pathlib.Path("na-" + lane).exists():
    raise SystemExit(75)
"""


class LanedRepo(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.log = self.tmp / "lanes.log"
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        py = sys.executable
        (self.repo / "local-gate.toml").write_text(
            f'[gate]\nrun = ["{py}", "gate.py"]\n\n'
            f'[gate.lanes.lint]\nrun = ["{py}", "gate.py", "--lane", "lint", "--log", "{self.log}"]\n'
            'exclude = ["docs/**"]\ntools = ["git"]\n\n'
            f'[gate.lanes.tests]\nrun = ["{py}", "gate.py", "--lane", "tests", "--log", "{self.log}"]\n'
            'exclude = ["docs/**", "**/*.py"]\nkeep = ["src/**"]\ntools = ["git"]\n',
            encoding="utf-8",
        )
        self.write("gate.py", GATE_PY)
        self.write("src/lib.rs", "fn a() {}\n")
        self.write("src/helper.py", "X = 1\n")
        self.write("tools/guard.py", "Y = 1\n")
        self.write("docs/readme.md", "hi\n")
        self.write("Cargo.lock", "# lock\n")
        self.commit("init")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> None:
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(text, encoding="utf-8")

    def commit(self, msg: str) -> None:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", msg)

    def config(self):  # type: ignore[no-untyped-def]
        loaded = load_gate_config(self.repo)
        config, findings = loaded.config, loaded.findings
        self.assertEqual(findings, [])
        assert config is not None
        return config

    def runs(self) -> list[str]:
        return self.log.read_text().split() if self.log.exists() else []

    def gate(self, **kw: bool) -> str:
        self.log.unlink(missing_ok=True)
        outcome = run_gate(self.repo, self.config(), **kw)
        self.assertEqual(outcome.exit_code, 0, outcome.message)
        att = parse_attestation(_git(self.repo, "log", "-1", "--format=%B"))
        assert att is not None and att.lanes is not None
        return att.lanes


class OptionalLaneTest(LanedRepo):
    """A lane that cannot run on every host (zackees/ci.yml#202: a local VM)."""

    def _make_tests_optional(self) -> None:
        text = (self.repo / "local-gate.toml").read_text(encoding="utf-8")
        (self.repo / "local-gate.toml").write_text(
            text.replace('keep = ["src/**"]\ntools = ["git"]\n', 'keep = ["src/**"]\ntools = ["git"]\noptional = true\n'),
            encoding="utf-8")

    def test_optional_lane_not_applicable_passes_unattested_and_uncached(self) -> None:
        self._make_tests_optional()
        self.write("na-tests", "")
        self.commit("tests n/a here")
        self.assertEqual(self.gate(), "lint:run,tests:n/a")
        # Never cached: once the host can run it, it runs.
        _git(self.repo, "rm", "-q", "na-tests")
        self.commit("host gained the VM")
        self.assertEqual(self.gate(), "lint:run,tests:run")
        self.assertIn("tests", self.runs())

    def test_exit_75_from_a_required_lane_is_a_failure(self) -> None:
        self.write("na-tests", "")
        self.commit("tests n/a but required")
        self.log.unlink(missing_ok=True)
        self.assertEqual(run_gate(self.repo, self.config()).exit_code, 75)


class LaneRunTest(LanedRepo):
    def test_reusing_pass_preserves_execution_time_in_trailer(self) -> None:
        self.write("ci-attestations.yml", "version: 1\ngates:\n  general/all/static: {lane: lint}\njobs: {}\n")
        self.commit("define attestation")
        self.gate()
        original = parse_trailers(_git(self.repo, "log", "-1", "--format=%B"))[0].attestation
        assert original is not None
        self.write("docs/readme.md", "another docs change\n")
        self.commit("docs")
        with patch("ci_lint.attestations.time.time", return_value=time.time() + 600):
            self.gate()
        reused = parse_trailers(_git(self.repo, "log", "-1", "--format=%B"))[0].attestation
        assert reused is not None
        self.assertEqual(reused.via, "reused")
        self.assertEqual(reused.at, original.at)

    def test_stamped_head_revalidates_expired_passes(self) -> None:
        self.gate()
        with patch("ci_lint.lane_cache.time.time", return_value=time.time() + 25 * 3600):
            self.assertEqual(self.gate(), "lint:run,tests:run")
        self.assertEqual(self.runs(), ["lint", "tests"])

    def test_no_cache_reruns_stamped_head_without_force(self) -> None:
        self.gate()
        self.assertEqual(self.gate(use_cache=False), "lint:run,tests:run")
        self.assertEqual(self.runs(), ["lint", "tests"])

    def test_stamped_head_revalidates_tool_versions(self) -> None:
        self.gate()
        with patch.object(ToolVersions, "get", return_value=ToolVersion("git", "changed")):
            self.assertEqual(self.gate(), "lint:run,tests:run")
        self.assertEqual(self.runs(), ["lint", "tests"])

    def test_repeat_keeps_head_and_does_not_execute(self) -> None:
        self.gate()
        head = _git(self.repo, "rev-parse", "HEAD")
        self.gate()
        self.assertEqual(self.runs(), [])
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head)

    def test_first_run_runs_every_lane_and_records_provenance(self) -> None:
        self.assertEqual(self.gate(), "lint:run,tests:run")
        self.assertEqual(self.runs(), ["lint", "tests"])
        self.assertEqual(check_commit(self.repo, "HEAD").state, "attested")

    def test_docs_only_change_reuses_every_lane(self) -> None:
        self.gate()
        self.write("docs/readme.md", "changed\n")
        self.commit("docs")
        lanes = self.gate()
        self.assertEqual(self.runs(), [])
        self.assertRegex(lanes, r"^lint:reused@[0-9a-f]{12},tests:reused@[0-9a-f]{12}$")

    def test_python_guard_change_reruns_lint_only(self) -> None:
        self.gate()
        self.write("tools/guard.py", "Y = 2\n")
        self.commit("guard")
        self.assertRegex(self.gate(), r"^lint:run,tests:reused@")
        self.assertEqual(self.runs(), ["lint"])

    def test_keep_overrides_exclude(self) -> None:
        self.gate()
        self.write("src/helper.py", "X = 2\n")  # *.py is excluded from tests, src/** is kept
        self.commit("helper")
        self.assertEqual(self.gate(), "lint:run,tests:run")

    def test_mandatory_input_cannot_be_excluded(self) -> None:
        self.gate()
        self.write("Cargo.lock", "# lock 2\n")
        self.commit("lock")
        self.assertEqual(self.gate(), "lint:run,tests:run")

    def test_a_failed_lane_is_never_cached(self) -> None:
        self.write("fail-tests", "")
        self.commit("make tests fail")
        self.log.unlink(missing_ok=True)
        outcome = run_gate(self.repo, self.config())
        self.assertEqual(outcome.exit_code, 4)
        self.assertEqual(self.runs(), ["lint", "tests"])
        self.assertEqual(check_commit(self.repo, "HEAD").state, "missing")
        _git(self.repo, "rm", "-q", "fail-tests")
        self.commit("fix")
        self.assertRegex(self.gate(), r"^lint:run,tests:run$")  # fail-tests was a lint input too

    def test_no_cache_and_revert_to_a_passed_state(self) -> None:
        self.gate()
        first = _git(self.repo, "rev-parse", "HEAD^{tree}")
        self.write("src/lib.rs", "fn b() {}\n")
        self.commit("edit")
        self.gate()
        self.write("src/lib.rs", "fn a() {}\n")  # back to the first tree's content
        self.commit("revert")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD^{tree}"), first)
        self.assertRegex(self.gate(force=True), r"^lint:reused@.*,tests:reused@")
        self.assertEqual(self.gate(force=True, use_cache=False), "lint:run,tests:run")

    def test_cache_lives_in_the_shared_git_dir(self) -> None:
        self.gate()
        self.assertTrue(str(cache_dir(self.repo)).endswith(".git/ci-lint/lane-cache"))
        self.assertEqual(len(list((cache_dir(self.repo) / "lint").glob("*.json"))), 1)


class ParallelTest(LanedRepo):
    """Light lanes run alongside the heavy chain (#177, soldr follow-up)."""

    def _three_lanes(self, *, light: bool) -> None:
        py = sys.executable
        weight = 'weight = "light"\n' if light else ""
        lanes = "".join(
            f'[gate.lanes.{lane}]\nrun = ["{py}", "gate.py", "--lane", "{lane}", "--log", "{self.log}"]\n'
            f'tools = ["git"]\n{weight if lane != "heavy" else ""}\n'
            for lane in ("a", "b", "heavy")
        )
        (self.repo / "local-gate.toml").write_text(f'[gate]\nrun = ["{py}", "gate.py"]\n\n{lanes}', encoding="utf-8")
        self.write("sleep", "1.5")
        self.commit("three lanes")

    def test_light_lanes_overlap_the_heavy_chain(self) -> None:
        self._three_lanes(light=True)
        start = time.monotonic()
        self.assertEqual(self.gate(), "a:run,b:run,heavy:run")  # provenance keeps declared order
        self.assertLess(time.monotonic() - start, 3.5)  # sequential would be >= 4.5 s

    def test_heavy_lanes_stay_sequential(self) -> None:
        self._three_lanes(light=False)
        start = time.monotonic()
        self.gate()
        self.assertGreaterEqual(time.monotonic() - start, 4.4)

    def test_passing_lanes_are_cached_even_when_another_lane_fails(self) -> None:
        self._three_lanes(light=True)
        self.write("fail-b", "")
        self.write("sleep", "0")
        self.commit("b fails")
        self.log.unlink(missing_ok=True)
        self.assertEqual(run_gate(self.repo, self.config()).exit_code, 4)
        # Same tree, no cache bypass: a and heavy were recorded, so only b reruns.
        self.log.unlink(missing_ok=True)
        run_gate(self.repo, self.config())
        self.assertEqual(self.runs(), ["b"])

    def test_bad_weight(self) -> None:
        text = (self.repo / "local-gate.toml").read_text(encoding="utf-8").replace(
            'tools = ["git"]\n\n[gate.lanes.tests]', 'tools = ["git"]\nweight = "medium"\n\n[gate.lanes.tests]'
        )
        (self.repo / "local-gate.toml").write_text(text, encoding="utf-8")
        self.assertEqual([f.rule for f in load_gate_config(self.repo).findings], ["GATE-007"])


class HeavyGroupTest(LanedRepo):
    """Heavy lanes in independent groups run as concurrent chains (#399)."""

    def _lanes(self, groups: dict[str, str | None]) -> None:
        py = sys.executable
        lanes = "".join(
            f'[gate.lanes.{lane}]\nrun = ["{py}", "gate.py", "--lane", "{lane}", "--log", "{self.log}"]\n'
            f'tools = ["git"]\n' + (f'group = "{group}"\n' if group else "") + "\n"
            for lane, group in groups.items()
        )
        (self.repo / "local-gate.toml").write_text(f'[gate]\nrun = ["{py}", "gate.py"]\n\n{lanes}', encoding="utf-8")
        self.write("sleep", "1.5")
        self.commit("grouped lanes")

    def test_independent_groups_overlap(self) -> None:
        self._lanes({"rust": None, "tests": "isolated"})
        start = time.monotonic()
        self.assertEqual(self.gate(), "rust:run,tests:run")
        self.assertLess(time.monotonic() - start, 2.8)  # one chain would be >= 3.0 s

    def test_lanes_in_one_group_stay_sequential(self) -> None:
        self._lanes({"rust": None, "cross": None, "tests": "isolated"})
        start = time.monotonic()
        self.assertEqual(self.gate(), "rust:run,cross:run,tests:run")
        self.assertGreaterEqual(time.monotonic() - start, 2.9)  # rust then cross

    def test_a_failure_stops_only_its_own_group(self) -> None:
        self._lanes({"rust": None, "cross": None, "tests": "isolated"})
        self.write("fail-rust", "")
        self.write("sleep", "0")
        self.commit("rust fails")
        self.log.unlink(missing_ok=True)
        self.assertEqual(run_gate(self.repo, self.config()).exit_code, 4)
        self.assertEqual(sorted(self.runs()), ["rust", "tests"])  # cross never started
        # tests passed in its own group: same tree, so only rust reruns.
        self.log.unlink(missing_ok=True)
        run_gate(self.repo, self.config())
        self.assertEqual(self.runs(), ["rust"])

    def test_group_on_a_light_lane_is_a_finding(self) -> None:
        text = (self.repo / "local-gate.toml").read_text(encoding="utf-8").replace(
            'tools = ["git"]\n\n[gate.lanes.tests]',
            'tools = ["git"]\nweight = "light"\ngroup = "x"\n\n[gate.lanes.tests]',
        )
        (self.repo / "local-gate.toml").write_text(text, encoding="utf-8")
        self.assertEqual([f.rule for f in load_gate_config(self.repo).findings], ["GATE-007"])

    def test_empty_group_is_a_finding(self) -> None:
        text = (self.repo / "local-gate.toml").read_text(encoding="utf-8").replace(
            'tools = ["git"]\n\n[gate.lanes.tests]', 'tools = ["git"]\ngroup = ""\n\n[gate.lanes.tests]'
        )
        (self.repo / "local-gate.toml").write_text(text, encoding="utf-8")
        self.assertEqual([f.rule for f in load_gate_config(self.repo).findings], ["GATE-007"])


class FullRunReceiptTest(LanedRepo):
    """A full run can seed distinct lane passes only with an exact receipt."""

    def setUp(self) -> None:
        super().setUp()
        py = sys.executable
        text = (self.repo / "local-gate.toml").read_text(encoding="utf-8")
        text = text.replace(
            f'run = ["{py}", "gate.py"]\n\n',
            f'run = ["{py}", "full.py", "--full", "--log", "{self.log}"]\n\n'
            '[gate.full-run]\nreceipt = "lane-passes-v1"\nmin-misses = 2\n\n',
        )
        (self.repo / "local-gate.toml").write_text(text, encoding="utf-8")
        self.write("full.py", """\
import json, os, pathlib, sys
log = pathlib.Path(sys.argv[sys.argv.index("--log") + 1])
log.write_text(log.read_text() + "full\\n" if log.exists() else "full\\n")
if not pathlib.Path("no-receipt").exists():
    lanes = ["lint"] if pathlib.Path("missing-tests").exists() else ["lint", "tests"]
    receipt = {"version": 1, "tree": os.environ["CI_LINT_GATE_TREE"],
               "passes": [{"lane": lane, "secs": 2} for lane in lanes]}
    pathlib.Path(os.environ["CI_LINT_GATE_RECEIPT"]).write_text(json.dumps(receipt))
""")
        self.commit("full-run receipt gate")

    def test_cold_full_run_seeds_each_lane_and_warm_hit_reuses(self) -> None:
        self.assertEqual(self.gate(), "lint:run,tests:run")
        self.assertEqual(self.runs(), ["full"])
        self.assertEqual(len(list((cache_dir(self.repo) / "lint").glob("*.json"))), 1)
        self.assertEqual(len(list((cache_dir(self.repo) / "tests").glob("*.json"))), 1)
        self.write("docs/readme.md", "new docs\n")
        self.commit("docs")
        self.assertRegex(self.gate(), r"^lint:reused@.*,tests:reused@")
        self.assertEqual(self.runs(), [])

    def test_optional_unavailable_full_run_never_records_a_pass(self) -> None:
        py = sys.executable
        path = self.repo / "local-gate.toml"
        path.write_text(path.read_text() +
                        f'\n[gate.lanes.winvm]\nrun = ["{py}", "gate.py", "--lane", "winvm", "--log", "{self.log}"]\n'
                        'tools = ["git"]\noptional = true\n')
        script = self.repo / "full.py"
        script.write_text(script.read_text().replace(
            'pathlib.Path(os.environ["CI_LINT_GATE_RECEIPT"]).write_text(json.dumps(receipt))',
            'receipt["not-applicable"] = ["winvm"]\n'
            '    pathlib.Path(os.environ["CI_LINT_GATE_RECEIPT"]).write_text(json.dumps(receipt))'))
        self.commit("optional host lane")
        cfg = self.config()
        result = run_lanes(self.repo, cfg, _git(self.repo, "rev-parse", "HEAD"),
                           _git(self.repo, "rev-parse", "HEAD^{tree}"), use_cache=False)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.provenance, ["lint:run", "tests:run", "winvm:n/a"])
        self.assertEqual(tuple(p.lane for p in result.passed), ("lint", "tests"))
        self.assertEqual(list((cache_dir(self.repo) / "winvm").glob("*.json")), [])

    def test_one_miss_runs_only_that_lane(self) -> None:
        self.gate()
        self.write("tools/guard.py", "Y = 2\n")
        self.commit("guard")
        self.assertRegex(self.gate(), r"^lint:run,tests:reused@")
        self.assertEqual(self.runs(), ["lint"])

    def test_missing_receipt_fails_closed(self) -> None:
        self.write("no-receipt", "")
        self.commit("no receipt")
        outcome = run_gate(self.repo, self.config())
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertEqual(self.runs(), ["full"])
        self.assertEqual(len(list((cache_dir(self.repo) / "lint").glob("*.json"))), 0)

    def test_partial_receipt_fails_closed(self) -> None:
        self.write("missing-tests", "")
        self.commit("partial receipt")
        outcome = run_gate(self.repo, self.config())
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertEqual(self.runs(), ["full"])
        self.assertEqual(len(list((cache_dir(self.repo) / "lint").glob("*.json"))), 0)

    def test_receipt_rejects_duplicate_lane_wrong_tree_and_symlink(self) -> None:
        path = self.tmp / "receipt.json"
        expected = ("lint", "tests")
        path.write_text(json.dumps({"version": 1, "tree": TREE,
                                    "passes": [{"lane": "lint", "secs": 1}, {"lane": "lint", "secs": 2}]}))
        self.assertIsNone(load_receipt(path, tree=TREE, expected_lanes=expected).evidence)
        path.write_text(json.dumps({"version": 1, "tree": "b" * 40,
                                    "passes": [{"lane": "lint", "secs": 1}, {"lane": "tests", "secs": 2}]}))
        self.assertIsNone(load_receipt(path, tree=TREE, expected_lanes=expected).evidence)
        link = self.tmp / "link.json"
        link.symlink_to(path)
        self.assertIsNone(load_receipt(link, tree=TREE, expected_lanes=expected).evidence)


class KeyTest(LanedRepo):
    def test_key_depends_on_tool_version_env_and_argv(self) -> None:
        cfg = self.config()
        lane = cfg.lanes[0]
        entries = tree_entries(self.repo, "HEAD")
        base = dict(gate_run=cfg.run, gate_source=cfg.source)
        v1 = ToolVersions([ToolVersion("git", "git 1")])
        v2 = ToolVersions([ToolVersion("git", "git 2")])
        k = lane_key(entries, lane, versions=v1, **base).key
        self.assertNotEqual(k, lane_key(entries, lane, versions=v2, **base).key)
        with_env = LaneConfig(lane.id, lane.run, lane.exclude, lane.keep, lane.tools, ("FOO",))
        self.assertNotEqual(
            lane_key(entries, with_env, versions=v1, environ={"FOO": "1"}, **base).key,
            lane_key(entries, with_env, versions=v1, environ={"FOO": "2"}, **base).key,
        )
        moved = LaneConfig(lane.id, (*lane.run, "--extra"), lane.exclude, lane.keep, lane.tools)
        self.assertNotEqual(k, lane_key(entries, moved, versions=v1, **base).key)

    def test_pins_follow_the_lane_tools(self) -> None:
        # Design revision 3 (#177): a Python-only lane is not invalidated by
        # Cargo.lock; a Rust lane and an unrecognized-tool lane are.
        cfg = self.config()
        base = dict(gate_run=cfg.run, gate_source=cfg.source, versions=ToolVersions([ToolVersion("uv", "1"),
                    ToolVersion("soldr", "1"), ToolVersion("git", "1")]))
        before = tree_entries(self.repo, "HEAD")
        self.write("Cargo.lock", "# lock 2\n")
        self.commit("lock")
        after = tree_entries(self.repo, "HEAD")
        def lane(tools: tuple[str, ...]) -> LaneConfig:
            return LaneConfig("x", cfg.run, exclude=("Cargo.lock",), tools=tools)
        py = lane(("uv",))
        self.assertEqual(lane_key(before, py, **base).key, lane_key(after, py, **base).key)
        for tools in (("soldr",), ("git",)):
            rs = lane(tools)
            self.assertNotEqual(lane_key(before, rs, **base).key, lane_key(after, rs, **base).key)

    def test_cpp_pins_are_lane_inputs_for_cpp_tools(self) -> None:
        # zackees/ci.yml#393: a CMake lane cannot exclude its configure inputs
        # (presets, CMakeLists.txt, cmake/**, *.cmake, dependency manifests,
        # submodules, format/tidy configs); a Python-only lane is not
        # invalidated by them.
        cfg = self.config()
        tools = ("uv", "cmake", "cc", "c++")
        base = dict(gate_run=cfg.run, gate_source=cfg.source,
                    versions=ToolVersions([ToolVersion(t, "1") for t in tools]))
        pins = {
            "CMakePresets.json": "{}\n", "CMakeLists.txt": "project(x)\n", "cmake/common/helpers.cmake": "# a\n",
            "src/toolchain/arm.cmake": "# t\n", "deps/buildspec.json": "{}\n", "vcpkg.json": "{}\n",
            "conanfile.txt": "[requires]\n", ".gitmodules": "", ".clang-format": "BasedOnStyle: LLVM\n",
            ".clang-tidy": "Checks: '-*'\n",
        }
        for rel, text in pins.items():
            self.write(rel, text)
        self.commit("pins")
        exclude = (*pins, "cmake/**", "**/*.cmake", "deps/**")

        def lane(lane_tools: tuple[str, ...]) -> LaneConfig:
            return LaneConfig("x", cfg.run, exclude=exclude, tools=lane_tools)

        for rel in pins:
            before = tree_entries(self.repo, "HEAD")
            self.write(rel, (self.repo / rel).read_text(encoding="utf-8") + "# changed\n")
            self.commit(f"edit {rel}")
            after = tree_entries(self.repo, "HEAD")
            cpp = lane(("cmake",))
            self.assertNotEqual(lane_key(before, cpp, **base).key, lane_key(after, cpp, **base).key, rel)
            py = lane(("uv",))
            self.assertEqual(lane_key(before, py, **base).key, lane_key(after, py, **base).key, rel)

    def test_cpp_lane_key_covers_the_compiler_identity(self) -> None:
        # #393: a CMake lane resolves `cc`/`c++` without declaring them; a
        # compiler upgrade must invalidate it. Non-C/C++ lanes do not probe it.
        cfg = self.config()
        entries = tree_entries(self.repo, "HEAD")
        base = dict(gate_run=cfg.run, gate_source=cfg.source)

        def versions(cc: str) -> ToolVersions:
            return ToolVersions([ToolVersion("cmake", "cmake 3.31"), ToolVersion("ninja", "1.12"),
                                 ToolVersion("cc", cc), ToolVersion("c++", cc), ToolVersion("uv", "1")])

        cpp = LaneConfig("x", cfg.run, tools=("cmake", "ninja"))
        k14, k15 = (lane_key(entries, cpp, versions=versions(v), **base) for v in ("gcc 14", "gcc 15"))
        self.assertNotEqual(k14.key, k15.key)
        self.assertEqual([t.tool for t in k14.tools], ["c++", "cc", "cmake", "ninja"])
        py = LaneConfig("x", cfg.run, tools=("uv",))
        self.assertEqual(lane_key(entries, py, versions=versions("gcc 14"), **base).key,
                         lane_key(entries, py, versions=versions("gcc 15"), **base).key)
        fmt = LaneConfig("x", cfg.run, tools=("clang-format",))
        self.assertEqual(["clang-format"], [t.tool for t in lane_key(entries, fmt, versions=versions("gcc 14"), **base).tools])

    def test_expired_entry_is_ignored(self) -> None:
        lane = LaneConfig("lint", ("x",), max_age_hours=1.0)
        record(self.repo, lane, "k" * 64, secs=3, head="h", tree="t")
        self.assertIsNotNone(lookup(self.repo, lane, "k" * 64))
        self.assertIsNone(lookup(self.repo, lane, "k" * 64, now=time.time() + 2 * 3600))

    def test_trailer_round_trip(self) -> None:
        att = Attestation(tree=TREE, secs=5, lanes="lint:run,tests:reused@abcdef012345")
        self.assertEqual(parse_attestation(f"s\n\n{att.trailer()}\n"), att)
        legacy = parse_attestation(f"s\n\nLocal-Gate: v1 tree={TREE} secs=5\n")
        assert legacy is not None and legacy.lanes is None


class LinterPinKeyTest(LanedRepo):
    """zackees/ci.yml#401: a pin-only bump of ci.toml's top-level `linter`
    must not invalidate lanes that never run ci-lint -- soldr#3556 measured
    ~24 minutes of heavy-lane reruns per pin bump for a pin that only the
    guards lane consumes. A lane whose argv/tools invoke ci-lint keeps the
    full pin in its key, any OTHER ci.toml field change still moves every
    lane, and KEY_VERSION bumped to gate-007/v2 across the semantics
    change."""

    PIN_A = f'linter = "zackees/ci.yml@{"a" * 40}"\n\n[cache]\nbudget = "9GB"\n'
    PIN_B = f'linter = "zackees/ci.yml@{"b" * 40}"\n\n[cache]\nbudget = "9GB"\n'
    BUDGET_CHANGE = f'linter = "zackees/ci.yml@{"a" * 40}"\n\n[cache]\nbudget = "8GB"\n'

    def _keys_for(self, toml_text: str, lane: LaneConfig) -> str:
        path = self.repo / "ci.toml"
        if not path.exists() or path.read_text(encoding="utf-8") != toml_text:
            self.write("ci.toml", toml_text)
            self.commit(f"ci.toml {toml_text.splitlines()[0][:44]}")
        cfg = self.config()
        return lane_key(
            tree_entries(self.repo, "HEAD"), lane,
            gate_run=cfg.run, gate_source=cfg.source,
            versions=ToolVersions([ToolVersion("git", "1")]),
            ci_toml=tree_file(self.repo, "HEAD", "ci.toml"),
        ).key

    def test_pin_only_bump_moves_only_ci_lint_lanes(self) -> None:
        plain = LaneConfig("plain", ("python", "gate.py", "--lane", "lint"), tools=("git",))
        cilint = LaneConfig(
            "guards",
            ("uvx", "--from", f"git+https://github.com/zackees/ci.yml@{'c' * 40}", "ci-lint", "selftest"),
            tools=("git",),
        )

        plain_a = self._keys_for(self.PIN_A, plain)
        cilint_a = self._keys_for(self.PIN_A, cilint)
        plain_b = self._keys_for(self.PIN_B, plain)
        cilint_b = self._keys_for(self.PIN_B, cilint)

        # GREEN (acceptance): the pin-only bump leaves a non-ci-lint lane's
        # key equal, and still changes a ci-lint lane's.
        self.assertEqual(plain_a, plain_b, "a pin-only bump must not invalidate a non-ci-lint lane (#401)")
        self.assertNotEqual(cilint_a, cilint_b, "a lane that runs ci-lint keeps the full pin")

        # Any OTHER ci.toml field change still moves both lanes.
        self.assertNotEqual(self._keys_for(self.BUDGET_CHANGE, plain), plain_a, "budget change moves every lane")
        self.assertNotEqual(self._keys_for(self.BUDGET_CHANGE, cilint), cilint_a, "budget change moves every lane")

    def test_semantic_hash_ignores_comments_but_not_values(self) -> None:
        # The semantic contribution is parse-based: a comment-only edit of
        # ci.toml also leaves a non-ci-lint lane's key equal (the residual
        # of the decided design -- lanes do not read comments), while a
        # value edit moves it.
        plain = LaneConfig("plain", ("python", "gate.py"), tools=("git",))
        with_comment = self.PIN_A + "# a comment\n"
        self.assertEqual(self._keys_for(self.PIN_A, plain), self._keys_for(with_comment, plain))

    def test_no_ci_toml_bytes_keeps_byte_sensitivity(self) -> None:
        # A caller that passes no ci_toml (or a lane in a tree without
        # ci.toml) hashes ci.toml by blob sha, exactly as before #401.
        plain = LaneConfig("plain", ("python", "gate.py"), tools=("git",))
        cfg = self.config()

        def key_without_bytes(toml_text: str) -> str:
            self.write("ci.toml", toml_text)
            self.commit("pin")
            return lane_key(
                tree_entries(self.repo, "HEAD"), plain,
                gate_run=cfg.run, gate_source=cfg.source,
                versions=ToolVersions([ToolVersion("git", "1")]),
            ).key

        self.assertNotEqual(key_without_bytes(self.PIN_A), key_without_bytes(self.PIN_B))


class AuditTest(LanedRepo):
    def test_trace_reads_of_excluded_files_are_reported(self) -> None:
        cfg = self.config()
        lane = cfg.lanes[1]  # tests: excludes docs/** and **/*.py, keeps src/**
        root = str(self.repo.resolve())
        trace = "\n".join(
            [
                f'123 openat(AT_FDCWD, "{root}/docs/readme.md", O_RDONLY) = 3',
                f'123 openat(AT_FDCWD, "{root}/src/helper.py", O_RDONLY) = 3',
                f'123 openat(AT_FDCWD, "{root}/tools/guard.py", O_RDONLY) = -1 ENOENT (No such file)',
                f'123 openat(AT_FDCWD, "{root}/Cargo.lock", O_RDONLY) = 3',
                f'123 openat(AT_FDCWD, "{root}/gate.py", O_RDONLY) = 3',
            ]
        )
        audit = audit_trace(self.repo, lane, cfg.run, cfg.source, trace)
        self.assertEqual(audit.excluded_reads, ("docs/readme.md",))
        self.assertEqual(audit.opened, 4)

    @unittest.skipUnless(shutil.which("strace"), "strace not on PATH")
    def test_live_audit_of_a_clean_lane(self) -> None:
        cfg = self.config()
        audit = run_audit(self.repo, cfg.lanes[0], cfg.run, cfg.source)
        assert audit is not None
        self.assertEqual(audit.excluded_reads, ())
        self.assertGreater(audit.opened, 0)


class StaticTest(LanedRepo):
    def test_green(self) -> None:
        cfg = self.config()
        self.assertEqual(check_lanes_static(cfg.lanes, cfg.run, cfg.source, self.repo), [])

    def test_exclusions_that_drop_mandatory_inputs(self) -> None:
        cfg = self.config()
        bad = (LaneConfig("x", (*cfg.run, "--lane", "x"), exclude=("**/*.lock", "gate.py"), tools=("git",)),)
        findings = check_lanes_static(bad, cfg.run, cfg.source, self.repo)
        # Only the exclusion that names a mandatory input is reported; the broad
        # `**/*.lock` is harmless because mandatory inputs are re-included.
        self.assertEqual([f.rule for f in findings], ["GATE-007"])
        self.assertIn("gate.py", findings[0].message)
        self.assertNotIn("Cargo.lock", findings[0].message)

    def test_lane_must_slice_the_gate_command_and_declare_tools(self) -> None:
        cfg = self.config()
        lanes = (LaneConfig("x", ("bash", "other.sh")),)
        statuses = sorted(f.status.value for f in check_lanes_static(lanes, cfg.run, cfg.source, self.repo))
        self.assertEqual(statuses, [Status.NEEDS_REVIEW.value, Status.VIOLATION.value])

    def test_unknown_lane_key_is_ct_001(self) -> None:
        text = (self.repo / "local-gate.toml").read_text(encoding="utf-8") + "bogus = 1\n"
        (self.repo / "local-gate.toml").write_text(text, encoding="utf-8")
        findings = load_gate_config(self.repo).findings
        self.assertEqual([f.rule for f in findings], ["CT-001"])


if __name__ == "__main__":
    unittest.main()
