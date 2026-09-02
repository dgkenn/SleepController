"""Scoring a sleep stager over hours the sleeper was making breakfast.

Until bed exit could be detected at all, a session ran on for hours after the sleeper got up, so
"the night" as published routinely contains a walking-around morning. On 2026-08-27 that is 181
of 689 epochs, and including them takes kappa against Cole-Kripke+Webster from 0.513 to 0.154.

It is also why two runs of the same comparison over the same night produced 456 and 684 epochs
and could not be reconciled: neither recorded its own denominator.
"""

from sleepctl.eval.in_bed import (OUT_OF_BED_HR, in_bed_minutes, minute_heart_rates,
                                  out_of_bed_rows, provenance, split_in_bed)


def _s(ts, hr):
    return {"ts": ts, "heart_rate": hr}


def test_the_peak_heart_rate_of_each_minute_is_used():
    """A minute containing one ambulatory reading is not an in-bed minute."""
    hrs = minute_heart_rates([_s("2026-08-28T07:00:01", 66.0),
                              _s("2026-08-28T07:00:31", 118.0)])
    assert hrs["2026-08-28T07:00"] == 118.0


def test_an_ambulatory_minute_is_dropped():
    keys = ["2026-08-28T07:00", "2026-08-28T03:00"]
    hrs = {"2026-08-28T07:00": 118.0, "2026-08-28T03:00": 64.0}
    assert in_bed_minutes(keys, hrs) == ["2026-08-28T03:00"]


def test_a_minute_with_no_heart_rate_is_kept():
    """Absence of evidence is not evidence of being up. Dropping unmeasured epochs would shrink
    every comparison toward the stretches where the sensor happened to be working."""
    assert in_bed_minutes(["2026-08-28T03:00"], {}) == ["2026-08-28T03:00"]


def test_exactly_at_the_ceiling_counts_as_out_of_bed():
    assert in_bed_minutes(["m"], {"m": OUT_OF_BED_HR}) == []


def test_split_reports_what_it_removed():
    """A filter that silently removes a quarter of a night is indistinguishable from a bug."""
    samples = [_s("2026-08-28T07:00:01", 118.0), _s("2026-08-28T03:00:01", 64.0)]
    kept, dropped = split_in_bed(samples, ["2026-08-28T07:00", "2026-08-28T03:00"])
    assert kept == ["2026-08-28T03:00"]
    assert dropped == ["2026-08-28T07:00"]


def test_provenance_carries_the_denominator():
    night = {"night_date": "2026-08-27",
             "sensor_capture": {"n_samples": 2684, "heart_rate_present": 1508,
                                "movement_present": 1474}}
    line = provenance(night, kept=508, dropped=181)
    assert "2026-08-27" in line and "epochs scored=508" in line and "dropped=181" in line


def test_row_variant_uses_whichever_heart_rate_is_higher():
    rows = [{"hr": 66.0, "hr_from_ibi": 120.0}, {"hr": 64.0, "hr_from_ibi": 65.0}]
    assert len(out_of_bed_rows(rows)) == 1


def test_row_variant_keeps_rows_with_no_heart_rate_at_all():
    assert len(out_of_bed_rows([{"hr": None, "hr_from_ibi": None}])) == 1
