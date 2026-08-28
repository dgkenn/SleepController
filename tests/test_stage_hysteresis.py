"""Sleep stages are physiologically persistent -- a real bout runs 15-30 minutes -- so an
estimated label that changes every 30-second tick is measurement noise.

Measured on 2026-08-27: 233 stage flips across 686 maintenance ticks, median bout 2 ticks, and
187 of those flips (80%) were light<->deep oscillation from ordinary beat-to-beat HR variation
across the boundary. The daemon ticks ~30 s while the Pod frame refreshes ~60 s, so consecutive
ticks re-score the same physiology and land on different sides of it.

It costs more than a messy hypnogram: every deep->light flip fires a `stage_regression` vote in
the wake detector, so the flapping manufactures wake evidence (93 such flips that night).
"""
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import SleepStage

L, D, R, A = SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM, SleepStage.AWAKE


def _run(stages, hold=2):
    cfg = AppConfig.default()
    cfg.tunables.stage_hold_ticks = hold
    c = SleepController(cfg)
    return [c._hold_stage((s, 0.6, "model"), cfg)[0] for s in stages]


def test_a_single_tick_flip_is_absorbed():
    assert _run([L, L, D, L, L]) == [L, L, L, L, L]


def test_a_sustained_change_is_adopted():
    assert _run([L, L, D, D, D]) == [L, L, L, D, D]


def test_alternating_noise_never_switches():
    """The measured failure: light/deep alternating every tick."""
    assert _run([L, D] * 8) == [L] * 16


def test_awake_is_never_delayed():
    """Wake detection must stay responsive. Smoothing the chart is not worth delaying the one
    thing this system exists to catch."""
    assert _run([L, L, A, L, L])[2] is A


def test_awake_does_not_need_persistence_even_amid_churn():
    assert _run([L, D, A, D, L])[2] is A


def test_the_hysteresis_can_be_disabled():
    assert _run([L, D, L, D], hold=1) == [L, D, L, D]


def test_the_first_estimate_is_adopted_immediately():
    """Nothing to hold against on the first tick; delaying it would blank the start of a night."""
    assert _run([R])[0] is R


def test_suppressed_flips_are_counted_for_telemetry():
    cfg = AppConfig.default()
    cfg.tunables.stage_hold_ticks = 2
    c = SleepController(cfg)
    for s in [L, D, L, D, L]:
        c._hold_stage((s, 0.6, "model"), cfg)
    assert c._stage_hold_suppressed > 0


def test_stage_source_keeps_its_fixed_vocabulary():
    """`stage_source` is matched against a known set elsewhere; the hold must not decorate it."""
    cfg = AppConfig.default()
    cfg.tunables.stage_hold_ticks = 2
    c = SleepController(cfg)
    c._hold_stage((L, 0.6, "model"), cfg)
    _st, _cf, src = c._hold_stage((D, 0.6, "model"), cfg)
    assert src == "model"


def test_leaving_awake_is_also_immediate():
    """Exempting only the ENTRY to AWAKE made it sticky -- easy to enter, slow to leave. On the
    2026-08-27 sequence that inflated the awake label count from 30 to 53, and would have
    inflated WASO and the wake-event count with it. Waking must be responsive; so must going
    back to sleep."""
    assert _run([L, L, A, L, L]) == [L, L, A, L, L]


def test_churn_around_an_awakening_does_not_inflate_the_awake_count():
    out = _run([L, D, L, A, L, D, L, A, L])
    assert sum(1 for x in out if x is A) == 2
