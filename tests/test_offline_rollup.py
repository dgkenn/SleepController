"""The stored night summary is staged from the WHOLE night (restage_night_offline + rollup_night),
with the recorded labels as the fallback and ``raw_samples`` left exactly as the controller wrote
them."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from sleepctl.config import AppConfig
from sleepctl.controller import state_estimator as se
from sleepctl.loop import night_rollup, restage
from sleepctl.ml.sleep_staging import offline
from sleepctl.ml.sleep_staging.infer import StageEstimate
from sleepctl.storage.repository import Repository

NIGHT = "2026-06-23"
BED = datetime(2026, 6, 23, 23, 0)
HOURS = 3.0
LATENCY_MIN = 15


@contextmanager
def _repo():
    tmp = tempfile.mkdtemp()
    repo = Repository(os.path.join(tmp, "sleepctl.db"))
    try:
        yield repo
    finally:
        repo.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _night(repo, *, recorded="light", pairs=True):
    """Raw ticks every 30 s in pairs sharing one 60 s Pod frame ``ts`` (naive local, as live),
    plus a dense HR stream every 5 s (aware UTC)."""
    repo.conn.execute(
        "CREATE TABLE IF NOT EXISTS sensor_samples (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, hr REAL, hrv REAL, movement REAL, source TEXT,"
        " hr_frozen INTEGER DEFAULT 0, not_worn INTEGER DEFAULT 0)")
    for i in range(int(HOURS * 120)):
        obs = BED + timedelta(seconds=30 * i)
        t = BED + timedelta(minutes=i // 2)
        if not pairs:                            # one tick per frame, observed at the frame
            if i % 2:
                continue
            obs = t
        early = i < LATENCY_MIN * 2
        repo.conn.execute(
            "INSERT INTO raw_samples (ts, night_date, stage, stage_confidence, heart_rate, hrv,"
            " respiratory_rate, movement, presence, commanded_level, controller_state,"
            " wake_event, sample_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.isoformat(), NIGHT, "awake" if early else recorded, 0.6, 60.0, 50.0, 14.0, None,
             1, -40, "induction" if early else "maintenance", 0, obs.isoformat()))
    utc0 = BED.astimezone().astimezone(timezone.utc) - timedelta(minutes=50)
    rows = []
    for k in range(int((HOURS * 60 + 50) * 12)):
        t = utc0 + timedelta(seconds=5 * k)
        rows.append((t.isoformat(), 60.0 + (k % 7) * 0.3))
    repo.conn.executemany("INSERT INTO sensor_samples (ts, hr) VALUES (?,?)", rows)
    repo.conn.commit()


def _raw(repo):
    return [tuple(r) for r in repo.conn.execute("SELECT * FROM raw_samples ORDER BY id")]


class _FakeStager:
    """Scripted emissions by clock: light, then a deep block whose first minutes are only weakly
    deep (the stretch a causal filter misses), then light again."""

    available = True
    hrv_available = False

    def __init__(self, hmm):
        self.hmm = hmm
        self.calls = []

    def predict(self, hr_samples, activity_samples=None, minutes_since_start=None,
                minutes_since_onset=None, *, smooth=True, ibi_samples=None, total_minutes=None):
        self.calls.append(dict(smooth=smooth, total_minutes=total_minutes,
                               minutes_since_start=minutes_since_start))
        m = minutes_since_start or 0.0
        if 60.0 <= m < 62.0:
            p = [0.05, 0.45, 0.45, 0.05]
        elif 62.0 <= m < 110.0:
            p = [0.02, 0.08, 0.88, 0.02]
        else:
            p = [0.05, 0.80, 0.10, 0.05]
        probs = dict(zip(("wake", "light", "deep", "rem"), p))
        lbl = max(probs, key=probs.get)
        return StageEstimate(stage_label=lbl, p_wake=p[0], confidence=probs[lbl], probs=probs,
                             smoothed=False, variant="hr")


HMM = {"trans": [[0.90, 0.08, 0.01, 0.01], [0.02, 0.94, 0.02, 0.02],
                 [0.01, 0.04, 0.95, 0.00], [0.01, 0.04, 0.00, 0.95]],
       "start": [0.9, 0.05, 0.03, 0.02], "prior": [0.1, 0.5, 0.2, 0.2],
       "emission_prior": [0.25] * 4, "temper": 0.35}


def _on():
    cfg = AppConfig.default()
    cfg.tunables.offline_night_staging = True
    return cfg


@pytest.fixture
def fake(monkeypatch):
    f = _FakeStager(HMM)
    monkeypatch.setattr(se, "_STAGER", f)
    monkeypatch.setattr(se, "_STAGER_LOADED", True)
    return f


def test_offline_restage_smooths_the_whole_night(fake):
    with _repo() as repo:
        _night(repo)
        before = _raw(repo)
        labels = restage.restage_night_offline(repo, NIGHT)
        assert _raw(repo) == before                       # the audit trail is untouched
    assert se._STAGER is fake                             # the live stager is back in place
    # every model call was answered UNSMOOTHED, with the night's real length as its clock span
    assert fake.calls and all(c["smooth"] is False for c in fake.calls)
    assert all(c["total_minutes"] == pytest.approx(HOURS * 60 - 0.5) for c in fake.calls)
    at = {datetime.fromisoformat(k): v for k, v in labels.items()}
    assert set(at.values()) <= {"awake", "light", "deep", "rem"}
    # the weakly-deep lead-in is deep once the minutes after it are known
    assert at[BED + timedelta(minutes=61)] == "deep"
    assert at[BED + timedelta(minutes=90)] == "deep"
    assert at[BED + timedelta(minutes=40)] == "light"
    assert at[BED + timedelta(minutes=150)] == "light"


def test_sparse_ticks_are_back_filled_onto_the_30_s_grid(fake):
    """Live, every tick rescored each 30 s step of its window, so a night ticked once a minute
    must still hand the smoother an emission per 30 s epoch, each from the history cut there."""
    with _repo() as repo:
        _night(repo, pairs=False)
        restage.restage_night_offline(repo, NIGHT)
    mss = sorted({round(c["minutes_since_start"], 2) for c in fake.calls})
    steps = {round(b - a, 2) for a, b in zip(mss, mss[1:])}
    assert steps == {0.5}
    assert len(mss) >= HOURS * 120 - 2


def test_offline_restage_keeps_the_hypnogram_constraints(fake, monkeypatch):
    """REM that the smoother would keep before onset is still reclassified, as live."""
    def rem_everywhere(times, ems, hmm, **_kw):
        return [{"stage": "rem", "probs": {"wake": 0.0, "light": 0.1, "deep": 0.0, "rem": 0.9}}
                for _ in times]
    monkeypatch.setattr(offline, "smooth_night", rem_everywhere)
    with _repo() as repo:
        _night(repo)
        labels = restage.restage_night_offline(repo, NIGHT)
    at = {datetime.fromisoformat(k): v for k, v in labels.items()}
    assert at[BED + timedelta(minutes=5)] == "light"       # before onset
    assert at[BED + timedelta(minutes=LATENCY_MIN + 10)] == "light"   # REM too early
    assert at[BED + timedelta(minutes=150)] == "rem"


def test_offline_restage_restores_the_stager_when_it_fails(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("smoother failed")
    monkeypatch.setattr(offline, "smooth_night", boom)
    with _repo() as repo:
        _night(repo)
        with pytest.raises(RuntimeError):
            restage.restage_night_offline(repo, NIGHT)
    assert se._STAGER is fake


def test_offline_restage_without_sensor_history_returns_nothing(fake):
    with _repo() as repo:
        _night(repo)
        repo.conn.execute("DELETE FROM sensor_samples")
        assert restage.restage_night_offline(repo, NIGHT) == {}


def test_rollup_uses_the_offline_labels(fake):
    with _repo() as repo:
        _night(repo)
        before = _raw(repo)
        recorded = night_rollup.reconstruct_night_summary(repo, NIGHT)
        ns = night_rollup.rollup_night(repo, NIGHT, _on())
        assert _raw(repo) == before
    assert recorded.deep_min == 0.0
    assert ns.deep_min and ns.deep_min >= 40.0
    assert ns.temp_profile_summary["staging"] == "offline"


def test_rollup_falls_back_to_the_recorded_labels_on_any_error(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("replay failed")
    monkeypatch.setattr(restage, "restage_night_offline", boom)
    with _repo() as repo:
        _night(repo, recorded="deep")
        want = night_rollup.reconstruct_night_summary(repo, NIGHT)
        ns = night_rollup.rollup_night(repo, NIGHT, _on())
    assert (ns.deep_min, ns.light_min, ns.total_sleep_min, ns.waso_min) == \
        (want.deep_min, want.light_min, want.total_sleep_min, want.waso_min)
    assert ns.temp_profile_summary["staging"] == "recorded"
    assert se._STAGER is fake


def test_rollup_falls_back_when_the_night_cannot_be_replayed(monkeypatch):
    monkeypatch.setattr(se, "_STAGER", None)
    monkeypatch.setattr(se, "_STAGER_LOADED", True)
    with _repo() as repo:
        _night(repo, recorded="deep")
        ns = night_rollup.rollup_night(repo, NIGHT, _on())
    assert ns.deep_min and ns.temp_profile_summary["staging"] == "recorded"


def test_rollup_is_todays_path_while_the_switch_is_off(fake, monkeypatch):
    def never(*a, **k):
        raise AssertionError("restaged with the switch off")
    monkeypatch.setattr(restage, "restage_night_offline", never)
    with _repo() as repo:
        _night(repo, recorded="deep")
        want = night_rollup.reconstruct_night_summary(repo, NIGHT)
        for cfg in (None, AppConfig.default()):
            ns = night_rollup.rollup_night(repo, NIGHT, cfg)
            assert ns.deep_min == want.deep_min and ns.rem_min == want.rem_min
            assert ns.temp_profile_summary == want.temp_profile_summary


def test_rollup_of_an_empty_night_is_still_a_bare_summary():
    with _repo() as repo:
        ns = night_rollup.rollup_night(repo, NIGHT, _on())
    assert ns.total_sleep_min is None and not ns.temp_profile_summary


def test_offline_restage_runs_on_the_bundled_stager():
    """End to end on the real weights: labels for the in-bed ticks, the singleton restored."""
    live = se._get_stager()
    if live is None:
        pytest.skip("no bundled stager")
    with _repo() as repo:
        _night(repo)
        labels = restage.restage_night_offline(repo, NIGHT)
    assert se._get_stager() is live
    assert len(labels) >= HOURS * 60 * 0.9           # one label per Pod frame ts
    assert set(labels.values()) <= {"awake", "light", "deep", "rem"}
