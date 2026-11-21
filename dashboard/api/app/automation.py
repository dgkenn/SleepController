"""Hands-free nights: the iPhone's Sleep schedule drives the controller.

iOS Shortcuts has personal automations on the Health sleep schedule -- "Wind Down Begins",
"Bedtime Begins", "Waking Up" -- that can run without asking. Each one POSTs here:

    POST /api/automation/sleep  {"event": "bedtime", "alarm": "<the next alarm, any format>"}

and the box does what the buttons would have done:

* any event carrying an alarm  -> arm the smart wake for that time (set_wake), once per change;
* ``wind_down``                -> nothing else yet (recorded; the wake is armed early);
* ``bedtime``                  -> start the onset program (induce_sleep), unless a night is
                                  already running;
* ``wake``                     -> end the night and start the morning light (woke_up), only if
                                  a night is running -- the trigger also fires on days off.

The alarm comes from Shortcuts' alarm list, whose text format depends on the phone's locale and
how the shortcut formats it, so the parser accepts "7:00 AM", "07:00", "7:00", ISO datetimes and
"Nov 22, 2026 at 7:00 AM". The wake time is kept as local HH:MM, which is what set_wake takes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import bridge

EVENTS = {"wind_down", "bedtime", "wake"}
_ALIASES = {"winddown": "wind_down", "wind down": "wind_down", "wind_down_begins": "wind_down",
            "bed": "bedtime", "bedtime_begins": "bedtime", "sleep": "bedtime",
            "waking_up": "wake", "waking up": "wake", "wake_up": "wake", "woke_up": "wake",
            "awake": "wake", "bedtime begins": "bedtime", "wind down begins": "wind_down"}
RUNNING_STATES = {"induction", "maintenance", "wake_recovery", "wake_window", "calibration"}

_TIME = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?::\d{2})?\s*([ap]\.?\s?m\.?)?", re.I)


def normalize_event(value) -> Optional[str]:
    v = str(value or "").strip().lower().replace("-", "_")
    v = _ALIASES.get(v, _ALIASES.get(v.replace("_", " "), v))
    return v if v in EVENTS else None


def parse_alarm(value) -> Optional[str]:
    """Any alarm text the phone may send -> local "HH:MM", or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:   # ISO first: its offset is the phone's, and the hour is local to it
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if "T" in text or " " in text.strip():
            return f"{dt.hour:02d}:{dt.minute:02d}"
    except ValueError:
        pass
    m = None
    for m in _TIME.finditer(text):
        pass   # the LAST time in the text (a date like "2026.11" can precede it)
    if m is None:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "")
    ampm = ampm.replace(" ", "")
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def _night_running(conn) -> bool:
    rt = bridge.read_runtime_state(conn)
    return str(rt.get("state") or "").lower() in RUNNING_STATES and not rt.get("stale")


def _last_alarm_set(conn) -> Optional[str]:
    """The last wake time this module armed, if armed in the last 20 h (so a new night re-arms)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    row = conn.execute("SELECT alarm FROM automation_events WHERE alarm IS NOT NULL AND ts >= ? "
                       "AND actions LIKE '%set_wake%' ORDER BY id DESC LIMIT 1", (since,)).fetchone()
    return row[0] if row else None


def handle(conn, payload: dict) -> dict:
    event = normalize_event(payload.get("event"))
    if event is None:
        return {"ok": False, "error": "event must be wind_down, bedtime or wake",
                "got": str(payload.get("event"))[:40]}
    raw = payload.get("alarm")
    alarm = parse_alarm(raw)
    actions: list = []

    if alarm and event != "wake":
        if alarm == _last_alarm_set(conn):
            actions.append({"skipped": "set_wake", "why": f"already armed for {alarm}"})
        else:
            cid = bridge.enqueue_command(conn, "set_wake", {"wake_time": alarm})
            actions.append({"queued": "set_wake", "wake_time": alarm, "command_id": cid})
    elif raw not in (None, "") and not alarm:
        actions.append({"skipped": "set_wake", "why": "could not read a time from the alarm"})

    if event == "bedtime":
        if _night_running(conn):
            actions.append({"skipped": "induce_sleep", "why": "a night is already running"})
        else:
            actions.append({"queued": "induce_sleep",
                            "command_id": bridge.enqueue_command(conn, "induce_sleep", None)})
    elif event == "wake":
        if _night_running(conn):
            actions.append({"queued": "woke_up",
                            "command_id": bridge.enqueue_command(conn, "woke_up", None)})
        else:
            actions.append({"skipped": "woke_up", "why": "no night is running"})

    conn.execute("INSERT INTO automation_events (ts, event, alarm, alarm_raw, actions, source) "
                 "VALUES (?,?,?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), event, alarm,
                  None if raw is None else str(raw)[:120], json.dumps(actions),
                  str(payload.get("source") or "shortcuts")[:40]))
    conn.commit()
    return {"ok": True, "event": event, "alarm": alarm, "actions": actions}


def summary(conn, days: int = 7) -> dict:
    """Counts per event and whether the last call did anything -- no times (public snapshot)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT event, COUNT(*) FROM automation_events WHERE ts >= ? "
                        "GROUP BY event", (since,)).fetchall()
    last = conn.execute("SELECT event, actions FROM automation_events ORDER BY id DESC "
                        "LIMIT 1").fetchone()
    out = {"events_7d": {e: n for e, n in rows}}
    if last:
        acts = json.loads(last[1] or "[]")
        out["last"] = {"event": last[0],
                       "did": [a.get("queued") or f"skipped {a.get('skipped')}" for a in acts]}
    return out
