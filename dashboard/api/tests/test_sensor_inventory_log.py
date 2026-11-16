"""The daemon logs what Eight Sleep's sleep data includes, once per change, and publishes it."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

from sleepctl.config import AppConfig  # noqa: E402
from sleepctl.loop.live import SimulatedLiveClient  # noqa: E402

from app.db import get_repo  # noqa: E402
from live_daemon import LiveDashboardDaemon  # noqa: E402


def test_the_inventory_is_logged_once_per_change(capsys):
    client = SimulatedLiveClient(scenario="normal", seed=7)
    d = LiveDashboardDaemon(AppConfig.default(), client, get_repo(), dry_run=True, verbose=True)
    capsys.readouterr()
    client.sensor_inventory = {"trends_keys": [], "stages": False, "presence": False,
                               "trends_error": "HTTP 403 Subscription required", "checked": "t1"}
    d._log_sensor_inventory()
    assert "feed refused" in capsys.readouterr().out
    client.sensor_inventory = {"trends_keys": ["heartRate", "tempBedC", "tempRoomC"],
                               "stages": True, "presence": True, "trends_error": None,
                               "checked": "t2"}
    d._log_sensor_inventory()
    out = capsys.readouterr().out
    assert "heart rate, bed temperature, room temperature, sleep stages, in-bed times" in out
    d._log_sensor_inventory()                      # unchanged: no repeat line
    assert capsys.readouterr().out == ""


def test_the_recorded_nights_and_side_are_logged(capsys):
    client = SimulatedLiveClient(scenario="normal", seed=7)
    d = LiveDashboardDaemon(AppConfig.default(), client, get_repo(), dry_run=True, verbose=True)
    capsys.readouterr()
    client.sensor_inventory = {"trends_keys": [], "stages": False, "presence": False,
                               "trends_error": None, "checked": "t1",
                               "nights_7d": [{"day": "2026-11-14", "sessions": 0},
                                             {"day": "2026-11-15", "sessions": 1}],
                               "assigned_side": None, "reading_side": "left", "away": False}
    d._log_sensor_inventory()
    out = capsys.readouterr().out
    assert "recorded 1 of the last 2 nights" in out and "assigned the NO side" in out
