"""workspace_lock: per-subject mutex for concurrent pipeline safety.

Tests cover:
  - happy path (acquire, release, re-acquire)
  - stale lock reclamation (PID no longer alive)
  - contention with a live PID raises after timeout
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alchemist.workspace_lock import (
    WorkspaceLockError,
    _is_pid_alive,
    _read_pid,
    workspace_lock,
)


def test_acquire_and_release(tmp_path: Path) -> None:
    lock_file = tmp_path / ".alchemist" / "workspace.lock"
    with workspace_lock(tmp_path, timeout=2.0):
        # Inside the context, the lock file should exist with our PID.
        assert lock_file.exists()
        assert _read_pid(lock_file) == os.getpid()
    # Released on exit.
    assert not lock_file.exists()


def test_reacquire_after_release(tmp_path: Path) -> None:
    with workspace_lock(tmp_path, timeout=2.0):
        pass
    # Second acquisition should succeed (lock was cleanly released).
    with workspace_lock(tmp_path, timeout=2.0):
        pass


def test_stale_lock_reclaimed(tmp_path: Path) -> None:
    """A lock file left by a dead process should be reclaimed automatically."""
    lock_dir = tmp_path / ".alchemist"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "workspace.lock"
    # Write a PID that definitely doesn't exist (PIDs 2^31-2 ... -1 are unusual).
    lock_file.write_text("2147483646", encoding="utf-8")
    assert lock_file.exists()

    with workspace_lock(tmp_path, timeout=2.0):
        # We reclaimed the lock.
        assert _read_pid(lock_file) == os.getpid()


def test_contention_raises_after_timeout(tmp_path: Path) -> None:
    """A live-held lock must make a second attempt raise after the timeout."""
    lock_dir = tmp_path / ".alchemist"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "workspace.lock"
    # Write OUR OWN PID — _is_pid_alive will say True.
    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    with pytest.raises(WorkspaceLockError, match="locked by PID"):
        with workspace_lock(tmp_path, timeout=0.5, poll_interval=0.1):
            pass

    # Clean up so other tests aren't affected.
    lock_file.unlink(missing_ok=True)


def test_is_pid_alive_self() -> None:
    assert _is_pid_alive(os.getpid()) is True


def test_is_pid_alive_negative_and_zero() -> None:
    assert _is_pid_alive(0) is False
    assert _is_pid_alive(-1) is False
