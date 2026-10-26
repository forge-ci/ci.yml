"""GEN-030 (#322): `local-gate` owns its lane logs and reclaims them.

Every `local-gate run` writes its lane logs (and any `full-run-*` scratch
dir) into its own run dir, `<git-dir>/ci-lint/lane-logs/run-<UTC stamp>-<pid>`.
After the run completes -- still inside the per-worktree run lock, so no
other gate run of this worktree is active -- `prune_run_log_dirs` keeps the
newest `KEEP_RUNS` run dirs and deletes the rest.

Ownership only: a directory is deleted only when its name matches the
`run-<stamp>-<pid>` shape this module creates. Anything else in the
lane-logs dir (legacy flat `<lane>.log` files, foreign files) is left
alone. The current run's dir is never deleted. Ordering is by the name's
UTC stamp, never by mtime.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

KEEP_RUNS = 20
RUN_DIR_RE = re.compile(r"^run-(\d{8}T\d{6}(?:\.\d{6})?Z)-(\d+)$")


@dataclass(frozen=True)
class RetentionReport:
    kept: tuple[str, ...]
    removed: tuple[str, ...]
    freed_bytes: int


def new_run_log_dir(base: Path, *, now: float | None = None, pid: int | None = None) -> Path:
    stamp_time = time.time() if now is None else now
    micros = int(round((stamp_time % 1) * 1_000_000)) % 1_000_000
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(stamp_time)) + f".{micros:06d}Z"
    path = base / f"run-{stamp}-{os.getpid() if pid is None else pid}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def prune_run_log_dirs(base: Path, *, keep: int = KEEP_RUNS, current: Path | None = None) -> RetentionReport:
    if not base.is_dir():
        return RetentionReport((), (), 0)
    owned = sorted(
        (p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and RUN_DIR_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    current_name = current.name if current is not None else None
    kept: list[str] = []
    removed: list[str] = []
    freed = 0
    for index, path in enumerate(owned):
        if index < keep or path.name == current_name:
            kept.append(path.name)
            continue
        size = _size(path)
        try:
            shutil.rmtree(path)
        except OSError:
            kept.append(path.name)
            continue
        removed.append(path.name)
        freed += size
    return RetentionReport(tuple(kept), tuple(removed), freed)
