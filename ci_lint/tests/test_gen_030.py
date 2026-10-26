"""GEN-030 (#322): lane-log retention (ci_lint/lane_log_retention.py) and the
static no-broad-sweeps clause (ci_lint/rules/host_sweeps.py)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.lane_log_retention import RUN_DIR_RE, new_run_log_dir, prune_run_log_dirs
from ci_lint.rules.host_sweeps import check_gen_030, is_broad_sweep


class RetentionTest(unittest.TestCase):
    def test_keeps_newest_and_only_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            made = [new_run_log_dir(base, now=1_700_000_000 + i, pid=7) for i in range(25)]
            for d in made:
                (d / "lane.log").write_text("x" * 10, encoding="utf-8")
                self.assertRegex(d.name, RUN_DIR_RE)
            (base / "legacy.log").write_text("old", encoding="utf-8")
            (base / "foreign").mkdir()
            report = prune_run_log_dirs(base, keep=20, current=made[-1])
            self.assertEqual(len(report.removed), 5)
            self.assertEqual(report.freed_bytes, 50)
            self.assertEqual(set(report.removed), {d.name for d in made[:5]})
            for d in made[5:]:
                self.assertTrue(d.is_dir())
            self.assertTrue((base / "legacy.log").is_file())
            self.assertTrue((base / "foreign").is_dir())

    def test_never_removes_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = new_run_log_dir(base, now=1_000, pid=1)
            new_run_log_dir(base, now=2_000, pid=1)
            report = prune_run_log_dirs(base, keep=1, current=old)
            self.assertEqual(report.removed, ())
            self.assertTrue(old.is_dir())

    def test_missing_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(prune_run_log_dirs(Path(tmp) / "nope").removed, ())


class SweepTest(unittest.TestCase):
    def test_classifier(self) -> None:
        for line in (
            "docker system prune -af",
            "docker volume prune -f",
            "docker image prune -a",
            'subprocess.run(["docker", "system", "prune", "-f"])',
            "rm -rf ~/.cargo/registry",
            "rm -rf ../*/target",
            "rm -rf $HOME/.soldr",
            "rm -rf /tmp/bosn-*",
            "find /tmp -mtime +3 -delete",
            "auto_retention = false",
            "export SOLDR_AUTO_GC_DISABLED=1",
            'env = { SOLDR_NO_GC_TARGET = "true" }',
        ):
            self.assertIsNotNone(is_broad_sweep(line), line)
        for line in ("docker image prune", "rm -rf build", "rm -rf ./dist", "docker run --rm x", "find . -name x -delete",
                     "auto_retention = true", "SOLDR_AUTO_GC_DISABLED=0"):
            self.assertIsNone(is_broad_sweep(line), line)

    def test_scope_and_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "ci").mkdir()
            (repo / "ci" / "clean.sh").write_text(
                "#!/bin/sh\n# docker system prune in a comment\ndocker system prune -af\n"
                "docker volume prune -f  # ci-lint: allow GEN-030 recovery only\n",
                encoding="utf-8",
            )
            (repo / ".claude" / "skills" / "x").mkdir(parents=True)
            (repo / ".claude" / "skills" / "x" / "go.py").write_text('import os\nos.system("rm -rf ~/.cargo")\n', encoding="utf-8")
            (repo / ".github" / "workflows").mkdir(parents=True)
            (repo / ".github" / "workflows" / "ci.yml").write_text(
                "jobs:\n  a:\n    steps:\n      - run: docker system prune -af\n", encoding="utf-8"
            )
            (repo / "local-gate.toml").write_text('[gate.env]\nSOLDR_AUTO_GC_DISABLED = "1"\n', encoding="utf-8")
            findings = check_gen_030(repo)
            got = sorted((f.path, f.line) for f in findings)
            self.assertEqual(got, [(".claude/skills/x/go.py", 2), ("ci/clean.sh", 3), ("local-gate.toml", 2)])
            self.assertTrue(all(f.status == Status.VIOLATION and f.rule == "GEN-030" for f in findings))


if __name__ == "__main__":
    unittest.main()
