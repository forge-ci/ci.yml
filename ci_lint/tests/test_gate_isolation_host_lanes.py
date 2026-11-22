"""GATE-009 shared replay exempts host soldr lanes (forge-ci/bosn#895).

A `[gate.lanes.lint] run = ["soldr", "lint", ...]` lane runs nothing under
bosn, so it needs no bosn receipt: adding it must not drop the gate out of
the shared-replay proof (which would demand a private run-record parser).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ci_lint.gate_isolation import _shared_replay
from ci_lint.local_gate import load_gate_config

RUN = '["bosn", "ci", "run", "--workspace", ".", "--workflow", ".github/workflows/ci.yml", "--wait", "--json"'
GATE = f"""\
[gate]
run = {RUN}]
mode = "enforce"

[gate.isolation]
marker = "BOSN_TEST_ISOLATED"
runner = ["bosn", "ci", "run"]
proves-tree = true
guard = "tests/conftest.py"

[gate.lanes.rust]
run = {RUN}, "--job", "rust"]
tools = ["bosn"]

[gate.replay]
repository = "forge-ci/bosn"
workflow = "ci.yml"
mode = "minimal"
report-source = "stdout"
qualified = true
provider-query = ["bosn", "ci", "runners", "list", "--json"]

[[gate.replay.jobs]]
source-job = "ci.yml:rust"
lanes = ["rust"]
"""
HOST_LINT = '\n[gate.lanes.lint]\nrun = ["soldr", "lint", "rust", "--check", "--cross-dylint"]\ntools = ["soldr"]\n'
SCRIPT_LANE = '\n[gate.lanes.other]\nrun = ["python3", "ci/gate.py"]\ntools = ["bosn"]\n'


class SharedReplayHostLaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _shared(self, toml: str) -> bool:
        (self.tmp / "local-gate.toml").write_text(toml, encoding="utf-8")
        loaded = load_gate_config(self.tmp)
        cfg = loaded.config
        assert cfg is not None and cfg.isolation is not None, [f.render() for f in loaded.findings]
        return _shared_replay(cfg.isolation, cfg.run, cfg.replay, cfg.lanes)

    def test_host_soldr_lane_keeps_shared_replay(self) -> None:
        self.assertTrue(self._shared(GATE))
        self.assertTrue(self._shared(GATE + HOST_LINT))

    def test_other_non_runner_lane_still_breaks_it(self) -> None:
        self.assertFalse(self._shared(GATE + SCRIPT_LANE))


if __name__ == "__main__":
    unittest.main()
