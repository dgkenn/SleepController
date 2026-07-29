"""Advisory CBT-I sleep-window module: sleep restriction (compress/expand/hold), the shift-safety
overrides, the time-in-bed floor, and evidence-supported stimulus-control tips.

This module is purely advisory — these tests check the *numbers and reasoning it would suggest*,
never anything about actual controller/thermal behaviour (there is none here to affect).
"""

from sleepctl.cbti import CBTIConfig, recommend_sleep_window, stimulus_control_tips


def night(i, tib, sleep, eff=None, wake=None, **kw):
    """Build a plain-dict night record. `eff` defaults to sleep/tib if not given."""
    if eff is None:
        eff = sleep / tib
    return dict(
        date=f"2026-06-{10 + i:02d}",
        time_in_bed_min=tib,
        total_sleep_min=sleep,
        sleep_efficiency=eff,
        wake_events=wake if wake is not None else [],
        **kw,
    )


def uniform_nights(n, tib, sleep, **kw):
    return [night(i, tib, sleep, **kw) for i in range(n)]


# --------------------------------------------------------------------------- compress / expand / hold


def test_compression_triggered_by_low_efficiency():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)  # eff ~79.2%, below 85% threshold
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.direction == "compress"
    assert adv.change_min < 0
    assert adv.recommended_tib_min == 480 - cfg.step_min
    assert "79" in adv.rationale or "%" in adv.rationale
    assert adv.safe is True


def test_expansion_triggered_by_high_efficiency():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=400, sleep=380)  # eff 95%, above 90% threshold
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.direction == "expand"
    assert adv.change_min == cfg.step_min
    assert adv.recommended_tib_min == 400 + cfg.step_min


def test_hold_within_band():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=450, sleep=400)  # eff ~88.9%, inside 85-90% band
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.direction == "hold"
    assert adv.change_min == 0
    assert adv.recommended_tib_min == 450


def test_step_size_is_respected():
    cfg = CBTIConfig(step_min=20)
    nights = uniform_nights(10, tib=480, sleep=380)
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.direction == "compress"
    assert abs(adv.change_min) == 20

    nights_expand = uniform_nights(10, tib=400, sleep=380)
    adv2 = recommend_sleep_window(nights_expand, cfg=cfg)
    assert adv2.direction == "expand"
    assert adv2.change_min == 20


# --------------------------------------------------------------------------- floor


def test_time_in_bed_floor_never_breached_with_terrible_efficiency():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=340, sleep=100)  # eff ~29%, catastrophic
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.recommended_tib_min >= cfg.min_time_in_bed_min


def test_floor_never_breached_even_when_baseline_already_below_it():
    # Pathological input: the user's actual current time-in-bed is already under the floor.
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=200, sleep=50)
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.recommended_tib_min >= cfg.min_time_in_bed_min


def test_floor_holds_across_many_simulated_weekly_steps():
    # Simulate repeated weekly calls where sleep ability stays fixed and TIB compresses each time;
    # the floor must never be crossed at any point in the sequence.
    cfg = CBTIConfig()
    tib = 700.0
    fixed_sleep = 310  # kept above the severe-short-sleep guard so compression can actually run
    for _ in range(40):
        nights = uniform_nights(10, tib=tib, sleep=fixed_sleep)
        adv = recommend_sleep_window(nights, cfg=cfg)
        assert adv.recommended_tib_min >= cfg.min_time_in_bed_min
        tib = adv.recommended_tib_min
        if adv.direction != "compress":
            break


# --------------------------------------------------------------------------- insufficient data


def test_insufficient_data_returns_hold_with_low_confidence():
    cfg = CBTIConfig()
    nights = uniform_nights(3, tib=480, sleep=380)  # below min_nights_required=7
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.direction == "hold"
    assert adv.change_min == 0
    assert adv.confidence <= 0.4
    assert "not enough nights" in adv.rationale.lower()


def test_no_nights_at_all_is_handled_gracefully():
    adv = recommend_sleep_window([])
    assert adv.direction == "hold"
    assert adv.eligible_nights == 0
    assert adv.confidence <= 0.4


# --------------------------------------------------------------------------- shift / safety


def test_compression_refused_when_upcoming_high_stakes():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)  # would otherwise compress
    adv = recommend_sleep_window(nights, upcoming_high_stakes=True, cfg=cfg)
    assert adv.direction == "hold"
    assert adv.change_min == 0
    assert adv.safe is True
    assert len(adv.safety_notes) >= 1
    assert any(
        "on-call" in note.lower() or "shift" in note.lower() or "high-stakes" in note.lower()
        for note in adv.safety_notes
    )


def test_high_stakes_does_not_block_expand_or_hold():
    cfg = CBTIConfig()
    expand_nights = uniform_nights(10, tib=400, sleep=380)
    adv = recommend_sleep_window(expand_nights, upcoming_high_stakes=True, cfg=cfg)
    assert adv.direction == "expand"  # only compression is restricted by this safety rule
    assert adv.safety_notes == []


def test_compression_refused_when_severe_short_sleep():
    cfg = CBTIConfig()
    # Bad efficiency AND already averaging well under the severe-short-sleep guard (300 min).
    nights = uniform_nights(10, tib=480, sleep=260)
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.direction == "hold"
    assert adv.change_min == 0
    assert len(adv.safety_notes) >= 1
    assert any("short-sleeping" in note.lower() or "short sleep" in note.lower()
               for note in adv.safety_notes)


# --------------------------------------------------------------------------- eligibility filtering


def test_ineligible_nights_excluded_from_efficiency_calc():
    # Widen the rolling window so all 20 nights are considered — isolates the eligibility filter
    # from window-slicing behaviour, which is a separate concern.
    cfg = CBTIConfig(rolling_window_nights=25)
    good_nights = uniform_nights(10, tib=480, sleep=380)  # eff ~79.2%, drives compression
    # A pile of "great" nights that are flagged as work/short nights — should NOT drag the mean up.
    contaminating_nights = [
        night(60 + i, tib=480, sleep=470, night_type="work_night") for i in range(10)
    ]
    adv_clean = recommend_sleep_window(good_nights, cfg=cfg)
    adv_mixed = recommend_sleep_window(good_nights + contaminating_nights, cfg=cfg)
    assert adv_mixed.eligible_nights == adv_clean.eligible_nights == 10
    assert adv_mixed.direction == adv_clean.direction == "compress"
    assert adv_mixed.mean_efficiency == adv_clean.mean_efficiency


def test_explicit_eligible_false_excludes_a_night():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)
    nights.append(night(50, tib=480, sleep=470, eligible=False))
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.eligible_nights == 10


def test_nap_flag_excludes_a_night():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)
    nights.append(night(50, tib=60, sleep=55, night_type="nap"))
    adv = recommend_sleep_window(nights, cfg=cfg)
    assert adv.eligible_nights == 10


# --------------------------------------------------------------------------- bedtime computation


def test_recommended_bedtime_computed_from_wake_time():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)
    adv = recommend_sleep_window(nights, required_wake_time="06:30", cfg=cfg)
    assert adv.recommended_bedtime is not None
    # 480 - step(15) = 465 min TIB -> bedtime = 06:30 minus 465 min = 22:45
    assert adv.recommended_bedtime == "22:45"


def test_recommended_bedtime_none_when_wake_time_not_given():
    nights = uniform_nights(10, tib=480, sleep=380)
    adv = recommend_sleep_window(nights)
    assert adv.recommended_bedtime is None


# --------------------------------------------------------------------------- stimulus control tips


def test_long_awakenings_tip_appears_when_pattern_present():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380, wake=[25, 30, 22])
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert any("20 min" in t or "get up" in t.lower() or "get out of bed" in t.lower() for t in tips)


def test_long_awakenings_tip_absent_when_awakenings_are_short():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380, wake=[3, 5, 2])
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert not any("often long" in t.lower() for t in tips)


def test_frequent_awakenings_count_tip_when_only_counts_available():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380, wake=3)  # bare count, no durations
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert any("waking frequently" in t.lower() for t in tips)


def test_no_awakening_tip_when_awakenings_are_rare():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380, wake=0)
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert not any("waking frequently" in t.lower() or "often long" in t.lower() for t in tips)


def test_variable_bedtime_tip_appears_when_bedtimes_scatter():
    cfg = CBTIConfig()
    scattered = ["22:00", "23:30", "00:45", "21:30", "23:00", "00:15", "22:45", "23:50", "21:00", "00:30"]
    nights = [night(i, 480, 380, bedtime=scattered[i]) for i in range(10)]
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert any("anchor" in t.lower() or "consistent" in t.lower() for t in tips)


def test_no_variable_bedtime_tip_when_bedtimes_are_consistent():
    cfg = CBTIConfig()
    nights = [night(i, 480, 380, bedtime="22:30") for i in range(10)]
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert not any("anchor" in t.lower() for t in tips)


def test_no_variable_bedtime_tip_when_bedtime_field_absent():
    # The module must not invent a bedtime pattern from data that isn't there.
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)  # no `bedtime` key at all
    tips = stimulus_control_tips(nights, cfg=cfg)
    assert not any("anchor" in t.lower() for t in tips)


def test_no_tips_with_insufficient_data():
    nights = uniform_nights(2, tib=480, sleep=380, wake=[30, 30])
    assert stimulus_control_tips(nights) == []


# --------------------------------------------------------------------------- determinism


def test_recommend_sleep_window_is_deterministic():
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380, wake=[25, 10])
    results = [recommend_sleep_window(nights, required_wake_time="06:30", cfg=cfg) for _ in range(5)]
    first = results[0]
    for r in results[1:]:
        assert r.direction == first.direction
        assert r.recommended_tib_min == first.recommended_tib_min
        assert r.change_min == first.change_min
        assert r.rationale == first.rationale
        assert r.confidence == first.confidence
        assert r.recommended_bedtime == first.recommended_bedtime


def test_stimulus_control_tips_is_deterministic():
    nights = uniform_nights(10, tib=480, sleep=380, wake=[25, 10])
    results = [stimulus_control_tips(nights) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_determinism_unaffected_by_input_list_order():
    # Nights are sorted internally by date, so shuffled input order should not change the result.
    cfg = CBTIConfig()
    nights = uniform_nights(10, tib=480, sleep=380)
    reversed_nights = list(reversed(nights))
    adv1 = recommend_sleep_window(nights, cfg=cfg)
    adv2 = recommend_sleep_window(reversed_nights, cfg=cfg)
    assert adv1.direction == adv2.direction
    assert adv1.recommended_tib_min == adv2.recommended_tib_min


# ------------------------------------------------------------------ time-of-day coercion
def test_parse_time_of_day_accepts_a_full_datetime():
    """REGRESSION: NightSummary.bedtime/wake_time are full datetimes, and a datetime is NOT an
    instance of datetime.time -- so this raised TypeError on every night that actually had a
    bedtime recorded. The tests here all passed because they supply strings and time objects;
    only calling it the way services.cbti_advice does exposed it."""
    from datetime import datetime as _dt, time as _t

    from sleepctl.cbti import _parse_time_of_day

    assert _parse_time_of_day(_dt(2026, 7, 1, 23, 30)) == _t(23, 30)
    assert _parse_time_of_day(_t(23, 30)) == _t(23, 30)
    assert _parse_time_of_day("23:30") == _t(23, 30)
    assert _parse_time_of_day("23:30:59") == _t(23, 30)


def test_parse_time_of_day_still_rejects_nonsense():
    import pytest as _pytest

    from sleepctl.cbti import _parse_time_of_day

    for bad in (None, 1234, [], {}):
        with _pytest.raises(TypeError):
            _parse_time_of_day(bad)


def test_stimulus_control_tips_handles_datetime_bedtimes():
    """The end-to-end shape services.cbti_advice actually passes."""
    from datetime import datetime as _dt

    from sleepctl.cbti import stimulus_control_tips

    rows = [{"date": f"2026-07-{i + 1:02d}", "time_in_bed_min": 480.0,
             "total_sleep_min": 420.0, "sleep_efficiency": 0.875, "wake_events": 1,
             "bedtime": _dt(2026, 7, i + 1, 21 + (i % 3), 0)} for i in range(10)]
    tips = stimulus_control_tips(rows)      # must not raise
    assert isinstance(tips, list)
