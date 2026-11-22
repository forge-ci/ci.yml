"""soldr#4195 -- the inert-change exemption.

A head whose diff changes no path any declared lane can read needs no
`Local-Gate:` attestation: every lane's input set is unchanged, so the gate
would replay all of them. The tests below pin both halves -- that the
exemption fires for a docs-only change, and that it stays off for anything a
lane reads, for a deletion, for a rename, and for a repository that declares
no lanes at all.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ci_lint.local_gate import check_push, inert_change, load_gate_config, verify
from ci_lint.proc import run_captured

# Two lanes whose input sets (GATE-007) are decided by exclusion:
#   rust: reads everything except docs/** and *.md
#   docs: reads everything except src/**      (so nothing is universally safe)
LANES = """
[gate.lanes.rust]
run = ["true", "rust"]
exclude = ["docs/**", "*.md"]

[gate.lanes.docs]
run = ["true", "docs"]
exclude = ["src/**"]
"""

# Both lanes exclude docs/** and *.md: an edit under docs/ changes no input.
DOCS_ONLY_LANES = """
[gate.lanes.rust]
run = ["true", "rust"]
exclude = ["docs/**", "*.md"]

[gate.lanes.py]
run = ["true", "py"]
exclude = ["docs/**", "*.md", "src/**"]
"""


def _git(repo: Path, *args: str) -> str:
    proc = run_captured(["git", "-C", str(repo), *args])
    if not proc.ok:
        raise subprocess.CalledProcessError(proc.returncode, ["git", *args], proc.stdout, proc.stderr)
    return proc.stdout.strip()


def _repo(root: Path, lanes: str) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "local-gate.toml").write_text(
        '[gate]\nmode = "enforce"\nrun = ["true", "gate"]\nmirrors = ["ci.yml:lint"]\n'
        'verify = "ci.yml:verify"\n' + lanes,
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "design.md").write_text("# design\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


class InertChangeCase(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def config(self, repo: Path):  # type: ignore[no-untyped-def]
        loaded = load_gate_config(repo)
        self.assertEqual(loaded.findings, [])
        assert loaded.config is not None
        return loaded.config

    def base(self, repo: Path) -> str:
        return _git(repo, "rev-parse", "HEAD")

    def commit(self, repo: Path, message: str = "change") -> str:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", message)
        return _git(repo, "rev-parse", "HEAD")

    # ── the exemption fires ────────────────────────────────────────────────

    def test_docs_only_change_is_inert(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        base = self.base(repo)
        (repo / "docs" / "design.md").write_text("# design, revised\n", encoding="utf-8")
        head = self.commit(repo)
        reason = inert_change(repo, base, head, self.config(repo))
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("docs/design.md", reason)

    def test_new_docs_file_is_inert(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        base = self.base(repo)
        (repo / "docs" / "new.md").write_text("# new\n", encoding="utf-8")
        head = self.commit(repo)
        self.assertIsNotNone(inert_change(repo, base, head, self.config(repo)))

    # ── the exemption stays off ────────────────────────────────────────────

    def test_code_change_is_not_inert(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        base = self.base(repo)
        (repo / "src" / "main.rs").write_text("fn main() { }\n", encoding="utf-8")
        head = self.commit(repo)
        self.assertIsNone(inert_change(repo, base, head, self.config(repo)))

    def test_a_path_one_lane_reads_is_not_inert(self) -> None:
        """`docs/**` is safe only because *every* lane excludes it."""
        repo = _repo(self.root, LANES)  # the docs lane reads docs/**
        base = self.base(repo)
        (repo / "docs" / "design.md").write_text("# design, revised\n", encoding="utf-8")
        head = self.commit(repo)
        self.assertIsNone(inert_change(repo, base, head, self.config(repo)))

    def test_deletion_is_never_inert(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        base = self.base(repo)
        (repo / "docs" / "design.md").unlink()
        head = self.commit(repo)
        self.assertIsNone(inert_change(repo, base, head, self.config(repo)))

    def test_rename_is_never_inert(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        base = self.base(repo)
        _git(repo, "mv", "docs/design.md", "docs/design2.md")
        head = self.commit(repo)
        self.assertIsNone(inert_change(repo, base, head, self.config(repo)))

    def test_gate_declaration_is_never_inert(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        base = self.base(repo)
        (repo / "local-gate.toml").write_text(
            (repo / "local-gate.toml").read_text(encoding="utf-8") + "\n# note\n", encoding="utf-8"
        )
        head = self.commit(repo)
        self.assertIsNone(inert_change(repo, base, head, self.config(repo)))

    def test_no_lanes_means_no_exemption(self) -> None:
        repo = _repo(self.root, "")
        base = self.base(repo)
        (repo / "docs" / "design.md").write_text("# design, revised\n", encoding="utf-8")
        head = self.commit(repo)
        self.assertIsNone(inert_change(repo, base, head, self.config(repo)))

    def test_unreadable_diff_means_no_exemption(self) -> None:
        repo = _repo(self.root, DOCS_ONLY_LANES)
        self.assertIsNone(inert_change(repo, "0" * 40, "1" * 40, self.config(repo)))


class PushAndVerifyCase(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = _repo(self.root, DOCS_ONLY_LANES)
        self.base = _git(self.repo, "rev-parse", "HEAD")

    def config(self):  # type: ignore[no-untyped-def]
        loaded = load_gate_config(self.repo)
        assert loaded.config is not None
        return loaded.config

    def commit(self, path: str, body: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "change")
        return _git(self.repo, "rev-parse", "HEAD")

    def push_line(self, head: str) -> str:
        return f"refs/heads/topic {head} refs/heads/topic {self.base}\n"

    def test_check_push_admits_a_docs_only_head(self) -> None:
        head = self.commit("docs/design.md", "# revised\n")
        result = check_push(self.repo, self.push_line(head))
        self.assertEqual(result.problems, [])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.inert), 1)
        self.assertIn("docs/design.md", result.inert[0])

    def test_check_push_still_refuses_a_code_head(self) -> None:
        head = self.commit("src/main.rs", "fn main() { let _ = 1; }\n")
        result = check_push(self.repo, self.push_line(head))
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.inert, [])
        self.assertTrue(any("trailer" in problem for problem in result.problems))

    def test_verify_accepts_an_inert_head_but_never_trusts_it(self) -> None:
        head = self.commit("docs/design.md", "# revised\n")
        outcome = verify(self.repo, self.config(), sha=head, event="pull_request",
                         author="zackees", base=self.base)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.state, "inert")
        self.assertFalse(outcome.attested, "an inert head must not be trusted to skip remote jobs")

    def test_verify_fails_an_inert_head_when_no_base_is_known(self) -> None:
        head = self.commit("docs/design.md", "# revised\n")
        outcome = verify(self.repo, self.config(), sha=head, event="pull_request", author="zackees")
        self.assertEqual(outcome.exit_code, 1)
        self.assertFalse(outcome.attested)

    def test_verify_refuses_a_code_head_with_a_base(self) -> None:
        head = self.commit("src/main.rs", "fn main() { let _ = 1; }\n")
        outcome = verify(self.repo, self.config(), sha=head, event="pull_request",
                         author="zackees", base=self.base)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.state, "missing")

    def test_gate_would_replay_every_lane_for_an_inert_head(self) -> None:
        """The claim the exemption rests on, checked against the lane layer."""
        from ci_lint.lane_cache import lane_inputs, tree_entries

        head = self.commit("docs/design.md", "# revised\n")
        config = self.config()
        base_entries = tree_entries(self.repo, _git(self.repo, "rev-parse", f"{self.base}^{{tree}}"))
        head_entries = tree_entries(self.repo, _git(self.repo, "rev-parse", f"{head}^{{tree}}"))
        for lane in config.lanes:
            before = lane_inputs(base_entries, lane, config.run, config.source)
            after = lane_inputs(head_entries, lane, config.run, config.source)
            self.assertEqual([(e.path, e.sha) for e in before], [(e.path, e.sha) for e in after],
                             f"lane {lane.id} input set changed, so the exemption was wrong")


if __name__ == "__main__":
    unittest.main()


class NoLaneWorkCase(unittest.TestCase):
    """The stronger form: every lane already holds a fresh pass for the tree."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = _repo(self.root, LANES)

    def config(self):  # type: ignore[no-untyped-def]
        loaded = load_gate_config(self.repo)
        assert loaded.config is not None
        return loaded.config

    def keys(self):  # type: ignore[no-untyped-def]
        from ci_lint.lane_cache import ToolVersions, lane_key, tree_entries, tree_file
        from ci_lint.workflow_replay_runtime import query_execution_pins

        config = self.config()
        entries = tree_entries(self.repo, "HEAD")
        ci_toml = tree_file(self.repo, "HEAD", "ci.toml")
        pins = query_execution_pins(self.repo, config.replay)
        return config, [
            (lane, lane_key(entries, lane, gate_run=config.run, gate_source=config.source,
                            versions=ToolVersions(), provider=pins, ci_toml=ci_toml).key)
            for lane in config.lanes
        ]

    def test_no_pass_means_no_exemption(self) -> None:
        from ci_lint.local_gate import _no_lane_work

        self.assertIsNone(_no_lane_work(self.repo, "HEAD", self.config()))

    def test_one_missing_lane_means_no_exemption(self) -> None:
        from ci_lint.lane_cache import record
        from ci_lint.local_gate import _no_lane_work

        config, keys = self.keys()
        lane, key = keys[0]
        record(self.repo, lane, key, secs=1, head="HEAD", tree="")
        self.assertIsNone(_no_lane_work(self.repo, "HEAD", config))

    def test_all_lanes_hit_means_no_gate_needed(self) -> None:
        from ci_lint.lane_cache import record
        from ci_lint.local_gate import _no_lane_work

        config, keys = self.keys()
        for lane, key in keys:
            record(self.repo, lane, key, secs=1, head="HEAD", tree="")
        reason = _no_lane_work(self.repo, "HEAD", config)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("would execute nothing", reason)
