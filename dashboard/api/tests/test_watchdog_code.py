"""The watchdog PROCESS vs the watchdog SCRIPT: the 08-28 cadence change sat undeployed for
eleven days because the file changed and the process did not. These pin the detection, the
safety guard, and the rate limit."""
from __future__ import annotations

import hashlib
import os
import time

from app import watchdog_code as wc

NOW = time.time()


def _box(tmp_path, *, script="param()\n# v1\n", marker=None, started="2026-09-01 08:00:00",
         heartbeat=True):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "windows-watchdog.ps1").write_text(script, encoding="utf-8")
    run = tmp_path / ".run"
    run.mkdir()
    if heartbeat:
        (run / "watchdog.heartbeat").write_text("x")
    if started:
        (run / "watchdog.log").write_text(
            f"2026-08-01 07:00:00  watchdog starting (root=C:\\x)\n"
            f"2026-08-01 07:00:15  api up\n"
            f"{started}  watchdog starting (root=C:\\x)\n"
            f"{started[:-2]}30  api up\n", encoding="utf-8")
    if marker == "match":
        (run / "watchdog-code.hash").write_text(
            hashlib.sha256(script.encode()).hexdigest().upper() + "\n")
    elif marker:
        (run / "watchdog-code.hash").write_text(marker)
    return str(root), str(run)


def test_missing_marker_means_the_process_predates_the_marker_code(tmp_path):
    root, run = _box(tmp_path)
    a = wc.assess(root, run, NOW)
    assert a["stale"] is True and a["marker_present"] is False
    assert a["running_since"] == "2026-09-01 08:00:00"


def test_matching_marker_is_current(tmp_path):
    root, run = _box(tmp_path, marker="match")
    assert wc.assess(root, run, NOW)["stale"] is False
    assert wc.maybe_request_restart(root, run, NOW) == "current"


def test_differing_marker_is_stale_case_insensitively(tmp_path):
    root, run = _box(tmp_path, marker="abc123")
    assert wc.assess(root, run, NOW)["stale"] is True
    root, run = _box(tmp_path / "b", marker="match")
    p = os.path.join(run, "watchdog-code.hash")
    low = open(p).read().lower()
    open(p, "w").write(low)
    assert wc.assess(root, run, NOW)["stale"] is False


def test_no_heartbeat_means_nothing_to_compare(tmp_path):
    root, run = _box(tmp_path, heartbeat=False)
    assert wc.assess(root, run, NOW)["stale"] is None
    assert wc.maybe_request_restart(root, run, NOW) == "unknown"


def test_stale_and_recent_enough_gets_a_restart_request_once(tmp_path):
    root, run = _box(tmp_path)
    assert wc.maybe_request_restart(root, run, NOW) == "requested"
    assert open(os.path.join(run, "restart.request")).read() == "watchdog"
    # the request is pending until the watchdog consumes it
    assert wc.maybe_request_restart(root, run, NOW) == "pending"
    os.remove(os.path.join(run, "restart.request"))
    # consumed but still stale (marker never appeared): rate-limited, not hammered
    assert wc.maybe_request_restart(root, run, NOW + 60) == "rate_limited"
    later = NOW + wc.AUTO_REQUEST_EVERY_S + 1
    os.utime(os.path.join(run, "watchdog.heartbeat"), (later, later))  # still alive at `later`
    os.utime(os.path.join(run, wc.AUTO_MARKER), (NOW, NOW))              # pin the marker to NOW
    assert wc.maybe_request_restart(root, run, later) == "requested"


def test_a_process_older_than_the_self_restart_fix_is_never_asked(tmp_path):
    """Before 2026-08-05 the self-restart just exited and Task Scheduler never relaunched it --
    asking that process to restart is the 08-05 outage again. Report it; don't press the button."""
    root, run = _box(tmp_path, started="2026-08-03 22:00:00")
    a = wc.assess(root, run, NOW)
    assert a["stale"] is True and a["safe_to_self_restart"] is False
    assert wc.maybe_request_restart(root, run, NOW) == "unsafe"
    assert not os.path.exists(os.path.join(run, "restart.request"))


def test_unknown_start_time_is_treated_as_unsafe(tmp_path):
    root, run = _box(tmp_path, started=None)
    assert wc.maybe_request_restart(root, run, NOW) == "unsafe"


def test_start_time_falls_back_to_the_rotated_log(tmp_path):
    root, run = _box(tmp_path, started=None)
    open(os.path.join(run, "watchdog.log.1"), "w").write("2026-08-20 01:02:03  watchdog starting (root=x)\n")
    assert wc.running_since(run) is not None
    assert wc.maybe_request_restart(root, run, NOW) == "requested"


def test_diagnostics_check_reports_the_verdict(tmp_path, monkeypatch):
    from app import diagnostics
    root, run = _box(tmp_path)
    c = diagnostics._check_watchdog_code(root, run, NOW)
    assert c["id"] == "watchdog_code" and c["status"] == "warn"
    assert "automatically" in c["detail"]
    root, run = _box(tmp_path / "old", started="2026-08-03 22:00:00")
    c = diagnostics._check_watchdog_code(root, run, NOW)
    assert c["status"] == "warn" and "not requested automatically" in c["detail"]
    root, run = _box(tmp_path / "cur", marker="match")
    assert diagnostics._check_watchdog_code(root, run, NOW)["status"] == "ok"
