"""The bed-level dip watch logs the start and end of a dip with the Pod's flags."""

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

from live_daemon import LiveDashboardDaemon  # noqa: E402


def _daemon(status):
    d = object.__new__(LiveDashboardDaemon)
    d.logs = []
    d._log = d.logs.append
    d._skip = lambda *a, **k: d.logs.append("SKIP")
    d._session_running = lambda: False
    d._safe_device_status = lambda: status
    return d


def test_dip_logged_once_with_flags_then_closed():
    st = {"side_on": False, "priming": False,
          "external_schedule": {"activity": "schedule", "target_level": 7}}
    d = _daemon(st)
    t0 = datetime(2026, 11, 22, 1, 0)
    for i, lvl in enumerate([7, 6, -2, -9, -14, 7]):
        d._watch_level_dips(SimpleNamespace(device_level=lvl, target_level=7),
                            t0 + timedelta(minutes=i))
    assert len(d.logs) == 2
    assert "bed level dip: level -2" in d.logs[0] and "side_on=False" in d.logs[0]
    assert "schedule=schedule/7" in d.logs[0]
    assert "over after 3 min (lowest -14)" in d.logs[1]


def test_no_target_no_log():
    d = _daemon({})
    d._watch_level_dips(SimpleNamespace(device_level=-50, target_level=None), datetime.now())
    assert d.logs == []
