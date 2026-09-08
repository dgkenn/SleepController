"""A daemon restart mid-night must resume MAINTENANCE, not re-run the induction cascade."""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.session_recovery import recover_session_state
from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage
from sleepctl.storage.repository import Repository

NIGHT = "2026-09-07"
T0 = datetime(2026, 9, 7, 21, 41)


def _repo(tmp_path):
    return Repository(str(tmp_path / "r.db"), check_same_thread=False)


def _sample(repo, ts, state, stage=None):
    repo.conn.execute(
        "INSERT INTO raw_samples (ts, night_date, controller_state, stage) VALUES (?,?,?,?)",
        (ts.isoformat(), NIGHT, state, stage))


def _night(repo, until_min=120, last_state="maintenance"):
    t = T0
    for i in range(20):                       # 10 min of induction
        _sample(repo, t, "induction", "light"); t += timedelta(seconds=30)
    onset = t
    stages = ["light"] * 20 + ["deep"] * 20 + ["rem"] * 10
    i = 0
    while (t - T0).total_seconds() / 60.0 < until_min:
        _sample(repo, t, last_state, stages[i % len(stages)]); t += timedelta(seconds=30); i += 1
    repo.conn.commit()
    return onset, t


def test_a_fresh_maintenance_night_is_recovered_with_onset_and_architecture(tmp_path):
    repo = _repo(tmp_path)
    onset, last = _night(repo)
    rec = recover_session_state(repo.conn, NIGHT, last + timedelta(minutes=2))
    assert rec is not None
    assert rec["state"] == "maintenance" and rec["onset_ts"] == onset
    a = rec["architecture"]
    assert a["deep_min"] > 20 and a["rem_min"] > 10 and a["light_min"] > 20
    assert abs(a["deep_min"] + a["rem_min"] + a["light_min"] - 110) < 2


def test_a_stale_last_sample_is_a_previous_night_not_a_live_session(tmp_path):
    repo = _repo(tmp_path)
    _, last = _night(repo)
    assert recover_session_state(repo.conn, NIGHT, last + timedelta(hours=3)) is None


def test_a_night_still_in_induction_has_nothing_to_resume(tmp_path):
    repo = _repo(tmp_path)
    _night(repo, until_min=30, last_state="induction")
    now = T0 + timedelta(minutes=31)
    assert recover_session_state(repo.conn, NIGHT, now) is None


def _frame(ts, hr=68.0):
    return SensorFrame(timestamp=ts, stage=SleepStage.LIGHT, stage_confidence=0.6,
                       heart_rate=hr, presence=None, data_age_seconds=10)


def test_the_controller_resumes_maintenance_instead_of_re_inducing():
    c = SleepController(AppConfig.default())
    c.set_session("induce", keep_light=False)          # what the daemon's session restore does
    assert c.sm.state is ControllerState.INDUCTION
    onset = T0 + timedelta(minutes=10)
    c.restore_bed_entry(T0)
    c.restore_session_state("maintenance", onset, {"deep_min": 30.0, "rem_min": 12.0, "light_min": 40.0})
    assert c.sm.state is ControllerState.MAINTENANCE
    assert c.sleep_onset_time == onset
    assert c._arch_deep_min == 30.0
    now = T0 + timedelta(minutes=120)
    d = c.decide(_frame(now), ContextRecord(date=NIGHT), [], now)
    assert d.state is ControllerState.MAINTENANCE
    assert "onset_warm" not in d.reason


def test_restore_never_overrides_a_live_onset():
    c = SleepController(AppConfig.default())
    c._sleep_onset_time = T0 + timedelta(minutes=5)
    c.restore_session_state("maintenance", T0 + timedelta(minutes=30), None)
    assert c.sleep_onset_time == T0 + timedelta(minutes=5)


def test_restore_ignores_states_before_onset():
    c = SleepController(AppConfig.default())
    c.restore_session_state("induction", T0, None)
    assert c.sm.state is ControllerState.IDLE and c.sleep_onset_time is None
