

# ------------------------------------------- wearable bed-entry evidence (2026-08-25 audit)
def _wb_frame(hr, presence=None):
    from sleepctl.models import SensorFrame
    f = SensorFrame.__new__(SensorFrame)
    f.heart_rate = hr
    f.presence = presence
    return f


def _wb_controller():
    from sleepctl.controller.controller import SleepController
    return SleepController.__new__(SleepController)   # pure helper, no init needed


def test_live_varying_wearable_hr_counts_as_bed_entry():
    from sleepctl.config import AppConfig
    c, cfg = _wb_controller(), AppConfig()
    fr = [_wb_frame(h) for h in (77, 74, 71, 72, 68, 69, 69, 77)]   # real 2026-08-25 HR
    assert c._wearable_bed_entry(fr[-1], fr[:-1], cfg) is True


def test_a_frozen_flat_hr_is_not_bed_entry():
    """A band left on a charger reporting a flat line produced a whole morning of fake DEEP on
    2026-08-04. A stale/frozen feed has zero HR range and must never qualify as bed entry."""
    from sleepctl.config import AppConfig
    c, cfg = _wb_controller(), AppConfig()
    fr = [_wb_frame(62.0) for _ in range(8)]
    assert c._wearable_bed_entry(fr[-1], fr[:-1], cfg) is False


def test_implausible_or_missing_hr_is_not_bed_entry():
    from sleepctl.config import AppConfig
    c, cfg = _wb_controller(), AppConfig()
    bad = [_wb_frame(15.0 + i * 0.5) for i in range(8)]
    assert c._wearable_bed_entry(bad[-1], bad[:-1], cfg) is False
    none = [_wb_frame(None) for _ in range(8)]
    assert c._wearable_bed_entry(none[-1], none[:-1], cfg) is False


def test_wearable_evidence_never_contradicts_a_positive_pod_reading():
    """This only ever fills in for UNKNOWN presence. A Pod that positively says out-of-bed (or
    in-bed) owns the answer -- the wearable must not override it in either direction."""
    from sleepctl.config import AppConfig
    c, cfg = _wb_controller(), AppConfig()
    out = [_wb_frame(70.0 + (i % 4), presence=False) for i in range(8)]
    assert c._wearable_bed_entry(out[-1], out[:-1], cfg) is False
    inb = [_wb_frame(70.0 + (i % 4), presence=True) for i in range(8)]
    assert c._wearable_bed_entry(inb[-1], inb[:-1], cfg) is False


# ------------------------------------------- target stabilizer / arm C (2026-08-24 audit)
def _stab_controller(deadband=0.4, dwell=12.0, last_target=68.0):
    from sleepctl.config import AppConfig
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig()
    cfg.tunables.target_stabilizer = True
    cfg.tunables.stabilizer_deadband_f = deadband
    cfg.tunables.stabilizer_min_dwell_min = dwell
    c = SleepController.__new__(SleepController)
    c._last_target_f = last_target
    return c, cfg


def test_stabilizer_suppresses_sub_deadband_noise():
    """A move the bed cannot even resolve is noise, not a decision."""
    from datetime import datetime
    c, cfg = _stab_controller()
    held, why = c._stabilize_target(68.2, datetime(2026, 8, 24, 23, 0), cfg)
    assert held == 68.0 and "deadband" in why


def test_stabilizer_never_delays_a_same_direction_move():
    """A genuine ramp must run at full speed -- only oscillation is damped."""
    from datetime import datetime, timedelta
    t0 = datetime(2026, 8, 24, 23, 0)
    c, cfg = _stab_controller()
    c._stab_last_dir = -1
    c._stab_last_move_at = t0
    held, _ = c._stabilize_target(66.0, t0 + timedelta(minutes=1), cfg)   # cooler again
    assert held is None


def test_stabilizer_holds_a_reversal_inside_the_dwell():
    """THE measured failure: 31 of 36 interventions reversing, several inside one minute."""
    from datetime import datetime, timedelta
    t0 = datetime(2026, 8, 24, 23, 0)
    c, cfg = _stab_controller()
    c._stab_last_dir = -1                 # last move was cooler
    c._stab_last_move_at = t0
    held, why = c._stabilize_target(70.0, t0 + timedelta(minutes=1), cfg)   # now warmer
    assert held == 68.0 and "dwell" in why


def test_stabilizer_allows_a_reversal_once_the_dwell_has_passed():
    from datetime import datetime, timedelta
    t0 = datetime(2026, 8, 24, 23, 0)
    c, cfg = _stab_controller()
    c._stab_last_dir = -1
    c._stab_last_move_at = t0
    held, _ = c._stabilize_target(70.0, t0 + timedelta(minutes=30), cfg)
    assert held is None


def test_stabilizer_is_off_by_default_so_arm_b_is_unchanged():
    from sleepctl.config import AppConfig
    assert AppConfig().tunables.target_stabilizer is False
