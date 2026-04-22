"""Workspace-level mutual exclusion.

Two concurrent `alchemist translate` invocations on the same subject
directory race on:
  - `subjects/<name>/.alchemist/output/` (crate files rewritten per iter)
  - `subjects/<name>/.alchemist/wins/` (cache writes interleaved)
  - `target/` (cargo build state — already LNK1104-prone on Windows)

This module provides a per-workspace advisory lock. Acquired at pipeline
entry; blocks if another invocation is running; released on exit.

Cross-platform via msvcrt (Windows) / fcntl (POSIX). Stale locks are
detected via a PID-in-file check and reclaimed if the original process
is no longer alive.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkspaceLockError(RuntimeError):
    """Raised when a lock cannot be acquired or is held by another live process."""


def _read_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else None
    except (OSError, ValueError):
        return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import psutil  # type: ignore
        except ImportError:
            # Conservative: assume alive if we can't check.
            return True
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return True
    # POSIX: sending signal 0 tests whether the pid exists.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user.
        return True


@contextmanager
def workspace_lock(
    workspace_root: Path, *, timeout: float = 5.0, poll_interval: float = 0.5,
) -> Iterator[Path]:
    """Acquire an exclusive advisory lock on `workspace_root`.

    The lock file sits at `<workspace_root>/.alchemist/workspace.lock` and
    contains the PID of the holder. On contention, we poll up to `timeout`
    seconds. If the existing holder is dead, we reclaim the lock. If
    `timeout` elapses with a live holder still present, we raise
    WorkspaceLockError.

    Usage:
        with workspace_lock(Path("subjects/zlib")):
            run_pipeline(...)
    """
    lock_dir = workspace_root / ".alchemist"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "workspace.lock"
    deadline = time.monotonic() + timeout
    my_pid = os.getpid()
    while True:
        try:
            # O_CREAT | O_EXCL: atomic "create-or-fail"
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, str(my_pid).encode("utf-8"))
            finally:
                os.close(fd)
            break
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            # Lock exists — check if holder is alive.
            holder_pid = _read_pid(lock_path)
            if holder_pid is None or not _is_pid_alive(holder_pid):
                # Stale lock — reclaim.
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            # Live holder, wait.
            if time.monotonic() >= deadline:
                raise WorkspaceLockError(
                    f"workspace {workspace_root} is locked by PID {holder_pid} "
                    f"(waited {timeout}s). Kill that process or delete "
                    f"{lock_path} if it is stale."
                )
            time.sleep(poll_interval)
    try:
        yield lock_path
    finally:
        # Only remove the lock if we still own it (guard against process
        # that replaced ours mid-run).
        try:
            if lock_path.exists() and _read_pid(lock_path) == my_pid:
                lock_path.unlink()
        except OSError:
            pass
