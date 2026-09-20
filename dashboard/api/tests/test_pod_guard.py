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


durations = []


def _armed(commanded=-58, ago_s=600):
    d, client, repo = _daemon()
    writes = []
    durations.clear()
    # The daemon fixture shares one database across the file; the toggle test below writes
    # pod_guard=false into it, so every test starts from the shipped default.
    repo.conn.execute("DELETE FROM settings_kv WHERE key='pod_guard'")
    repo.conn.commit()
    d._guard_enabled_cache = None

    async def set_heating_level(level, duration_s=0):
        writes.append(level)
        durations.append(duration_s)

    client.set_heating_level = set_heating_level
    d.power_on, d.paused, d.away, d.mode = True, False, False, "auto"
    d._last_commanded_level = commanded
    d._last_command_at = datetime.now() - timedelta(seconds=ago_s)
    return d, repo, writes


def test_a_foreign_accepted_target_with_no_schedule_is_the_user_and_is_honoured():
    """Before 2026-09-20 this case was re-asserted. The night showed who moves the accepted
    target when no schedule is driving: the person in the bed, who was cold."""
    d, repo, writes = _armed()
    now = datetime.now()
    assert _run(d._guard_pod(_frame(-30), now)) is False
    assert writes == []
    assert d._last_commanded_level == -30
    s = d._pod_guard_summary()
    assert s["enabled"] is True and s["reasserts_24h"] == 0 and s["user_overrides_24h"] == 1


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


# ------------------------------------------------------- 2026-09-20: the schedule takeover
def _schedule(d, target, activity="schedule"):
    """The device's own session, as device_status reports it: "schedule" is the app's bedtime
    schedule (fought); "temperatureControl" is a person setting a temperature (honoured)."""
    d._safe_device_status = lambda: {"external_schedule": {
        "activity": activity, "target_level": target, "active": True}}


def test_the_schedule_register_is_watched_even_right_after_our_own_write():
    """01:03-04:25: the accepted target kept echoing our -58 while the schedule's target read -3
    and the water followed it. The old guard compared only the accepted target and saw peace."""
    d, repo, writes = _armed(commanded=-58, ago_s=10)      # we wrote 10 s ago
    _schedule(d, -3)
    now = datetime.now()
    assert _run(d._guard_pod(_frame(-58), now)) is False              # read 1
    assert _run(d._guard_pod(_frame(-58), now + timedelta(seconds=60))) is True
    assert writes == [-58]
    s = d._pod_guard_summary()
    assert s["reasserts_24h"] == 1 and s["last_observed_level"] == -3


def test_a_schedule_honouring_our_level_is_not_a_fight():
    d, repo, writes = _armed(commanded=-58, ago_s=600)
    _schedule(d, -57)
    now = datetime.now()
    for k in range(3):
        _run(d._guard_pod(_frame(-58), now + timedelta(seconds=60 * k)))
    assert writes == []


def test_every_write_is_a_timed_override_not_a_bare_level_set():
    """A bare currentLevel set is what the schedule supersedes; the timed override is the
    app's own mechanism for beating its schedule."""
    d, repo, writes = _armed()
    _run(d._set_level(-58))
    assert writes == [-58]
    assert durations == [7200]


def test_a_long_hold_is_renewed_before_the_override_lapses():
    """The controller only writes when its target changes; a 3-hour hold would let the
    schedule back in once the override expired."""
    d, repo, writes = _armed(commanded=-58, ago_s=7200 // 2 + 5)
    now = datetime.now()
    _run(d._guard_pod(_frame(-58), now))
    assert writes == [-58]
    assert durations == [7200]


# ------------------------------------------------- 2026-09-20 01:03: the user's own hand
def test_a_manual_change_from_the_phone_is_honoured_not_fought():
    """The user woke cold at 68F and set 80F. Writing 68F back every two minutes would have
    fought them all night."""
    d, repo, writes = _armed(commanded=-58, ago_s=600)
    _schedule(d, -3, activity="temperatureControl")
    now = datetime.now()
    assert _run(d._guard_pod(_frame(-58), now)) is False
    assert writes == [], "the guard wrote our level back over the user's"
    assert d._last_commanded_level == -3            # we hold THEIR level now
    assert d._user_override_active(now + timedelta(minutes=30))
    assert not d._user_override_active(now + timedelta(minutes=61))
    s = d._pod_guard_summary()
    assert s["user_overrides_24h"] == 1 and s["reasserts_24h"] == 0


def test_a_warmer_override_raises_tonights_floor_a_degree_above_where_they_were():
    d, repo, writes = _armed(commanded=-58, ago_s=600)
    _schedule(d, -3, activity="temperatureControl")
    _run(d._guard_pod(_frame(-58), datetime.now()))
    from sleepctl.controller.thermal import default_level_to_f
    prior = default_level_to_f(-58)
    assert d.cycle.controller.session_floor_f == prior + 1.0
    assert d.cycle.controller.session_ceiling_f is None


def test_a_cooler_override_lowers_tonights_ceiling():
    d, repo, writes = _armed(commanded=-20, ago_s=600)
    _schedule(d, -60, activity="temperatureControl")
    _run(d._guard_pod(_frame(-20), datetime.now()))
    from sleepctl.controller.thermal import default_level_to_f
    assert d.cycle.controller.session_ceiling_f == default_level_to_f(-20) - 1.0


def test_the_accepted_target_moving_with_no_schedule_is_also_the_user():
    d, repo, writes = _armed(commanded=-58, ago_s=600)
    now = datetime.now()
    _run(d._guard_pod(_frame(-3), now))               # accepted target -3, no schedule active
    assert writes == []
    assert d._last_commanded_level == -3


def test_the_schedule_is_still_fought_after_a_user_override_expires():
    d, repo, writes = _armed(commanded=-58, ago_s=600)
    _schedule(d, -3, activity="schedule")
    now = datetime.now()
    _run(d._guard_pod(_frame(-58), now))
    assert _run(d._guard_pod(_frame(-58), now + timedelta(seconds=60))) is True
    assert writes == [-58]
