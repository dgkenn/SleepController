"""Regression tests for the 2026-09-25 learning-loop audit.

One test (or a small group) per confirmed defect, each built from the replay that exposed it:
trials pooling ineligible nights, the comfort integrator forgetting, note-declared misses not
counted, declared intervals dropped/double-matched, restart-artifact onset latencies, an
auto-stopped arm blocking the verdict forever, HMM epochs deduped on the pod's frame time, the
ML feature table exporting a neutral that never ran, the lag learner reading the oldest rows,
and the response estimator keying interventions by calendar date."""
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from sleepctl.config import AppConfig, ThermalTrialConfig
from sleepctl.storage.repository import Repository

NOTES_DDL = ("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
             "date TEXT NOT NULL, text TEXT, created TEXT)")


@pytest.fixture
def repo(tmp_path):
    r = Repository(str(tmp_path / "audit.db"), check_same_thread=False)
    r.conn.execute(NOTES_DDL)
    yield r
    r.close()


def _dates(n, start=date(2026, 7, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------- 1. trials: randomized nights only


def test_thermal_analysis_and_auto_stop_ignore_ineligible_forced_control_nights(repo):
    from sleepctl.ml.thermal_trial import _auto_stopped_arms, analyze_dose_response
    cfg = ThermalTrialConfig(enabled=True, auto_stop_min_n=6, auto_stop_threshold=1.0,
                             offset_ladder_f=[0.0, 0.8], min_nights_before_verdict=6)
    days = iter(_dates(60))
    for w in [2] * 8:                                   # randomized control nights
        d = next(days); repo.assign_thermal_trial_night(d, "+0.00", 0.0, True)
        repo.record_thermal_trial_outcome(d, wake_events=w)
    for w in [2, 2, 3, 2, 2, 3]:                        # randomized +0.80 nights, ~same
        d = next(days); repo.assign_thermal_trial_night(d, "+0.80", 0.8, True)
        repo.record_thermal_trial_outcome(d, wake_events=w)
    for _ in range(20):                                 # forced-control ineligible nights
        d = next(days); repo.assign_thermal_trial_night(d, "+0.00", 0.0, False)
        repo.record_thermal_trial_outcome(d, wake_events=0)
    # Pooled, control reads ~0.57 and +0.80 (2.33) would be auto-stopped as >= 1.0 worse.
    assert _auto_stopped_arms(repo, cfg) == set()
    out = analyze_dose_response(repo.thermal_trial_rows(resolved_only=True), cfg=cfg)
    assert out["arms"]["+0.00"]["n"] == 8
    assert out["arms"]["+0.00"]["mean_wake_events"] == 2.0
    assert out["n_excluded_ineligible"] == 20


def test_efficacy_analysis_and_auto_stop_ignore_ineligible_forced_active_nights(repo):
    from sleepctl.config import EfficacyTrialConfig
    from sleepctl.ml.efficacy_trial import _auto_stop_triggered, analyze_trials
    days = iter(_dates(60))
    for arm, eligible, w, k in (("active", True, 2, 10), ("sham", True, 2, 10),
                                ("active", False, 0, 30)):
        for _ in range(k):
            d = next(days); repo.assign_efficacy_trial_night(d, arm, eligible)
            repo.record_efficacy_trial_outcome(d, wake_events=w)
    # Pooled, active reads 0.5 and sham 2.0: auto-stop would fire and the verdict would say
    # the controller saves 1.5 wakes/night -- all of it night type.
    assert _auto_stop_triggered(repo, EfficacyTrialConfig()) is False
    out = analyze_trials(repo.efficacy_trial_rows(resolved_only=True))
    assert out["n_active"] == 10 and out["n_sham"] == 10
    assert out["wake_events"]["diff"] == 0.0
    assert out["n_excluded_ineligible"] == 30


def test_hand_built_rows_without_an_eligible_key_are_still_analyzed():
    from sleepctl.ml.efficacy_trial import analyze_trials
    from sleepctl.ml.thermal_trial import analyze_dose_response
    assert analyze_dose_response([{"arm": "+0.00", "wake_events": 1}])["arms"]["+0.00"]["n"] == 1
    assert analyze_trials([{"arm": "sham", "wake_events": 1}])["n_sham"] == 1


# ---------------------------------------------------------------- 2. comfort: no forgetting, no spill


def _comfort_repo(reviews=(), notes=()):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE wake_review (night_date TEXT PRIMARY KEY, ts TEXT, rested INTEGER,"
                 " temperature TEXT, onset_feel TEXT, note TEXT, verdicts TEXT)")
    conn.execute(NOTES_DDL)
    for night, temp in reviews:
        conn.execute("INSERT INTO wake_review (night_date, ts, temperature) VALUES (?, ?, ?)",
                     (night, night + "T07:00:00", temp))
    for d, text in notes:
        conn.execute("INSERT INTO notes (date, text) VALUES (?, ?)", (d, text))
    return SimpleNamespace(conn=conn)


def test_earned_warmth_is_not_forgotten_after_sixty_nights():
    from sleepctl.learning.comfort_feedback import comfort_anchor
    nights = _dates(90, start=date(2026, 9, 22))
    repo = _comfort_repo(reviews=[(n, "too_cold" if i < 3 else "right")
                                  for i, n in enumerate(nights)])
    cfg = AppConfig.default()
    early = comfort_anchor(repo, cfg, 69.0, now=datetime(2026, 10, 15, 20))
    late = comfort_anchor(repo, cfg, 69.0, now=datetime(2026, 12, 25, 20))
    assert early["neutral_f"] == late["neutral_f"] == 73.0      # was 70.0 once day 1 aged out
    assert late["feedback_f"] == 3.0


def test_two_cold_notes_one_morning_are_one_vote_on_the_night_before():
    from sleepctl.learning.comfort_feedback import comfort_anchor
    repo = _comfort_repo(notes=[("2026-09-24", "woke up cold"), ("2026-09-24", "still freezing")])
    cfg = AppConfig.default()
    for now in (datetime(2026, 9, 24, 20), datetime(2026, 9, 26, 20)):
        a = comfort_anchor(repo, cfg, 69.0, now=now)
        assert a["n_notes"] == 1 and a["feedback_f"] == 0.25
        assert a["last_vote"]["night_date"] == "2026-09-23"


def test_a_note_never_credits_a_night_that_has_not_been_slept():
    from sleepctl.learning.comfort_feedback import comfort_anchor
    cfg = AppConfig.default()
    # The night 2026-09-23 is still the current night until noon on the 24th.
    repo = _comfort_repo(notes=[("2026-09-24", "so cold")])
    assert comfort_anchor(repo, cfg, 69.0, now=datetime(2026, 9, 24, 8))["n_notes"] == 0
    assert comfort_anchor(repo, cfg, 69.0, now=datetime(2026, 9, 24, 13))["n_notes"] == 1


# ---------------------------------------------------------------- 3. wake truth: note misses count


def _light_nights(repo, n_nights, start=date(2026, 9, 1)):
    for night in _dates(n_nights, start=start):
        t0 = datetime.fromisoformat(night) + timedelta(days=1, hours=1, minutes=50)
        for k in range(60):
            repo.conn.execute("INSERT INTO raw_samples (ts, night_date, stage) VALUES (?,?,?)",
                              ((t0 + timedelta(seconds=30 * k)).isoformat(), night, "light"))
        morning = (datetime.fromisoformat(night) + timedelta(days=1)).date().isoformat()
        repo.conn.execute("INSERT INTO notes (date, text, created) VALUES (?,?,?)",
                          (morning, "awake 2:00-2:10", "x"))
    repo.conn.commit()


def test_note_declared_awakenings_scored_asleep_are_misses(repo):
    from sleepctl.learning.wake_truth import wake_truth_profile
    _light_nights(repo, 12)
    p = wake_truth_profile(repo)
    assert p["n_notes"] == 12 and p["agreement"] == 0.0
    assert p["miss_rate"] == 1.0            # was 0.0: the notes only diluted n
    assert p["bias"] > 1.0                  # every awakening missed -> see MORE wake, not less


# ---------------------------------------------------------------- 4. declared: every interval, one night


def test_every_interval_in_a_note_counts_under_exactly_one_night(repo):
    from sleepctl.learning.declared_awakenings import declared_instants
    # Samples cover the night of 09-20 (00:00-08:00 on the 21st) AND the night of 09-21 at the
    # same clock times, so the note could match under either candidate night.
    for night, t0 in (("2026-09-20", datetime(2026, 9, 21)), ("2026-09-21", datetime(2026, 9, 22))):
        for k in range(8 * 120):
            repo.conn.execute("INSERT INTO raw_samples (ts, night_date, stage) VALUES (?,?,?)",
                              ((t0 + timedelta(seconds=30 * k)).isoformat(), night, "awake"))
    repo.conn.execute("INSERT INTO notes (date, text, created) VALUES (?,?,?)",
                      ("2026-09-21", "awake 0:15-0:25, woke 3:10, awake 5:00-5:20", "x"))
    repo.conn.commit()
    inst = declared_instants(repo)
    assert [t for t, _ in inst] == ["2026-09-21T00:20:00", "2026-09-21T03:12:30",
                                    "2026-09-21T05:10:00"]


# ---------------------------------------------------------------- 5. onset: restart artifacts ignored


def test_onset_learners_ignore_restart_artifact_latencies():
    from sleepctl.learning.onset_tuning import learn_cold_settle, learn_onset
    warm = ([{"onset_warm_f": 2.5, "onset_latency_min": 1.0}] * 4
            + [{"onset_warm_f": 1.0, "onset_latency_min": 20.0}] * 6
            + [{"onset_warm_f": 0.0, "onset_latency_min": 30.0}] * 4)
    m = learn_onset(warm, base_f=1.0)
    assert m.n == 10 and m.onset_warm_f == 1.0      # was pulled toward 2.5 by the 1-min nights
    cold = ([{"onset_cold_settle_f": 56.0, "onset_latency_min": 0.0}] * 4
            + [{"onset_cold_settle_f": 60.0, "onset_latency_min": 20.0}] * 6
            + [{"onset_cold_settle_f": 64.0, "onset_latency_min": 30.0}] * 4)
    c = learn_cold_settle(cold, base_f=60.0)
    assert c.n == 10 and c.onset_cold_settle_f == 60.0


# ---------------------------------------------------------------- 6. dose-response: frozen arms


def test_an_auto_stopped_arm_does_not_block_the_verdict_forever():
    from sleepctl.ml.thermal_trial import analyze_dose_response
    rows = ([{"arm": "+0.00", "wake_events": 1}] * 30
            + [{"arm": "+2.00", "wake_events": 3}] * 6          # auto-stopped at n=6
            + [{"arm": a, "wake_events": 0} for a in ("+0.50", "+1.00", "+1.50") for _ in range(20)])
    out = analyze_dose_response(rows, cfg=AppConfig.default().thermal_trial)
    assert out["arms"]["+2.00"]["auto_stopped"] is True
    assert out["confident"] is True
    assert "Not enough data" not in out["verdict"]


def test_an_arm_retired_from_the_ladder_does_not_block_the_verdict():
    from sleepctl.ml.thermal_trial import analyze_dose_response
    rows = ([{"arm": "+0.00", "wake_events": 2}] * 10 + [{"arm": "+0.50", "wake_events": 1}] * 10
            + [{"arm": "-0.75", "wake_events": 2}] * 3)          # pre-2026-09-21 ladder arm
    out = analyze_dose_response(rows, cfg=AppConfig.default().thermal_trial)
    assert out["arms"]["-0.75"]["off_ladder"] is True
    assert out["confident"] is True
    # an arm that IS still on the ladder and short still holds it back
    rows += [{"arm": "+1.00", "wake_events": 1}] * 3
    assert analyze_dose_response(rows, cfg=AppConfig.default().thermal_trial)["confident"] is False


# ---------------------------------------------------------------- 7. HMM priors: dedupe on tick time


def test_transition_learner_keeps_every_epoch_when_frame_time_lags(repo):
    from sleepctl.learning.hypnogram_priors import learn_transitions
    pop = {"trans": [[0.25] * 4 for _ in range(4)], "prior": [0.25] * 4}
    seq = ["light", "deep", "rem"]
    for n in range(5):
        night = f"2026-09-{10 + n:02d}"
        t0 = datetime(2026, 9, 10 + n, 23, 0)
        for k in range(960):
            # the pod's frame time refreshes every 60 s: two consecutive ticks share a ts
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, sample_ts, night_date, stage, stage_confidence, "
                "controller_state) VALUES (?,?,?,?,?,?)",
                ((t0 + timedelta(seconds=60 * (k // 2))).isoformat(),
                 (t0 + timedelta(seconds=30 * k)).isoformat(), night, seq[(k // 4) % 3], 0.9,
                 "maintenance"))
    repo.conn.commit()
    out = learn_transitions(repo, pop, min_nights=5)
    assert out["n_epochs"] == 4800                     # was 2400
    # true P(light->light) is 0.75; blended toward a flat 0.25 prior it reads ~0.63 (0.45 when
    # every other epoch was dropped and the light runs looked half as long)
    assert out["trans"][1][1] > 0.6


def test_transition_learner_rows_fall_back_to_ts_without_sample_ts():
    from sleepctl.learning.hypnogram_priors import _night_rows
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE raw_samples (ts TEXT, night_date TEXT, stage TEXT, "
                 "stage_confidence REAL, controller_state TEXT)")
    conn.execute("INSERT INTO raw_samples VALUES ('2026-09-10T23:00:00','2026-09-10','light',0.9,"
                 "'maintenance')")
    rows = _night_rows(SimpleNamespace(conn=conn), "2026-09-10")
    assert [tuple(r) for r in rows] == [("2026-09-10T23:00:00", "light", 0.9)]


# ---------------------------------------------------------------- 8. ML features: the neutral that ran


def test_feature_neutral_is_the_applied_neutral_not_the_stored_version(repo):
    from sleepctl.ml.dataset import build_feature_rows
    from sleepctl.models import NightSummary
    sp = AppConfig.default().default_setpoints()
    repo.save_setpoints(sp)
    offsets = {"2026-09-26": 2.0, "2026-09-27": 1.0, "2026-09-28": 1.5}
    for d, off in offsets.items():
        repo.assign_thermal_trial_night(d, f"{off:+.2f}", off, True)
        repo.save_night_summary(NightSummary(date=d, setpoint_version=sp.version, wake_events=1))
    # 2026-09-28 also has a logged night of NEUTRAL maintenance decisions: that wins
    for i in range(12):
        repo.conn.execute(
            "INSERT INTO decisions (ts, night_date, state, thermal_intent, target_temp_f) "
            "VALUES (?,?,?,?,?)", (f"2026-09-29T0{i % 9}:00:00", "2026-09-28", "maintenance",
                                   "neutral", 73.0 if i % 2 else 74.0))
    repo.conn.commit()
    got = {r.date: r.neutral_f for r in build_feature_rows(repo)}
    assert got["2026-09-26"] == pytest.approx(sp.neutral_f + 2.0)
    assert got["2026-09-27"] == pytest.approx(sp.neutral_f + 1.0)
    assert got["2026-09-28"] == pytest.approx(73.5)


# ---------------------------------------------------------------- 9. lag: newest rows, one night


def _lag_block(repo, t, lag_ticks, n_blocks, night=None):
    for _ in range(n_blocks):
        for i in range(40):
            lvl = -10 if i < 20 else -6
            bed = 79.0 if (lag_ticks <= i < 20) else 80.0
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, night_date, stage, bed_temp_f, commanded_level) "
                "VALUES (?,?,?,?,?)", (t.isoformat(), night or t.date().isoformat(), "light", bed, lvl))
            t += timedelta(seconds=30)
    return t


def test_response_lag_is_learned_from_the_newest_rows(repo):
    from sleepctl.learning.lead_time import learn_response_lag
    t = _lag_block(repo, datetime(2026, 8, 1, 23), 4, 40, night="2026-08-01")    # old: 2 min
    _lag_block(repo, t, 16, 20, night="2026-08-02")                              # new: 8 min
    repo.conn.commit()
    assert learn_response_lag(repo, lookback=800) == 8.0                         # was 2.0


def test_response_lag_does_not_pair_across_a_night_boundary(repo):
    from sleepctl.learning.lead_time import learn_response_lag
    t = datetime(2026, 8, 1, 23)
    for n, night in enumerate(_dates(4, start=date(2026, 8, 1))):
        # each night runs at one level (no command inside it); the next night starts 4 levels
        # colder and its bed reads 1 F lower two ticks in -- a boundary, not a response
        for i in range(20):
            repo.conn.execute(
                "INSERT INTO raw_samples (ts, night_date, stage, bed_temp_f, commanded_level) "
                "VALUES (?,?,?,?,?)", (t.isoformat(), night, "light",
                                       80.0 if i < 2 else 79.0, -4 * n))
            t += timedelta(seconds=30)
    repo.conn.commit()
    assert learn_response_lag(repo) is None


# ---------------------------------------------------------------- 10. response: noon-cutoff night


def test_post_midnight_cooling_is_credited_to_the_night_it_happened_in():
    from sleepctl.learning.response import ResponseEstimator, _night_date
    from sleepctl.models import ControllerState, CorrectionAction, Intervention, NightSummary

    def iv(ts):
        return Intervention(timestamp=ts, state=ControllerState.MAINTENANCE,
                            action=CorrectionAction.COOLER, magnitude_f=1.0, reason="t")

    assert _night_date(iv(datetime(2026, 9, 21, 2, 0))) == "2026-09-20"
    assert _night_date(iv(datetime(2026, 9, 21, 22, 0))) == "2026-09-21"
    assert _night_date(SimpleNamespace(night_date="2026-09-19",
                                       timestamp=datetime(2026, 9, 21, 2))) == "2026-09-19"
    # Nights 09-04..09-06 were cooled at 02:00 the following morning; 09-01..09-03 were not.
    history = [NightSummary(date=d, wake_events=(1 if d >= "2026-09-04" else 4))
               for d in _dates(6, start=date(2026, 9, 1))]
    ivs = [iv(datetime(2026, 9, day, 2, 0)) for day in (5, 6, 7)]
    r = ResponseEstimator().estimate(history, ivs)["cooling_vs_wake_events"]
    assert r["n"] == 3 and r["confidence"] > 0           # calendar keying left n=2, effect 0
    assert r["effect_size"] < 0
