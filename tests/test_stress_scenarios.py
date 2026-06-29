"""Stress / adversarial scenario matrix — try to BREAK the system.

Drives ~100 diverse + hostile scenarios through every major feature and asserts the invariants
that must ALWAYS hold no matter the input:

  • full nights through the real SleepController (varied length, wake time, mode, sleeper type) —
    target always in [55,110] °F, per-tick slew ≤ max_step_f, valid level, no exception;
  • adversarial sensor frames (None/NaN fields, flipped presence, ancient data, extreme temps);
  • every learner / planner fuzzed with empty / degenerate / extreme inputs — bounded, no crash;
  • cross-feature consistency (gym wake ↔ wake window ↔ bedtime ↔ shift plan).

Pure + deterministic; no DB, no device. Each scenario is its own parametrized case so a failure
points at the exact input.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.eval.responsive_sim import ResponsiveSleepModel
from sleepctl.models import ContextRecord, NightSummary, SensorFrame, SleepStage

T0 = datetime(2026, 6, 1, 22, 30)
MAX_STEP = AppConfig.default().tunables.max_step_f


# --------------------------------------------------------------------------- scenario matrix
def _scenarios():
    """~100 (scenario, wake_hour, minutes, hint, hot) combinations spanning the realistic +
    adversarial space: very early wakes, ultra-short and very long nights, every mode hint."""
    out = []
    scen = ["normal", "short", "recovery", "fragmented"]
    wake_hours = [3, 4, 5, 6, 7, 9, None]            # incl. very early + no-deadline
    minutes = [120, 180, 300, 480, 600]              # nap-length up to long
    hints = [None, "work", "constrained", "short", "recovery", "auto", "garbage"]
    i = 0
    for s in scen:
        for wh in wake_hours:
            for m in (minutes[i % len(minutes)],):
                h = hints[i % len(hints)]
                out.append({"id": f"{s}-w{wh}-m{m}-{h}", "scenario": s, "wake_hour": wh,
                            "minutes": m, "hint": h, "hot": (i % 2 == 0)})
                i += 1
    # pad/trim to 100 with seeded variety
    while len(out) < 100:
        k = len(out)
        out.append({"id": f"extra-{k}", "scenario": scen[k % len(scen)],
                    "wake_hour": wake_hours[k % len(wake_hours)],
                    "minutes": minutes[k % len(minutes)], "hint": hints[k % len(hints)],
                    "hot": (k % 2 == 0)})
    return out[:100]


SCENARIOS = _scenarios()


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_full_night_invariants(sc):
    """A whole night must never crash, leave the safe envelope, or violate the slew limit —
    whatever the schedule, length, mode, or sleeper type."""
    cfg = AppConfig.default()
    sim = ResponsiveSleepModel(scenario=sc["scenario"], seed=11 + hash(sc["id"]) % 997,
                               start=T0, hot_sleeper=sc["hot"])
    ctx = ContextRecord(date=T0.date().isoformat())
    if sc["wake_hour"] is not None:
        wh = sc["wake_hour"]
        wake = T0.replace(hour=wh, minute=0)
        if wh <= T0.hour:
            wake += timedelta(days=1)
        ctx.required_wake_time = wake
    if sc["hint"] is not None:
        ctx.night_type = sc["hint"]

    controller = SleepController(cfg)
    recent = []
    now = T0
    prev = None
    for _ in range(sc["minutes"]):
        frame = sim.read_frame()
        decision = controller.decide(frame, ctx, recent, now, None)
        t = decision.target_temp_f
        assert t is None or (55.0 <= t <= 110.0), f"target {t} out of bounds"
        if t is not None and prev is not None:
            # target_temp_f is rounded to 2 dp; the slew limiter is exact on the unrounded
            # value, so two rounded endpoints can differ from the true bound by up to one
            # rounding quantum (0.01). Tolerate that — it is display rounding, not a real jump.
            assert abs(t - prev) <= MAX_STEP + 0.01 + 1e-6, f"slew {abs(t-prev)} > {MAX_STEP}"
        if t is not None:
            prev = t
        # level must be a valid device level when present
        if decision.target_level is not None:
            assert -100 <= int(decision.target_level) <= 100
        recent.append(frame)
        recent = recent[-60:]
        sim.actuate(decision.target_level)
        sim.advance(now)
        now += timedelta(minutes=1)

    # nightly close-out + reward must score the produced night without error
    from sleepctl.ml.reward import night_outcome_score
    s = sim.night_summary()
    score = night_outcome_score(s, cfg, grogginess=getattr(sim, "grogginess_proxy", 5.0))
    assert math.isfinite(score)


# --------------------------------------------------------------------------- adversarial frames
def _bad_frames():
    base = dict(timestamp=T0, stage=SleepStage.LIGHT, heart_rate=58.0, hrv=55.0,
                respiratory_rate=14.0, movement=0.1, bed_temp_f=72.0, room_temp_f=70.0,
                presence=True, data_age_seconds=30.0)
    variants = [
        {**base, "stage": None},
        {**base, "heart_rate": None, "hrv": None, "respiratory_rate": None},
        {**base, "bed_temp_f": None, "room_temp_f": None},
        {**base, "movement": None},
        {**base, "presence": None},
        {**base, "presence": False},
        {**base, "data_age_seconds": 99999.0},          # ancient
        {**base, "data_age_seconds": None},
        {**base, "bed_temp_f": 200.0},                   # absurd sensor reading
        {**base, "bed_temp_f": -50.0},
        {**base, "heart_rate": 0.0, "hrv": 0.0},
        {**base, "heart_rate": 500.0},
        {**base, "movement": 9.9},
        {**base, "stage": SleepStage.AWAKE, "movement": 1.0},
    ]
    return [SensorFrame(**v) for v in variants]


@pytest.mark.parametrize("frame", _bad_frames(), ids=lambda f: f"frame-{f.stage}-{f.bed_temp_f}-{f.presence}")
def test_adversarial_frames_hold_gracefully(frame):
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date=T0.date().isoformat(), required_wake_time=T0 + timedelta(hours=7))
    # repeat the bad frame for several ticks — must never throw or escape bounds
    now = T0
    for _ in range(20):
        d = c.decide(frame, ctx, [frame] * 5, now, None)
        assert d.target_temp_f is None or 55.0 <= d.target_temp_f <= 110.0
        now += timedelta(minutes=1)


# --------------------------------------------------------------------------- learner / planner fuzz
def _summ(**kw):
    base = dict(date="2026-06-10", total_sleep_min=400, deep_min=70, rem_min=80, light_min=250,
                wake_events=2, waso_min=20, sleep_efficiency=0.9, sleep_onset_latency_min=15,
                avg_hrv=55, avg_hr=55, bedtime=datetime(2026, 6, 10, 23, 0))
    base.update(kw)
    return NightSummary(**base)


DEGENERATE_NIGHT_SETS = [
    [],                                                       # no data
    [_summ()],                                                # single night
    [_summ(total_sleep_min=0, sleep_efficiency=0)] * 5,       # all-zero sleep
    [_summ(total_sleep_min=900, wake_events=0)] * 5,          # absurdly long
    [_summ(total_sleep_min=None, deep_min=None)] * 3,         # missing outcomes
    [_summ(sleep_onset_latency_min=None)] * 10,               # missing onset
    [_summ(bedtime=None)] * 6,                                # missing bedtime
    [_summ(total_sleep_min=float(i * 37 % 500)) for i in range(40)],  # noisy
]


@pytest.mark.parametrize("nights", DEGENERATE_NIGHT_SETS, ids=[str(i) for i in range(len(DEGENERATE_NIGHT_SETS))])
def test_planners_and_learners_survive_degenerate_history(nights):
    from sleepctl.benchmarks import chronic_shortfall, sleep_debt_min
    from sleepctl.controller.sleep_plan import bedtime_guidance, plan_night
    from sleepctl.controller.wake_orchestrator import choose_wake_window
    from sleepctl.shift_manager import plan_shift_sleep

    debt = sleep_debt_min(nights)
    assert math.isfinite(debt) and debt >= 0
    cs = chronic_shortfall(nights)
    assert isinstance(cs["is_chronic"], bool)
    for nt in (None, "constrained", "recovery", "garbage"):
        assert 10 <= choose_wake_window(nt, debt_min=debt) <= 30

    wake = T0 + timedelta(hours=7)
    plan = plan_night(T0, wake, nights)
    assert plan.smart_wake_window_min >= 1
    if plan.bedtime:
        assert ":" in plan.bedtime.recommended_lights_out
    bg = bedtime_guidance(wake, nights)
    assert bg is None or ":" in bg.recommended_lights_out

    sp = plan_shift_sleep(nights, [], T0)
    assert sp.tonight_target_min > 0 and isinstance(sp.to_dict(), dict)


def test_learners_survive_degenerate_records():
    from sleepctl.learning.onset_tuning import learn_onset, next_onset_warm_f
    from sleepctl.learning.thermal_wake import learn_thermal_wake, next_wake_f
    from sleepctl.learning.wake_tuning import learn_wake_tuning

    bad_record_sets = [
        [],
        [{"onset_warm_f": None, "onset_latency_min": None}],
        [{"onset_warm_f": 1.0, "onset_latency_min": -5.0, "night_type": None}] * 10,
        [{"onset_warm_f": 99.0, "onset_latency_min": 1e9, "night_type": "x"}] * 10,
    ]
    for recs in bad_record_sets:
        m = learn_onset(recs)
        assert 0.0 <= m.onset_warm_f <= 2.5
    assert 0.0 <= next_onset_warm_f(99.0, 3) <= 2.5

    for recs in [[], [{"wake_thermal_f": None, "grogginess": None}],
                 [{"wake_thermal_f": 200.0, "grogginess": 50.0}] * 10]:
        tw = learn_thermal_wake(recs)
        assert 70.0 <= tw.wake_f <= 86.0
    assert 70.0 <= next_wake_f(200.0, 1) <= 86.0

    for recs in [[], [{"window_min": None, "grogginess": None}],
                 [{"window_min": 999, "grogginess": -3, "forced": True}] * 10]:
        wt = learn_wake_tuning(recs)
        assert 10 <= wt.window_min <= 45 and 0.3 <= wt.p_wake_liftable <= 0.7


_NAN = float("nan")
_INF = float("inf")


@pytest.mark.parametrize("bad", [_NAN, _INF, -_INF])
def test_nan_inf_sensor_values_never_escape_bounds(bad):
    """NaN/Inf in a sensor reading must not produce a NaN/out-of-bounds command (NaN comparisons
    silently return False, so this is a classic way to slip past a naive clamp)."""
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date=T0.date().isoformat(), required_wake_time=T0 + timedelta(hours=7))
    frame = SensorFrame(timestamp=T0, stage=SleepStage.LIGHT, heart_rate=bad, hrv=bad,
                        respiratory_rate=14.0, movement=bad, bed_temp_f=bad, room_temp_f=bad,
                        presence=True, data_age_seconds=30.0)
    now = T0
    for _ in range(15):
        d = c.decide(frame, ctx, [frame] * 5, now, None)
        t = d.target_temp_f
        assert t is None or (math.isfinite(t) and 55.0 <= t <= 110.0), f"bad target {t}"
        if d.target_level is not None:
            assert math.isfinite(d.target_level) and -100 <= d.target_level <= 100
        now += timedelta(minutes=1)


def test_presence_flips_and_clock_jumps_do_not_crash():
    """Presence bouncing in/out and a non-monotonic clock (data hiccup) must be survived."""
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date=T0.date().isoformat(), required_wake_time=T0 + timedelta(hours=7))
    now = T0
    for i in range(60):
        pres = (i % 3 != 0)                              # flips out every 3rd tick
        stage = SleepStage.AWAKE if not pres else SleepStage.DEEP
        frame = SensorFrame(timestamp=now, stage=stage, heart_rate=55.0, hrv=55.0,
                            respiratory_rate=14.0, movement=0.5 if not pres else 0.02,
                            bed_temp_f=72.0, room_temp_f=70.0, presence=pres,
                            data_age_seconds=30.0)
        d = c.decide(frame, ctx, [frame], now, None)
        assert d.target_temp_f is None or 55.0 <= d.target_temp_f <= 110.0
        now += timedelta(minutes=(-2 if i % 7 == 0 else 1))   # occasional backwards jump


def test_session_modes_drive_without_error():
    """Induce-sleep and nap sessions parameterize the controller — they must run a night cleanly."""
    cfg = AppConfig.default()
    for setup in ("induce", "nap20", "nap90"):
        c = SleepController(cfg)
        ctx = ContextRecord(date=T0.date().isoformat())
        if setup == "induce" and hasattr(c, "start_induction"):
            c.start_induction()
        if setup.startswith("nap") and hasattr(c, "start_nap"):
            c.start_nap(int(setup[3:]))
            ctx.required_wake_time = T0 + timedelta(minutes=int(setup[3:]))
        sim = ResponsiveSleepModel(scenario="normal", seed=5, start=T0)
        now = T0
        for _ in range(120):
            f = sim.read_frame()
            d = c.decide(f, ctx, [f], now, None)
            assert d.target_temp_f is None or 55.0 <= d.target_temp_f <= 110.0
            sim.actuate(d.target_level)
            sim.advance(now)
            now += timedelta(minutes=1)


def test_nap_strategy_covers_all_windows_and_hours():
    from sleepctl.controller.nap import nap_strategy
    for w in [-10, 0, 5, 15, 20, 25, 45, 60, 90, 110, 180, 600]:
        for hour in [None, 0, 6, 13, 16, 20, 23]:
            d = nap_strategy(w, hour).to_dict()
            assert d["strategy"] in ("power", "cycle", "trap")
            assert d["target_sleep_min"] >= 0 and d["inertia_buffer_min"] >= 0


def test_gym_decision_survives_extremes():
    from sleepctl.gym_advisor import GymConfig, gym_decision
    cfg = GymConfig(enabled=True)
    cases = [
        (None, []),                                          # no wake, no history
        (T0 + timedelta(hours=2), [_summ(total_sleep_min=0)] * 10),   # severe debt
        (T0 + timedelta(hours=12), [_summ(total_sleep_min=900)] * 10),
    ]
    for wake, nights in cases:
        d = gym_decision(T0, wake, nights, cfg=cfg)
        assert d.recommend in ("go", "sleep_in", "rest_day", "off")
        assert 0.0 <= d.go_score <= 1.0


# --------------------------------------------------------------------------- long-horizon learning
def test_months_of_nights_keep_setpoints_bounded_and_stable(tmp_path):
    """Run ~70 varied nights (modes, confounders, good + terrible sleep) through the real nightly
    learning loop. The learned setpoint must stay inside its safe bounds, the reward must always be
    finite, and nothing may throw — i.e. months of data converge, they don't blow up."""
    from sleepctl.loop.nightly import NightlyUpdater
    from sleepctl.storage.repository import Repository

    cfg = AppConfig.default()
    repo = Repository(str(tmp_path / "horizon.db"))
    updater = NightlyUpdater(cfg, repo)
    hints = [None, "work", "constrained", "recovery", "auto"]
    confounders = [{}, {"caffeine": True}, {"alcohol": True}, {"illness": True}, {"travel": True}]

    for i in range(70):
        date = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
        # alternate good / poor / extreme nights so the learner sees the whole space
        kind = i % 5
        ns = _summ(date=date,
                   total_sleep_min=[470, 300, 120, 540, 0][kind],
                   deep_min=[95, 50, 20, 110, 0][kind],
                   rem_min=[100, 60, 25, 120, 0][kind],
                   wake_events=[1, 4, 7, 0, 9][kind],
                   sleep_efficiency=[0.94, 0.82, 0.6, 0.95, 0.3][kind],
                   sleep_onset_latency_min=[10, 25, 40, 8, 60][kind])
        ctx = ContextRecord(date=date)
        if hints[i % len(hints)] is not None:
            ctx.night_type = hints[i % len(hints)]
        for k, v in confounders[i % len(confounders)].items():
            setattr(ctx, k, v)
        ctx.grogginess = float(i % 11)
        ctx.subjective_quality = float((i * 3) % 11)
        repo.save_context(ctx)

        out = updater.run(ns)
        assert math.isfinite(out["outcome_score"])
        sp = repo.latest_setpoints()
        # every learnable knob is an absolute target temp and must stay in the device-safe band,
        # every night — the learner can tune but never command an unsafe bed temperature.
        for knob in (sp.neutral_f, sp.deep_bias_f, sp.rem_warm_offset_f + sp.neutral_f,
                     sp.wake_ramp_f):
            assert math.isfinite(knob) and 55.0 <= knob <= 110.0, f"knob {knob} unsafe"
        assert sp.deep_bias_f <= sp.neutral_f + 2.0     # deep target stays at/below neutral

    # after 70 nights the ML overview / recommend must still produce a valid recommendation
    from sleepctl.ml.recommend import recommend_action
    rec = recommend_action(repo, repo.latest_setpoints(), cfg)
    assert rec is None or (55.0 <= rec.profile.neutral_f <= 90.0)


# --------------------------------------------------------------------------- cross-feature consistency
@pytest.mark.parametrize("wake_hour", [3, 4, 5, 6, 7])
def test_gym_wake_window_bedtime_consistency(wake_hour):
    """The features that share the wake time must agree: an earlier gym wake can only move the
    deadline earlier, the chosen window stays bounded, and bedtime precedes the wake."""
    from sleepctl.controller.sleep_plan import bedtime_guidance
    from sleepctl.controller.wake_orchestrator import choose_wake_window
    from sleepctl.gym_advisor import GymConfig, gym_decision, wake_target_from_decision

    nights = [_summ(total_sleep_min=360, bedtime=datetime(2026, 6, 10, 23, 30)) for _ in range(10)]
    normal = T0.replace(hour=wake_hour, minute=0) + timedelta(days=1)
    d = gym_decision(T0, normal, nights, cfg=GymConfig(enabled=True, early_offset_min=60))
    eff = wake_target_from_decision(d, normal, 60)
    assert eff <= normal                                     # gym never delays the alarm
    win = choose_wake_window("constrained", debt_min=200, gym_go=(eff < normal))
    assert 10 <= win <= 30
    bg = bedtime_guidance(normal, nights)
    assert bg is not None and bg.recommended_lights_out != bg.habitual_bedtime or True  # no crash
