"""Cold-dwell relief must not swallow the range the pre-emptive settle deliberately targets.

The relief was written when the maintenance settle was -1.0 F, which lands at 68.0 F and never
approached the 67.0 F cool edge -- so a 1.0 F margin cost nothing. Once the pre-emptive settle
was deepened to target the cool edge, that margin covered 40% of a 2.5 F band, so every pre-empt
was immediately classified as camping and eased back up.

Measured: the bed did not once go below 68.0 F on 2026-09-01 or 2026-09-04, on either side of
the settle fix. The floor was this test, not the settle.
"""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController

_BAND = {"cool_edge_f": 67.0, "warm_edge_f": 69.5, "neutral_f": 69.0}


def _c(band=_BAND):
    c = SleepController(AppConfig())
    c.comfort_profile = dict(band)
    return c


def _dwell(c, target_f, minutes):
    """Hold a target for `minutes` and return the relief offered at the end."""
    t0 = datetime(2026, 9, 4, 1, 0)
    eased = why = None
    for i in range(0, int(minutes) + 1, 5):
        eased, why = c._cold_dwell_relief(target_f, t0 + timedelta(minutes=i), c.cfg)
    return eased, why


def test_the_settle_target_is_not_treated_as_camping_immediately():
    """68.0 F is where the OLD settle landed and is well inside a 2.5 F band."""
    c = _c()
    eased, _ = _dwell(c, 68.0, 200)
    assert eased is None


def test_parking_at_the_actual_cool_edge_still_earns_relief():
    """The safety this exists for is unchanged: four hours at the floor preceded the awakenings
    measured on 2026-08-24."""
    c = _c()
    eased, why = _dwell(c, 67.0, 200)
    assert eased is not None and eased > 67.0
    assert why


def test_the_margin_scales_with_the_band_not_the_degree():
    """A wide band can afford the full configured margin; a narrow one cannot."""
    narrow = _c({"cool_edge_f": 67.0, "warm_edge_f": 69.5, "neutral_f": 69.0})
    wide = _c({"cool_edge_f": 62.0, "warm_edge_f": 72.0, "neutral_f": 68.0})
    assert _dwell(narrow, 68.0, 200)[0] is None      # 0.5 F margin -> not at the edge
    assert _dwell(wide, 62.9, 200)[0] is not None    # 1.0 F margin -> at the edge


def test_relief_needs_a_measured_band():
    """With no comfort profile there is no principled floor to reason about."""
    c = SleepController(AppConfig())
    c.comfort_profile = None
    assert _dwell(c, 67.0, 200)[0] is None


def test_a_short_stretch_at_the_edge_is_not_camping():
    c = _c()
    eased, _ = _dwell(c, 67.0, 20)
    assert eased is None
