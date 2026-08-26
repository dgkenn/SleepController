

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
