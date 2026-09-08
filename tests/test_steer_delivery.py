"""An actuated deepen whose water never moved is a control condition, not evidence about
cooling. Through 2026-09-07 every deepen resolved against a bed pinned at neutral."""
from datetime import datetime, timedelta

from sleepctl.models import ControllerState, CorrectionAction, Decision, NightObjective
from sleepctl.storage.repository import Repository
from sleepctl.controller.thermal import ThermalIntent

NIGHT = "2026-09-07"
T0 = datetime(2026, 9, 8, 0, 30)


def _repo(tmp_path):
    return Repository(str(tmp_path / "s.db"), check_same_thread=False)


def _decision(ts, target_f):
    return Decision(ts, ControllerState.MAINTENANCE, NightObjective.OPTIMIZE,
                    ThermalIntent.DEEP_BIAS_COOL, target_f, -54, CorrectionAction.HOLD, "x", 0.5, {})


def _night(repo, cooled: bool):
    # a minute of decisions before the event at 69.5, then 20 minutes after
    for i in range(3):
        repo.log_decision(_decision(T0 - timedelta(minutes=3 - i), 69.5), NIGHT)
    for i in range(1, 40):
        repo.log_decision(_decision(T0 + timedelta(seconds=30 * i), 67.0 if cooled else 69.5), NIGHT)
    repo.log_steer_event(NIGHT, T0, "deepen", "light", 30.0, 0.3, 20.0, applied=1)
    repo.conn.execute("UPDATE steer_events SET resolved=1, deepened=0, succeeded=0, caused_wake=0")
    repo.conn.commit()


def test_a_deepen_that_never_moved_the_water_is_reclassified_as_control(tmp_path):
    repo = _repo(tmp_path)
    _night(repo, cooled=False)
    rows = repo.maneuver_records("deepen")
    assert len(rows) == 1
    assert rows[0]["applied"] == 0 and rows[0]["delivered"] is False


def test_a_deepen_that_cooled_the_bed_stays_actuated(tmp_path):
    repo = _repo(tmp_path)
    _night(repo, cooled=True)
    rows = repo.maneuver_records("deepen")
    assert rows[0]["applied"] == 1 and rows[0]["delivered"] is True


def test_an_event_with_no_decision_log_around_it_is_left_as_recorded(tmp_path):
    repo = _repo(tmp_path)
    repo.log_steer_event(NIGHT, T0, "deepen", "light", 30.0, 0.3, 20.0, applied=1)
    repo.conn.execute("UPDATE steer_events SET resolved=1, deepened=1, succeeded=1, caused_wake=0")
    repo.conn.commit()
    rows = repo.maneuver_records("deepen")
    assert rows[0]["applied"] == 1 and rows[0]["delivered"] is None
