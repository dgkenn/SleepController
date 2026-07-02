"""Rotating SQLite backup (``sleepctl.storage.backup``): online-backup-API copy, rotation, and
the once-a-day file-mtime gate. Runs against a real on-disk DB (the backup API needs a real
file, not ``:memory:``)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from sleepctl.storage.backup import (
    default_backup_dir,
    list_backups,
    maybe_run_backup,
    run_backup,
)
from sleepctl.storage.repository import Repository


def _make_db(tmp_dir: str) -> str:
    path = os.path.join(tmp_dir, "sleepctl.db")
    repo = Repository(path)
    repo.log_event("device", "info", "prime", "prime applied")
    repo.close()
    return path


def test_run_backup_creates_a_consistent_readable_copy():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        dest = run_backup(db_path, keep=7)

        assert os.path.exists(dest)
        assert dest.endswith(".db")
        assert os.path.dirname(dest) == default_backup_dir(db_path)

        # the backup is a real, independently-openable SQLite file with the same data
        conn = sqlite3.connect(dest)
        try:
            row = conn.execute("SELECT code FROM events WHERE code='prime'").fetchone()
            assert row is not None
        finally:
            conn.close()


def test_run_backup_rejects_in_memory_db():
    try:
        run_backup(":memory:")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_backup_prunes_to_keep_most_recent():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        paths = []
        for i in range(10):
            p = run_backup(db_path, keep=3, now=base + timedelta(seconds=i))
            paths.append(p)

        backup_dir = default_backup_dir(db_path)
        remaining = list_backups(backup_dir)
        assert len(remaining) == 3
        # the survivors are the most recently written ones
        assert remaining == sorted(paths)[-3:]


def test_maybe_run_backup_is_gated_by_recency():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        now = datetime(2026, 6, 1, 3, 0, 0, tzinfo=timezone.utc)

        first = maybe_run_backup(db_path, interval_hours=24.0, now=now)
        assert first is not None

        # 1 hour later: too soon, no new backup
        soon = maybe_run_backup(db_path, interval_hours=24.0,
                                now=now + timedelta(hours=1))
        assert soon is None
        assert len(list_backups(default_backup_dir(db_path))) == 1

        # 25 hours later: due again
        later = maybe_run_backup(db_path, interval_hours=24.0,
                                 now=now + timedelta(hours=25))
        assert later is not None
        assert len(list_backups(default_backup_dir(db_path))) == 2
