"""Rotating SQLite backups via the online backup API.

Uses ``sqlite3.Connection.backup()`` (NOT a raw file copy) so the snapshot is consistent even
while the source DB is open and being written -- including under WAL mode, where a plain
``cp`` of the main file can miss committed pages still sitting in ``-wal``. Writes land in
``.run/backups/`` next to the DB by default and are pruned to the most recent ``keep`` files.

Restore (services must be STOPPED first -- the API/daemons must not have the DB open while you
swap files underneath them):

    1. Stop the dashboard API + daemon (and watchdog, so it doesn't restart them mid-swap).
    2. Pick a backup: ``ls .run/backups/`` (files are named ``sleep-YYYYMMDD-HHMMSS.db``).
    3. Copy it over the live DB: ``cp .run/backups/sleep-<ts>.db <SLEEPCTL_DB path>``.
    4. Restart services.

A restored DB is a point-in-time snapshot: anything written after that backup was taken is
lost, but the DB itself is guaranteed internally consistent (the backup API never captures a
half-written transaction).
"""

from __future__ import annotations

import glob
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_FILE_PREFIX = "sleep-"
_FILE_SUFFIX = ".db"
_FILE_GLOB = f"{_FILE_PREFIX}*{_FILE_SUFFIX}"


def default_backup_dir(db_path: str) -> str:
    """The ``.run/backups`` directory next to ``db_path`` (same ``.run`` convention as
    ``app.bridge.run_dir`` / ``app.main._run_dir``, kept independent so this module has no
    dependency on the dashboard package)."""
    root = os.path.dirname(db_path) or "."
    return os.path.join(root, ".run", "backups")


def list_backups(backup_dir: str) -> list[str]:
    """Existing backup files, oldest first (lexicographic == chronological given the
    YYYYMMDD-HHMMSS naming)."""
    return sorted(glob.glob(os.path.join(backup_dir, _FILE_GLOB)))


def _ts_from_filename(path: str) -> Optional[datetime]:
    """Parse the UTC timestamp embedded in a ``sleep-YYYYMMDD-HHMMSS.db`` filename. Used
    (instead of the file's OS mtime) to gate ``maybe_run_backup`` -- deterministic from the
    filename alone, so it isn't at the mercy of filesystem mtime resolution/clock skew and
    behaves correctly when a caller passes an explicit ``now`` (e.g. in tests)."""
    stem = os.path.basename(path)
    if not (stem.startswith(_FILE_PREFIX) and stem.endswith(_FILE_SUFFIX)):
        return None
    try:
        return datetime.strptime(
            stem[len(_FILE_PREFIX):-len(_FILE_SUFFIX)], "%Y%m%d-%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _prune(backup_dir: str, keep: int) -> int:
    """Delete all but the most recent ``keep`` backups. Returns the count removed."""
    if keep <= 0:
        return 0
    files = list_backups(backup_dir)
    excess = files[:-keep] if len(files) > keep else []
    removed = 0
    for f in excess:
        try:
            os.remove(f)
            removed += 1
        except OSError:
            pass
    return removed


def run_backup(db_path: str, keep: int = 7, backup_dir: Optional[str] = None,
               now: Optional[datetime] = None) -> str:
    """Make one consistent backup of ``db_path`` and prune to the most recent ``keep``.

    Safe to call while the DB is open elsewhere (a live daemon/API connection): the online
    backup API copies committed pages through SQLite itself rather than touching the file
    bytes directly, so it never races a concurrent writer.

    Returns the path to the newly-written backup file.
    """
    if not db_path or db_path == ":memory:":
        raise ValueError("run_backup requires a real on-disk db_path (not ':memory:')")
    root = backup_dir or default_backup_dir(db_path)
    os.makedirs(root, exist_ok=True)
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(root, f"{_FILE_PREFIX}{ts}{_FILE_SUFFIX}")

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    _prune(root, keep)
    return dest


def maybe_run_backup(db_path: str, keep: int = 7, interval_hours: float = 24.0,
                     backup_dir: Optional[str] = None,
                     now: Optional[datetime] = None) -> Optional[str]:
    """Run a backup only if the newest existing one is missing or older than
    ``interval_hours`` -- a cheap, idempotent "once a day" gate that survives daemon restarts
    (it looks at the files on disk, not in-memory state). The gate compares against the
    timestamp embedded in the backup's filename (not its OS mtime), so it's deterministic
    given an explicit ``now``. Returns the new backup path, or ``None`` if a recent-enough
    backup already exists."""
    now = now or datetime.now(timezone.utc)
    root = backup_dir or default_backup_dir(db_path)
    existing = list_backups(root)
    if existing:
        latest_ts = _ts_from_filename(existing[-1])
        if latest_ts is not None and (now - latest_ts).total_seconds() < interval_hours * 3600.0:
            return None
    return run_backup(db_path, keep=keep, backup_dir=root, now=now)
