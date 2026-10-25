"""Real-process duplicate-run regression for the shared CI front door (#362),
re-scoped per worktree (zackees/ci.yml#397): one worktree still runs exactly
one gate at a time, but sibling worktrees of the same clone no longer queue
behind each other -- the bosn#617 multi-hour wait."""

import os
import subprocess
import sys
import time
from pathlib import Path

from ci_lint.gate_run_lock import hold
from ci_lint.lane_cache import run_lock_path
from ci_lint.local_gate import run_gate
from ci_lint.tests.test_local_gate import TempRepoCase, _git, _repo


class GateRunLockTest(TempRepoCase):
    def _concurrent(self, *, sibling: bool) -> None:
        signals = self.tmp / "signals"
        signals.mkdir()
        repo = self.tmp / "repo"
        repo.mkdir()
        _repo(repo, f'''from pathlib import Path
import time
signals = Path({str(signals)!r})
with (signals / "calls").open("a") as out:
    out.write("run\\n")
if not (signals / "ready").exists():
    (signals / "ready").touch()
    while not (signals / "release").exists():
        time.sleep(0.01)
''')
        target = repo
        if sibling:
            target = self.tmp / "sibling"
            _git(repo, "worktree", "add", "-q", "-b", "sibling", str(target), "HEAD")
        script = (
            "from pathlib import Path; from ci_lint.local_gate import load_gate_config, run_gate; "
            "import sys; repo=Path(sys.argv[1]); "
            "result=run_gate(repo,load_gate_config(repo).config,stamp=False); "
            "print(result.message); sys.exit(result.exit_code)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        with (signals / "child.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, "-c", script, str(repo)],
                                     env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while not (signals / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((signals / "ready").exists(), "first gate never started")
                second = run_gate(target, self.config(target), stamp=False)
            finally:
                (signals / "release").touch()
                child.wait(timeout=10)
        self.assertEqual(child.returncode, 0, (signals / "child.log").read_text())
        if sibling:
            # #397: worktrees of one clone gate CONCURRENTLY. What #362's
            # clone-wide lock really protected still holds without it:
            # atomic lane-cache records, per-worktree lane logs, and git's
            # own cross-worktree safety; the per-worktree lock keeps the
            # real conflict (two gates in ONE worktree).
            self.assertEqual(second.exit_code, 0, second.message)
            self.assertEqual((signals / "calls").read_text().splitlines(), ["run", "run"])
        else:
            self.assertEqual(second.exit_code, 2, second.message)
            self.assertIn("another local gate", second.message)
            self.assertIn("this worktree", second.message)
            self.assertEqual((signals / "calls").read_text().splitlines(), ["run"])
        # A persistent lock file must not become a stale lock after release.
        self.assertEqual(run_gate(target, self.config(target), stamp=False).exit_code, 0)

    def test_concurrent_same_worktree_runs_only_one_gate(self) -> None:
        self._concurrent(sibling=False)

    def test_sibling_worktrees_gate_concurrently(self) -> None:
        self._concurrent(sibling=True)

    def test_lock_path_is_per_worktree(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        _repo(repo, "pass\n")
        sibling = self.tmp / "sibling-lock"
        _git(repo, "worktree", "add", "-q", "-b", "sibling-lock", str(sibling), "HEAD")
        main_lock = run_lock_path(repo)
        sibling_lock = run_lock_path(sibling)
        self.assertNotEqual(main_lock, sibling_lock)
        self.assertEqual(main_lock.name, "run.lock")
        self.assertEqual(main_lock.parent.name, "ci-lint")
        self.assertNotIn(str(main_lock), str(sibling_lock))

    def test_process_death_releases_persistent_lock(self) -> None:
        lock = self.tmp / "run.lock"
        ready = self.tmp / "ready"
        script = (
            "from pathlib import Path; from ci_lint.gate_run_lock import hold; import sys,time; "
            "manager=hold(Path(sys.argv[1])); manager.__enter__(); "
            "Path(sys.argv[2]).touch(); time.sleep(60)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        with (self.tmp / "child.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, "-c", script, str(lock), str(ready)],
                                     env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "lock holder never started")
            finally:
                child.kill()
                child.wait(timeout=10)
        with hold(lock):
            self.assertTrue(lock.exists())
