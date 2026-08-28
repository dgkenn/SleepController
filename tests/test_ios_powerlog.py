"""iOS screen-on events as objective wake anchors.

The live route is closed: iOS personal automations do not run while the device is locked, and
NOT unlocking is the behaviour being measured -- the user presses the side button to read the
lock-screen clock. Every live alternative (NFC, Back Tap, a posting shortcut) requires an unlock,
which would change the thing under observation and add an arousal to it.

Retrospective extraction avoids that entirely, and is stronger for it: iOS retains ~7 days of
PowerLog, so one capture yields anchors for nights ALREADY recorded, on which the user slept
normally and unaware of any marking.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from sleepctl.eval.ios_powerlog import discover_display_tables, screen_on_events, time_offset

OFFSET = 1_787_000_000.0


def _db(rows, table="PLDISPLAYAGENT_EVENTFORWARD_DISPLAY", offset=OFFSET, state_col=True):
    path = os.path.join(tempfile.mkdtemp(), "CurrentPowerlog.PLSQL")
    c = sqlite3.connect(path)
    if state_col:
        c.execute(f'CREATE TABLE "{table}" (timestamp REAL, state INTEGER)')
        c.executemany(f'INSERT INTO "{table}" VALUES (?,?)', rows)
    else:
        c.execute(f'CREATE TABLE "{table}" (timestamp REAL)')
        c.executemany(f'INSERT INTO "{table}" VALUES (?)', [(r[0],) for r in rows])
    if offset is not None:
        c.execute("CREATE TABLE PLSTORAGEOPERATOR_EVENTFORWARD_TIMEOFFSET "
                  "(timestamp REAL, system_offset REAL)")
        c.execute("INSERT INTO PLSTORAGEOPERATOR_EVENTFORWARD_TIMEOFFSET VALUES (1,?)", (offset,))
    c.commit()
    c.close()
    return path


def test_the_display_table_is_discovered_not_hardcoded():
    """~700 tables, varying by device and iOS version -- a hardcoded name breaks silently on the
    next release."""
    conn = sqlite3.connect(_db([(100.0, 1)], table="PLBACKLIGHTAGENT_EVENTPOINT_BACKLIGHT"))
    found = discover_display_tables(conn)
    assert found and found[0]["time_column"] == "timestamp"


def test_screen_on_events_are_extracted():
    ev, rep = screen_on_events(_db([(100.0, 1), (400.0, 1), (7300.0, 1)]))
    assert len(ev) == 3 and rep["offset_s"] == OFFSET


def test_screen_off_is_not_an_anchor():
    """Screen OFF is evidence of having stopped looking, not of being awake."""
    ev, _ = screen_on_events(_db([(100.0, 1), (102.0, 0), (104.0, 0)]))
    assert len(ev) == 1


def test_the_monotonic_offset_is_applied():
    ev, _ = screen_on_events(_db([(100.0, 1)]))
    got = datetime.fromisoformat(ev[0])
    assert abs(got.timestamp() - (100.0 + OFFSET)) < 1.0


def test_a_missing_offset_table_yields_visibly_wrong_times_not_silently_shifted_ones():
    """Returning 0.0 makes the failure show up as an implausible 1970 timestamp rather than
    shifting every anchor by the device's uptime, which would look plausible and be wrong."""
    ev, rep = screen_on_events(_db([(100.0, 1)], offset=None))
    assert rep["offset_s"] == 0.0
    assert datetime.fromisoformat(ev[0]).year < 2000


def test_one_glance_at_the_clock_counts_once():
    """A single look produces rapid on/off/on flicker."""
    ev, _ = screen_on_events(_db([(100.0, 1), (101.0, 1), (103.0, 1)]))
    assert len(ev) == 1


def test_separate_awakenings_stay_separate():
    ev, _ = screen_on_events(_db([(100.0, 1), (4000.0, 1)]))
    assert len(ev) == 2


def test_events_outside_the_night_window_are_excluded():
    lo = datetime.fromtimestamp(OFFSET + 1000, tz=timezone.utc)
    hi = datetime.fromtimestamp(OFFSET + 2000, tz=timezone.utc)
    ev, _ = screen_on_events(_db([(100.0, 1), (1500.0, 1), (9000.0, 1)]), lo=lo, hi=hi)
    assert len(ev) == 1


def test_a_table_without_a_state_column_is_still_usable():
    ev, _ = screen_on_events(_db([(100.0, 1), (4000.0, 1)], state_col=False))
    assert len(ev) == 2


def test_a_missing_or_unreadable_database_reports_rather_than_raising():
    ev, rep = screen_on_events("/nonexistent/CurrentPowerlog.PLSQL")
    assert ev == [] and "error" in rep


def test_the_database_is_opened_read_only():
    """Never modify a forensic artifact the user handed over."""
    path = _db([(100.0, 1)])
    screen_on_events(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM PLDISPLAYAGENT_EVENTFORWARD_DISPLAY").fetchone()[0] == 1
