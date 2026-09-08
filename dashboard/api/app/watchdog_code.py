"""Is the watchdog PROCESS running the watchdog SCRIPT on disk?

The watchdog reads its settings (publish cadences, Ensure-Verity behaviour, the BT-reset
handler) into ``$script:`` variables once at startup. A self-update replaces the file but not
the process, so a settings change can deploy and silently never take effect. Observed twice:
the health-publish interval was cut 10 min -> 3 min on 2026-08-28 and publishes stayed on the
10-minute cadence into September, because the process that was supposed to notice the change
was itself the process that predated the noticing code.

Two signals, one verdict:

* ``.run/watchdog-code.hash`` -- written by the watchdog at startup (new code). Equal to the
  SHA-256 of ``scripts/windows-watchdog.ps1`` means CURRENT; different means STALE; absent
  means the running watchdog predates the marker, i.e. STALE by construction.
* ``.run/watchdog.log`` -- the last ``watchdog starting`` line dates the running process. Used
  as the SAFETY GUARD: before 2026-08-05 the watchdog's self-restart just exited, and Task
  Scheduler treated that as "done" and never relaunched it (the 08-05 outage). A process that
  old must NOT be asked to restart itself from here -- it is reported for a hands-on restart.

The API's health thread calls ``maybe_request_restart`` once a minute; it writes the same
``restart.request = watchdog`` flag the ``/diag/action/restart-watchdog`` endpoint writes, with a
rate limit so a watchdog that somehow fails to write its marker is asked at most every 30 min.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime

WATCHDOG_SCRIPT = os.path.join("scripts", "windows-watchdog.ps1")
MARKER = "watchdog-code.hash"
AUTO_MARKER = "watchdog-restart.auto"
LOG = "watchdog.log"
HEARTBEAT = "watchdog.heartbeat"
HEARTBEAT_FRESH_S = 120
AUTO_REQUEST_EVERY_S = 30 * 60
# Processes started before this are running the exit-and-hope self-restart (see module doc).
SELF_RESTART_SAFE_SINCE = datetime(2026, 8, 6, 0, 0, 0)
_START_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+watchdog starting")
_LOG_TAIL_BYTES = 4 * 1024 * 1024


def script_hash(repo_root: str) -> str | None:
    try:
        with open(os.path.join(repo_root, WATCHDOG_SCRIPT), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest().upper()
    except OSError:
        return None


def marker_hash(run_dir: str) -> str | None:
    try:
        with open(os.path.join(run_dir, MARKER), "r", encoding="utf-8", errors="replace") as fh:
            v = fh.read().strip().upper()
            return v or None
    except OSError:
        return None


def _scan_last_start(path: str) -> datetime | None:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > _LOG_TAIL_BYTES:
                fh.seek(size - _LOG_TAIL_BYTES)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in text.splitlines():
        m = _START_RE.match(line)
        if m:
            try:
                last = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return last


def running_since(run_dir: str) -> datetime | None:
    """Local naive time of the last ``watchdog starting`` line, or None if unknown (no log, or
    the line has rotated out)."""
    for name in (LOG, LOG + ".1"):
        t = _scan_last_start(os.path.join(run_dir, name))
        if t is not None:
            return t
    return None


def assess(repo_root: str, run_dir: str, now: float | None = None) -> dict:
    import time
    now = time.time() if now is None else now
    on_disk = script_hash(repo_root)
    marker = marker_hash(run_dir)
    started = running_since(run_dir)
    hb_path = os.path.join(run_dir, HEARTBEAT)
    try:
        hb_age = now - os.path.getmtime(hb_path)
    except OSError:
        hb_age = None
    running = hb_age is not None and hb_age <= HEARTBEAT_FRESH_S

    try:
        script_changed = datetime.fromtimestamp(os.path.getmtime(os.path.join(repo_root, WATCHDOG_SCRIPT)))
    except OSError:
        script_changed = None

    if on_disk is None:
        stale, reason = None, "no watchdog script in this checkout"
    elif not running:
        stale, reason = None, "watchdog heartbeat missing or stale -- nothing to compare against"
    elif marker is None:
        stale = True
        reason = "the running watchdog never wrote its code marker -- it predates the marker code"
    elif marker != on_disk:
        stale, reason = True, "the running watchdog's code marker differs from the script on disk"
    else:
        stale, reason = False, "running watchdog matches the script on disk"

    safe = started is not None and started >= SELF_RESTART_SAFE_SINCE
    return {
        "stale": stale, "reason": reason, "running": running,
        "running_since": started.isoformat(sep=" ") if started else None,
        "script_changed": script_changed.isoformat(sep=" ") if script_changed else None,
        "marker_present": marker is not None,
        "safe_to_self_restart": safe,
    }


def _last_auto_request_age(run_dir: str, now: float) -> float | None:
    try:
        return now - os.path.getmtime(os.path.join(run_dir, AUTO_MARKER))
    except OSError:
        return None


def maybe_request_restart(repo_root: str, run_dir: str, now: float | None = None) -> str:
    """Write ``restart.request = watchdog`` when the running watchdog is stale AND it is safe to
    ask it to restart itself. Returns a one-word outcome for logs/tests:
    ``requested`` | ``current`` | ``unknown`` | ``unsafe`` | ``pending`` | ``rate_limited``."""
    import time
    now = time.time() if now is None else now
    a = assess(repo_root, run_dir, now)
    if a["stale"] is None:
        return "unknown"
    if not a["stale"]:
        return "current"
    if not a["safe_to_self_restart"]:
        return "unsafe"
    if os.path.exists(os.path.join(run_dir, "restart.request")):
        return "pending"
    age = _last_auto_request_age(run_dir, now)
    if age is not None and age < AUTO_REQUEST_EVERY_S:
        return "rate_limited"
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, AUTO_MARKER), "w", encoding="utf-8") as fh:
        fh.write(datetime.now().isoformat())
    with open(os.path.join(run_dir, "restart.request"), "w", encoding="utf-8") as fh:
        fh.write("watchdog")
    return "requested"
