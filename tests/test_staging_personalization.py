"""Marker-calibrated wake bias and personal stage transitions."""
import json
from datetime import datetime, timedelta

from sleepctl.learning.hypnogram_priors import learn_transitions
from sleepctl.learning.wake_truth import wake_truth_profile
from sleepctl.storage.repository import Repository


def _repo(tmp_path):
    return Repository(str(tmp_path / "p.db"), check_same_thread=False)


def _marker(repo, stage):
    repo.conn.execute("INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
                      (datetime.now().isoformat(), "sensor", "info", "marker_vs_stage", "m",
                       json.dumps({"stage_at_marker": stage})))


def test_wake_bias_learns_only_from_enough_markers_and_stays_bounded(tmp_path):
    repo = _repo(tmp_path)
    for _ in range(5):
        _marker(repo, "rem")
    p = wake_truth_profile(repo)
    assert p["personalized"] is False and p["bias"] == 1.0
    for _ in range(7):
        _marker(repo, "light")
    p = wake_truth_profile(repo)                      # 0 of 12 scored awake
    assert p["personalized"] is True and p["bias"] == 1.6 and p["agreement"] == 0.0
    repo.conn.execute("DELETE FROM events")
    for i in range(12):
        _marker(repo, "awake" if i < 11 else "light")   # 92% agreement: leave it alone
    p = wake_truth_profile(repo)
    assert abs(p["bias"] - (1.0 + 1.5 * (0.8 - 11 / 12))) < 1e-6


POP = {"trans": [[0.79, 0.19, 0.0, 0.02], [0.02, 0.95, 0.01, 0.02],
                 [0.01, 0.04, 0.95, 0.0], [0.01, 0.03, 0.0, 0.96]],
       "prior": [0.09, 0.56, 0.13, 0.22]}


def _night(repo, night, deep_heavy: bool):
    t = datetime(2026, 9, 1, 23, 0)
    stages = (["light"] * 20 + ["deep"] * 60 + ["light"] * 20 + ["rem"] * 20) if deep_heavy else \
             (["light"] * 60 + ["rem"] * 40 + ["light"] * 20)
    for i in range(360):
        st = stages[i % len(stages)]
        repo.conn.execute("INSERT INTO raw_samples (ts, night_date, controller_state, stage, stage_confidence) VALUES (?,?,?,?,?)",
                          ((t + timedelta(seconds=30 * i)).isoformat(), night, "maintenance", st, 0.65))
    repo.conn.commit()


def test_transitions_stay_population_until_five_nights_then_blend_toward_the_user(tmp_path):
    repo = _repo(tmp_path)
    for d in range(3):
        _night(repo, f"2026-09-0{d + 1}", deep_heavy=True)
    assert learn_transitions(repo, POP)["personalized"] is False
    for d in range(3, 8):
        _night(repo, f"2026-09-0{d + 1}", deep_heavy=True)
    out = learn_transitions(repo, POP)
    assert out["personalized"] is True and out["n_nights"] == 8
    assert out["prior"][2] > POP["prior"][2]                 # more deep than the population
    assert abs(sum(out["prior"]) - 1.0) < 1e-4
    assert all(abs(sum(r) - 1.0) < 1e-4 for r in out["trans"])
    assert out["trans"][2][2] > 0.9                          # deep still persistent


def test_the_stager_accepts_a_wake_bias_and_a_personal_hmm():
    from sleepctl.ml.sleep_staging.infer import SleepStager
    st = SleepStager.load()
    st.set_wake_bias(1.3)
    assert st.wake_bias == 1.3
    st.set_wake_bias(5.0)
    assert st.wake_bias == 2.0
    if st.hmm:
        st.set_personal_hmm(POP["trans"], POP["prior"])
        assert st.hmm["trans"][1][1] == 0.95
