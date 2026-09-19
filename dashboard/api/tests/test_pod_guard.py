"""Exclusive control: the daemon writes its level back over anything else that sets the bed.

The Eight Sleep app's schedule cannot be disabled through the API on this account (403), so
the only defence that always works is to notice the device's accepted target disagreeing with
ours and re-assert ours -- every time, within about a minute, while the Settings toggle is on.
"""
import asyncio
import json
from datetime import datetime, timedelta

from sleepctl.models import SensorFrame, SleepStage

from test_live_daemon import _daemon


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _frame(target_level):
    return SensorFrame(timestamp=datetime.now(), stage=SleepStage.LIGHT, heart_rate=60.0,
                       presence=None, target_level=target_level, device_level=target_level,
                       data_age_seconds=5.0)


def _armed(commanded=-58, ago_s=600):
    d, client, repo = _daemon()
    writes = []

    async def set_heating_level(level, duration_s=0):
        writes.append(level)

    client.set_heating_level = set_heating_level
    d.power_on, d.paused, d.away, d.mode = True, False, False, "auto"
    d._last_commanded_level = commanded
    d._last_command_at = datetime.now() - timedelta(seconds=ago_s)
    return d, repo, writes


def test_a_foreign_target_is_overridden_after_two_reads():
    d, repo, writes = _armed()
    now = datetime.now()
    assert _run(d._guard_pod(_frame(-30), now)) is False        # first read: streak 1
    assert writes == []
    assert _run(d._guard_pod(_frame(-30), now + timedelta(seconds=60))) is True
    assert writes == [-58]
    s = d._pod_guard_summary()
    assert s["enabled"] is True and s["reasserts_24h"] == 1
    assert s["last_observed_level"] == -30 and s["last_commanded_level"] == -58


def test_agreement_resets_the_streak():
    d, repo, writes = _armed()
    now = datetime.now()
    _run(d._guard_pod(_frame(-30), now))
    _run(d._guard_pod(_frame(-57), now + timedelta(seconds=60)))     # back in agreement
    assert _run(d._guard_pod(_frame(-30), now + timedelta(seconds=120))) is False
    assert writes == []


def test_our_own_fresh_command_is_given_time_to_settle():
    d, repo, writes = _armed(ago_s=10)
    now = datetime.now()
    for k in range(3):
        _run(d._guard_pod(_frame(-30), now + timedelta(seconds=k)))
    assert writes == []


def test_the_settings_toggle_turns_the_guard_off():
    d, repo, writes = _armed()
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('pod_guard', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(False),))
    repo.conn.commit()
    d._guard_enabled_cache = None
    now = datetime.now()
    for k in range(3):
        _run(d._guard_pod(_frame(-30), now + timedelta(seconds=60 * k)))
    assert writes == []
    assert d._pod_guard_summary()["enabled"] is False


def test_reasserts_are_rate_limited():
    d, repo, writes = _armed()
    now = datetime.now()
    for k in range(6):                       # six disagreeing reads a minute apart
        _run(d._guard_pod(_frame(-30), now + timedelta(seconds=60 * k)))
    assert len(writes) <= 3


def test_the_default_setting_is_on(auth_client):
    r = auth_client.get("/settings")
    assert r.status_code == 200
    assert r.json()["defaults"]["pod_guard"] is True


def test_the_conflict_check_reports_the_guard():
    from app import diagnostics
    c = diagnostics._check_external_conflict(
        None, {"pod_guard": {"enabled": True, "reasserts_24h": 4,
                             "last_observed_level": -30, "last_commanded_level": -58}},
        history=[])
    assert c["status"] == "warn"
    assert "overrode 4" in c["detail"]
    c = diagnostics._check_external_conflict(None, {"pod_guard": {"enabled": False, "reasserts_24h": 0}},
                                             history=[])
    assert "OFF" in c["detail"]
