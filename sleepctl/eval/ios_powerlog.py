"""Extract screen-on events from an iOS PowerLog as objective wake anchors.

WHY THIS AND NOT AN AUTOMATION
------------------------------
The natural idea is to have the phone report screen-ons live. iOS forbids it: personal
automations do not run while the device is locked (Apple, "Setting triggers in Shortcuts"), and
not unlocking is precisely the behaviour being measured -- the user presses the side button to
read the lock-screen clock and puts the phone down. Every live route (NFC tag, Back Tap, a
shortcut posting to this system) requires an unlock, which would change the very thing under
observation and add an arousal to it.

What remains is RETROSPECTIVE, and that turns out to be better suited anyway. iOS keeps roughly
seven days of PowerLog history, so a single capture yields anchors for nights ALREADY RECORDED,
with no behaviour change and no observer effect. That is a genuinely strong instrument: the
comparison is against nights the user slept normally, unaware of any marking.

Capture (no jailbreak, no Mac needed for the capture itself):
    hold VOLUME UP + VOLUME DOWN + SIDE for ~1.5 s, release, wait ~10 min, then
    Settings > Privacy & Security > Analytics & Improvements > Analytics Data >
    the newest `sysdiagnose_...` entry > share it off the phone.
The database is at logs/powerlogs/CurrentPowerlog.PLSQL inside the archive.

SCHEMA DISCOVERY RATHER THAN A HARDCODED TABLE
----------------------------------------------
The PowerLog holds ~700 tables and the set varies by device and iOS version, so a hardcoded
table name is a guess that breaks silently on the next release. This scans for tables whose
names indicate display/backlight state and whose columns include a timestamp, reports what it
found, and lets the caller see the evidence. Discovering beats assuming when the schema is not
contractual.

Timestamps are stored in MONOTONIC time, with the wall-clock offset in a separate table
(PLSTORAGEOPERATOR_EVENTFORWARD_TIMEOFFSET). Reading the raw column as a Unix timestamp -- the
obvious mistake -- yields times that are wrong by however long the device has been up.

Read-only. Never modifies the database it is given.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

#: Tables whose names suggest display/backlight state.
_DISPLAY_TABLE_RE = re.compile(r"(display|backlight|screen)", re.I)

#: Columns that plausibly carry the event time.
_TIME_COL_RE = re.compile(r"(timestamp|^time$|_time$|date)", re.I)

#: Columns that plausibly carry the on/off state.
_STATE_COL_RE = re.compile(r"(state|status|ison|on_off|brightness|level)", re.I)

#: Where the monotonic-to-wall-clock offset lives.
_OFFSET_TABLE = "PLSTORAGEOPERATOR_EVENTFORWARD_TIMEOFFSET"


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    except sqlite3.Error:
        return []


def discover_display_tables(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    """Candidate display-event tables, with the time and state columns identified."""
    found: List[Dict[str, str]] = []
    for t in _tables(conn):
        if not _DISPLAY_TABLE_RE.search(t):
            continue
        cols = _columns(conn, t)
        tcol = next((c for c in cols if _TIME_COL_RE.search(c)), None)
        if not tcol:
            continue
        scol = next((c for c in cols if _STATE_COL_RE.search(c)), None)
        found.append({"table": t, "time_column": tcol, "state_column": scol or ""})
    return found


def time_offset(conn: sqlite3.Connection) -> float:
    """Seconds to add to a monotonic timestamp to get Unix wall-clock time.

    Returns 0.0 when the offset table is absent, which makes the failure VISIBLE as implausible
    timestamps rather than silently shifting every anchor by the device's uptime.
    """
    try:
        cols = _columns(conn, _OFFSET_TABLE)
        if not cols:
            return 0.0
        tcol = next((c for c in cols if _TIME_COL_RE.search(c)), None)
        ocol = next((c for c in cols if re.search(r"offset", c, re.I)), None)
        if not tcol or not ocol:
            return 0.0
        row = conn.execute(
            f'SELECT "{ocol}" FROM "{_OFFSET_TABLE}" ORDER BY "{tcol}" DESC LIMIT 1').fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except (sqlite3.Error, TypeError, ValueError):
        return 0.0


def screen_on_events(db_path: str, lo: Optional[datetime] = None,
                     hi: Optional[datetime] = None,
                     min_gap_s: float = 60.0) -> Tuple[List[str], Dict[str, object]]:
    """Screen-on timestamps (ISO, UTC) plus a report of how they were found.

    ``min_gap_s`` collapses the rapid on/off flicker a single glance produces into one event, so
    checking the clock once counts once.
    """
    report: Dict[str, object] = {"tables": [], "offset_s": 0.0, "raw_events": 0}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        report["error"] = repr(exc)
        return [], report
    try:
        cands = discover_display_tables(conn)
        report["tables"] = cands
        off = time_offset(conn)
        report["offset_s"] = off
        stamps: List[float] = []
        for c in cands:
            tcol, scol = c["time_column"], c["state_column"]
            try:
                if scol:
                    rows = conn.execute(
                        f'SELECT "{tcol}", "{scol}" FROM "{c["table"]}"').fetchall()
                else:
                    rows = [(r[0], 1) for r in conn.execute(
                        f'SELECT "{tcol}" FROM "{c["table"]}"').fetchall()]
            except sqlite3.Error:
                continue
            for t, state in rows:
                if t is None:
                    continue
                # Only transitions INTO a lit screen. A zero/false state is the screen going off,
                # which is not evidence of being awake -- it is evidence of having stopped looking.
                try:
                    if state is not None and float(state) <= 0:
                        continue
                    stamps.append(float(t) + off)
                except (TypeError, ValueError):
                    continue
        report["raw_events"] = len(stamps)
    finally:
        conn.close()

    out: List[str] = []
    last = None
    for t in sorted(stamps):
        try:
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        if lo is not None and dt < lo:
            continue
        if hi is not None and dt > hi:
            continue
        if last is None or (t - last) >= min_gap_s:
            out.append(dt.isoformat())
            last = t
    report["anchors"] = len(out)
    return out, report
