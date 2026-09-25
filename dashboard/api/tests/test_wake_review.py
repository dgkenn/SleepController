"""The "I'm awake" flow: end the session, then say what the night was actually like.

Two things no sensor supplies, collected while the sleeper is certainly awake and looking at
the app: how the night FELT, and a verdict on each awakening the detector believes it found.
A denial is the only false-alarm evidence the system has ever had -- a marker gesture, by
construction, can never say "you imagined that one".
"""
import json
from datetime import datetime, timedelta

import pytest

from app import wake_review


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    r = Repository(str(tmp_path / "wr.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    return r


NIGHT = "2026-09-21"
T0 = datetime(2026, 9, 21, 23, 0)


def _seed(repo, wakes=((60, 3), (200, 2)), stage_at_wake="awake"):
    """A night of light sleep with wake_event runs at the given (minute, n_ticks) offsets."""
    wake_min = {}
    for start, n in wakes:
        for k in range(n):
            wake_min[start + k] = True
    for i in range(300):
        ts = (T0 + timedelta(minutes=i)).isoformat()
        is_wake = wake_min.get(i, False)
        repo.conn.execute(
            "INSERT INTO raw_samples (ts, night_date, stage, heart_rate, controller_state, wake_event) "
            "VALUES (?,?,?,?,?,?)",
            (ts, NIGHT, stage_at_wake if is_wake else "light", 62.0, "maintenance",
             1 if is_wake else 0))
    repo.conn.commit()


def test_suspected_awakenings_are_clustered_into_episodes(repo):
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    assert len(eps) == 2
    assert eps[0]["ts"] == (T0 + timedelta(minutes=60)).isoformat()
    assert eps[0]["n_ticks"] == 3 and eps[1]["n_ticks"] == 2
    assert eps[0]["ts"] < eps[1]["ts"]                  # presented in the order they happened


def test_a_run_of_ticks_is_one_awakening_not_several(repo):
    _seed(repo, wakes=((60, 8),))
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    assert len(eps) == 1 and eps[0]["n_ticks"] == 8 and eps[0]["minutes"] == 7.0


def test_the_list_is_capped_so_it_stays_answerable(repo):
    _seed(repo, wakes=tuple((10 + 20 * k, 1) for k in range(14)))
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    assert len(eps) == wake_review.MAX_EPISODES


def test_an_awakening_the_bed_answered_is_asked_about_even_without_a_vote(repo):
    """The voter no longer logs a lone turn in bed, but a sustained awakening the controller
    answered (WAKE_RECOVERY) is exactly the one whose verdict the learners need."""
    _seed(repo, wakes=())
    for k in range(4):
        repo.conn.execute("UPDATE raw_samples SET controller_state = 'wake_recovery' "
                          "WHERE ts = ?", ((T0 + timedelta(minutes=120 + k)).isoformat(),))
    repo.conn.commit()
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    assert len(eps) == 1 and eps[0]["n_ticks"] == 4


def test_a_quiet_night_asks_nothing(repo):
    _seed(repo, wakes=())
    assert wake_review.suspected_awakenings(repo, NIGHT) == []


def test_the_review_round_trips_and_rejects_nonsense(repo):
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    out = wake_review.save_review(repo, {
        "night_date": NIGHT, "rested": 4, "temperature": "too_cold", "onset_feel": "slow",
        "note": "cold again around 1am",
        "verdicts": [{"ts": eps[0]["ts"], "verdict": "yes"},
                     {"ts": eps[1]["ts"], "verdict": "no"}]})
    assert out["ok"] and out["verdicts"] == 2
    got = wake_review.get_review(repo, NIGHT)
    assert got["rested"] == 4 and got["temperature"] == "too_cold" and got["onset_feel"] == "slow"
    assert got["note"].startswith("cold again")
    assert [v["verdict"] for v in got["verdicts"]] == ["yes", "no"]

    wake_review.save_review(repo, {"night_date": NIGHT, "rested": 99, "temperature": "arctic",
                                   "onset_feel": "vibes",
                                   "verdicts": [{"ts": eps[0]["ts"], "verdict": "maybe"}]})
    got = wake_review.get_review(repo, NIGHT)
    assert got["rested"] is None and got["temperature"] is None and got["onset_feel"] is None
    assert got["verdicts"] == []


def test_a_confirmed_awakening_becomes_a_declared_instant_the_learner_reads(repo):
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    wake_review.save_review(repo, {"night_date": NIGHT,
                                   "verdicts": [{"ts": e["ts"], "verdict": "yes"} for e in eps]})
    rows = repo.conn.execute(
        "SELECT data FROM events WHERE code='marker_vs_stage' ORDER BY id").fetchall()
    assert len(rows) == 2
    for r in rows:
        d = json.loads(r[0])
        assert d["kind"] == "review" and d["declared_awake"] is True
        assert d["stage_at_marker"] == "awake"


def test_a_denied_awakening_is_recorded_as_declared_asleep(repo):
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    wake_review.save_review(repo, {"night_date": NIGHT,
                                   "verdicts": [{"ts": eps[0]["ts"], "verdict": "no"},
                                                {"ts": eps[1]["ts"], "verdict": "unsure"}]})
    rows = repo.conn.execute("SELECT data FROM events WHERE code='marker_vs_stage'").fetchall()
    assert len(rows) == 1, "an 'unsure' must not become a label"
    assert json.loads(rows[0][0])["declared_awake"] is False


def test_denials_pull_the_wake_bias_down_where_markers_only_ever_pushed_it_up(repo):
    """The asymmetry that mattered: a marker gesture can only say the detector MISSED one."""
    from sleepctl.learning.wake_truth import wake_truth_profile
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    # 12 awakenings the detector called and the sleeper denies -- it is crying wolf
    for k in range(12):
        ts = (T0 + timedelta(minutes=60 + k)).isoformat()
        repo.conn.execute(
            "INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
            (ts, "sensor", "info", "marker_vs_stage", "denied",
             json.dumps({"stage_at_marker": "awake", "kind": "review", "declared_awake": False})))
    # ...and 12 real ones it caught
    for k in range(12):
        ts = (T0 + timedelta(minutes=200 + k)).isoformat()
        repo.conn.execute(
            "INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
            (ts, "sensor", "info", "marker_vs_stage", "confirmed",
             json.dumps({"stage_at_marker": "awake", "kind": "review", "declared_awake": True})))
    repo.conn.commit()
    p = wake_truth_profile(repo)
    assert p["personalized"] is True and p["n_denied"] == 12
    assert p["false_alarm_rate"] == 1.0 and p["miss_rate"] == 0.0
    assert p["bias"] < 1.0, "denied awakenings must pull the wake threshold back"
    assert "denied" in p["rationale"]


def test_markers_alone_behave_exactly_as_before(repo):
    from sleepctl.learning.wake_truth import wake_truth_profile
    for k in range(12):
        repo.conn.execute(
            "INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
            ((T0 + timedelta(minutes=k)).isoformat(), "sensor", "info", "marker_vs_stage", "m",
             json.dumps({"stage_at_marker": "light"})))
    repo.conn.commit()
    p = wake_truth_profile(repo)
    assert p["n_denied"] == 0 and p["agreement"] == 0.0
    assert p["bias"] == 1.6      # every declared awakening missed: push the threshold up hard


def test_the_payload_carries_the_night_its_awakenings_and_any_existing_review(repo):
    _seed(repo)
    p = wake_review.review_payload(repo, NIGHT)
    assert p["night_date"] == NIGHT and len(p["awakenings"]) == 2 and p["review"] is None
    wake_review.save_review(repo, {"night_date": NIGHT, "rested": 3})
    assert wake_review.review_payload(repo, NIGHT)["review"]["rested"] == 3


def test_the_night_a_review_belongs_to_uses_the_noon_cutoff():
    assert wake_review.night_date_for(datetime(2026, 9, 22, 6, 0)) == "2026-09-21"
    assert wake_review.night_date_for(datetime(2026, 9, 21, 23, 0)) == "2026-09-21"
    assert wake_review.night_date_for(datetime(2026, 9, 21, 13, 0)) == "2026-09-21"


def test_a_broken_database_never_raises(repo):
    repo.conn.execute("DROP TABLE raw_samples")
    assert wake_review.suspected_awakenings(repo, NIGHT) == []
    repo.conn.execute("DROP TABLE wake_review")
    assert wake_review.get_review(repo, NIGHT) is None


# ------------------------------------------------------------------ the endpoints
def test_the_wake_button_ends_the_session_and_hands_back_the_review(auth_client):
    r = auth_client.post("/tonight/wake-up")
    assert r.status_code == 200
    body = r.json()
    assert "night_date" in body and "awakenings" in body and "review" in body
    # "woke_up": ends the session AND starts the morning light dose
    assert body["command"]["queued"] == "woke_up"


def test_the_review_endpoints_round_trip(auth_client):
    r = auth_client.get("/tonight/wake-review")
    assert r.status_code == 200
    night = r.json()["night_date"]
    r = auth_client.post("/tonight/wake-review", json={
        "night_date": night, "rested": 2, "temperature": "too_cold", "onset_feel": "slow",
        "verdicts": [{"ts": "2026-09-21T23:30:00", "verdict": "unsure"}]})
    assert r.status_code == 200 and r.json()["ok"] is True
    got = auth_client.get(f"/tonight/wake-review?date={night}").json()
    assert got["review"]["rested"] == 2 and got["review"]["temperature"] == "too_cold"


def test_the_review_endpoints_need_auth():
    """A fresh client: the shared one in conftest is logged in by the auth_client fixture."""
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get("/tonight/wake-review").status_code in (401, 403)
    assert anon.post("/tonight/wake-up").status_code in (401, 403)
    assert anon.post("/tonight/wake-review", json={}).status_code in (401, 403)


# ---- re-saving a review replaces its declared instants; it does not multiply them ----------
def _review_events(repo):
    return [json.loads(r[0]) for r in repo.conn.execute(
        "SELECT data FROM events WHERE code='marker_vs_stage' ORDER BY id").fetchall()]


def test_saving_the_same_review_again_does_not_multiply_its_verdicts(repo):
    """Every save used to insert one event per verdict, so 12 saves of one confirmed awakening
    read to wake_truth as 12 independent confirmations (n=12, bias pinned at the maximum)."""
    from sleepctl.learning.wake_truth import wake_truth_profile
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    body = {"night_date": NIGHT, "rested": 3,
            "verdicts": [{"ts": eps[0]["ts"], "verdict": "yes"},
                         {"ts": eps[1]["ts"], "verdict": "no"}]}
    for _ in range(12):
        wake_review.save_review(repo, body)
    evs = _review_events(repo)
    assert len(evs) == 2
    assert all(e["night_date"] == NIGHT for e in evs)
    p = wake_truth_profile(repo)
    assert p["n"] == 1 and p["n_denied"] == 1


def test_changing_or_dropping_a_verdict_replaces_the_old_instant(repo):
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    wake_review.save_review(repo, {"night_date": NIGHT, "verdicts": [
        {"ts": eps[0]["ts"], "verdict": "yes"}, {"ts": eps[1]["ts"], "verdict": "yes"}]})
    wake_review.save_review(repo, {"night_date": NIGHT, "verdicts": [
        {"ts": eps[0]["ts"], "verdict": "no"}]})
    evs = _review_events(repo)
    assert len(evs) == 1 and evs[0]["declared_awake"] is False


def test_a_resave_leaves_gestures_and_other_nights_and_cleans_up_legacy_duplicates(repo):
    _seed(repo)
    eps = wake_review.suspected_awakenings(repo, NIGHT)
    ins = ("INSERT INTO events (ts, category, severity, code, message, data) "
           "VALUES (?,?,?,?,?,?)")
    # a marker gesture during the night, another night's review, and three copies of this
    # night's verdict written before events carried a night_date
    repo.conn.execute(ins, (eps[0]["ts"], "sensor", "info", "marker_vs_stage", "gesture",
                            json.dumps({"stage_at_marker": "light", "kind": "tap"})))
    repo.conn.execute(ins, ("2026-09-20T02:00:00", "sensor", "info", "marker_vs_stage", "r",
                            json.dumps({"stage_at_marker": "light", "kind": "review",
                                        "declared_awake": True, "night_date": "2026-09-19"})))
    for _ in range(3):
        repo.conn.execute(ins, (eps[0]["ts"], "sensor", "info", "marker_vs_stage", "old",
                                json.dumps({"stage_at_marker": "awake", "kind": "review",
                                            "declared_awake": True})))
    repo.conn.commit()
    wake_review.save_review(repo, {"night_date": NIGHT, "verdicts": [
        {"ts": eps[0]["ts"], "verdict": "yes"}]})
    evs = _review_events(repo)
    kinds = sorted((e.get("kind"), e.get("night_date") or "") for e in evs)
    assert kinds == [("review", "2026-09-19"), ("review", NIGHT), ("tap", "")]


# ---- the night a review belongs to follows the session, not the clock ----------------------
def _session(repo, start, end, state="maintenance", wake_at=None, step_min=5):
    t = start
    while t <= end:
        nd = (t - timedelta(hours=12)).date().isoformat()   # how the controller labels ticks
        is_wake = wake_at is not None and t == wake_at
        repo.conn.execute(
            "INSERT INTO raw_samples (ts, night_date, stage, controller_state, wake_event) "
            "VALUES (?,?,?,?,?)",
            (t.isoformat(), nd, "awake" if is_wake else "light", state, 1 if is_wake else 0))
        t += timedelta(minutes=step_min)
    repo.conn.commit()


def test_a_day_sleeper_waking_after_noon_reviews_the_sleep_they_just_had(repo):
    """Post-night-shift sleep 04:00-12:05: the controller files the tail after noon under the
    next date, and tapping "I'm awake" at 12:10 opened an empty review for a night that had
    not happened yet -- the 09:00 awakening was never asked about."""
    _session(repo, datetime(2026, 7, 2, 4, 0), datetime(2026, 7, 2, 12, 5),
             wake_at=datetime(2026, 7, 2, 9, 0))
    now = datetime(2026, 7, 2, 12, 10)
    assert wake_review.night_date_for(now, repo) == "2026-07-01"
    p = wake_review.review_payload(repo, now=now)
    assert p["night_date"] == "2026-07-01"
    assert [a["ts"] for a in p["awakenings"]] == ["2026-07-02T09:00:00"]
    out = wake_review.save_review(repo, {"rested": 2}, now=now)
    assert out["night_date"] == "2026-07-01"


def test_an_idle_stretch_separates_last_night_from_this_afternoons_nap(repo):
    _session(repo, datetime(2026, 9, 21, 23, 0), datetime(2026, 9, 22, 7, 0))
    _session(repo, datetime(2026, 9, 22, 7, 5), datetime(2026, 9, 22, 13, 55), state="idle")
    _session(repo, datetime(2026, 9, 22, 14, 0), datetime(2026, 9, 22, 15, 0))
    assert wake_review.night_date_for(datetime(2026, 9, 22, 15, 5), repo) == "2026-09-22"


def test_a_normal_morning_and_no_recent_session_keep_the_noon_cutoff(repo):
    _session(repo, datetime(2026, 9, 21, 23, 0), datetime(2026, 9, 22, 7, 0))
    assert wake_review.night_date_for(datetime(2026, 9, 22, 7, 10), repo) == "2026-09-21"
    assert wake_review.night_date_for(datetime(2026, 9, 24, 6, 0), repo) == "2026-09-23"
    assert wake_review.night_date_for(datetime(2026, 9, 24, 13, 0), repo) == "2026-09-24"


def test_the_review_feeds_the_check_in_the_trial_and_the_note_parsers(repo):
    repo.conn.execute("INSERT INTO thermal_trials (night_date, arm, offset_f) "
                      "VALUES (?, '+0.50', 0.5)", (NIGHT,))
    repo.conn.commit()
    payload = {"night_date": NIGHT, "rested": 4, "temperature": "right",
               "note": "woke 3:10, a bit cold"}
    wake_review.save_review(repo, payload)
    wake_review.save_review(repo, payload)                      # a re-save does not stack
    assert repo.get_context(NIGHT).subjective_quality == 4.0
    row = repo.conn.execute("SELECT subjective_rating FROM thermal_trials WHERE night_date=?",
                            (NIGHT,)).fetchone()
    assert row[0] == 4.0
    notes = repo.conn.execute("SELECT date, text FROM notes").fetchall()
    assert len(notes) == 1 and notes[0][0] == "2026-09-22" and "woke 3:10" in notes[0][1]
