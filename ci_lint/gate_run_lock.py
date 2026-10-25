"""One local gate per worktree, held through execution and commit stamping.

Re-scoped from the clone in zackees/ci.yml#397: sibling worktrees gate
concurrently (their real shared state -- the lane-cache records -- is
written atomically, and the lane logs were always per-worktree), while one
worktree still runs exactly one gate at a time.
"""

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


class GateRunBusy(OSError):
    """Another process owns this worktree's gate execution lock."""


def _acquire(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def hold(path: Path) -> Iterator[None]:
    """Refuse contention immediately. Never unlink: contenders must lock the
    same inode. The kernel releases ownership on close or process death."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            _acquire(handle)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise GateRunBusy("another local gate is running for this worktree; retry after it finishes") from error
            raise
        try:
            yield
        finally:
            _release(handle)
