"""The comfort anchor: the sweep's neutral, re-anchored warmer and steered by the morning review."""
import sqlite3
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from sleepctl.config import AppConfig
from sleepctl.learning.comfort_feedback import comfort_anchor, note_vote, shifted_profile

NOW = datetime(2026, 10, 5, 20, 0)


def _repo(reviews=(), notes=()):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE wake_review (night_date TEXT PRIMARY KEY, ts TEXT, rested INTEGER,"
                 " temperature TEXT, onset_feel TEXT, note TEXT, verdicts TEXT)")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, text TEXT,"
                 " created TEXT)")
    for night, temp in reviews:
        conn.execute("INSERT INTO wake_review (night_date, ts, temperature) VALUES (?, ?, ?)",
                     (night, night + "T07:00:00", temp))
    for date, text in notes:
        conn.execute("INSERT INTO notes (date, text) VALUES (?, ?)", (date, text))
    return SimpleNamespace(conn=conn)


def _cfg(**kw):
    cfg = AppConfig.default()
    return replace(cfg, tunables=replace(cfg.tunables, **kw)) if kw else cfg


def test_default_reanchors_one_degree_above_the_sweep():
    a = comfort_anchor(_repo(), _cfg(), 69.0, now=NOW)
    assert a["base_offset_f"] == 1.0
    assert a["neutral_f"] == 70.0
    assert a["feedback_f"] == 0.0 and a["last_vote"] is None


def test_cold_mornings_warm_the_anchor_and_right_holds_it():
    repo = _repo(reviews=[("2026-09-22", "too_cold"), ("2026-09-23", "bit_cold"),
                          ("2026-09-24", "right"), ("2026-09-25", "right")])
    a = comfort_anchor(repo, _cfg(), 69.0, now=NOW)
    assert a["feedback_f"] == 1.5            # the warmth that fixed it is kept
    assert a["neutral_f"] == 71.5
    assert a["n_reviews"] == 4


def test_warm_mornings_come_back_down_but_never_below_the_sweep():
    repo = _repo(reviews=[(f"2026-09-{d}", "too_warm") for d in range(22, 28)])
    a = comfort_anchor(repo, _cfg(), 69.0, now=NOW)
    assert a["neutral_f"] == 69.0            # the measured neutral is the cold bound


def test_warm_side_is_bounded_and_does_not_wind_up():
    reviews = [(f"2026-09-{d}", "too_cold") for d in range(22, 30)]
    reviews.append(("2026-09-30", "bit_warm"))
    a = comfort_anchor(_repo(reviews=reviews), _cfg(), 69.0, now=NOW)
    # clipped at +4.0 every step, so one "a bit warm" is felt immediately
    assert a["offset_f"] == 3.5
    assert a["neutral_f"] == 72.5


def test_nights_before_the_start_date_are_already_priced_in():
    repo = _repo(reviews=[("2026-09-21", "too_cold")], notes=[("2026-09-22", "woke up cold")])
    a = comfort_anchor(repo, _cfg(), 69.0, now=NOW)
    assert a["neutral_f"] == 70.0 and a["n_reviews"] == 0 and a["n_notes"] == 0


def test_a_note_votes_at_half_weight_when_there_is_no_review():
    repo = _repo(notes=[("2026-09-24", "woke at 2am freezing")])
    a = comfort_anchor(repo, _cfg(), 69.0, now=NOW)
    assert a["n_notes"] == 1
    assert a["feedback_f"] == 0.25
    assert a["last_vote"]["night_date"] == "2026-09-23"


def test_the_review_wins_over_the_note_for_the_same_night():
    repo = _repo(reviews=[("2026-09-23", "right")], notes=[("2026-09-24", "a bit cold")])
    a = comfort_anchor(repo, _cfg(), 69.0, now=NOW)
    # the morning note describes the reviewed night; it is not re-attributed elsewhere
    assert a["n_reviews"] == 1 and a["n_notes"] == 0
    assert a["neutral_f"] == 70.0


def test_feedback_can_be_switched_off():
    repo = _repo(reviews=[("2026-09-22", "too_cold")])
    a = comfort_anchor(repo, _cfg(comfort_feedback_enabled=False), 69.0, now=NOW)
    assert a["neutral_f"] == 70.0


def test_a_missing_review_table_is_just_no_votes():
    repo = SimpleNamespace(conn=sqlite3.connect(":memory:"))
    assert comfort_anchor(repo, _cfg(), 69.0, now=NOW)["neutral_f"] == 70.0


def test_note_vote_reads_complaints_not_mentions():
    assert note_vote("so cold at 3am") == "bit_cold"
    assert note_vote("bed felt chilly") == "bit_cold"
    assert note_vote("woke up sweating") == "bit_warm"
    assert note_vote("way too hot") == "bit_warm"
    assert note_vote("wasn't too hot, not cold") is None
    assert note_vote("slept great") is None
    assert note_vote("cold early then too hot") is None


def test_shifted_profile_moves_the_whole_band():
    prof = {"neutral_f": 69.0, "cool_edge_f": 67.0, "warm_edge_f": 69.5, "source": "sweep"}
    out = shifted_profile(prof, 1.0)
    assert (out["neutral_f"], out["cool_edge_f"], out["warm_edge_f"]) == (70.0, 68.0, 70.5)
    assert out["source"] == "sweep" and out["anchor_offset_f"] == 1.0
    assert prof["neutral_f"] == 69.0          # the stored profile is not mutated
    assert shifted_profile(None, 1.0) is None


def test_controller_steers_around_the_anchored_neutral():
    """End to end through the thermal controller: the settle and the clamp both follow."""
    from sleepctl.controller.controller import SleepController
    cfg = _cfg()
    ctrl = SleepController(cfg)
    anchor = comfort_anchor(_repo(reviews=[("2026-09-22", "too_cold")]), cfg, 69.0, now=NOW)
    prof = shifted_profile({"neutral_f": 69.0, "cool_edge_f": 67.0, "warm_edge_f": 69.5},
                           anchor["offset_f"])
    ctrl.thermal.set_measured_neutral(prof["neutral_f"])
    ctrl.set_comfort_profile(prof)
    assert ctrl.thermal.profile.neutral_f == 71.0
    assert ctrl.thermal.measured_neutral_f == 71.0
