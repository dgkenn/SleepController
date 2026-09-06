"""Will the band still be running at wake time?

A percentage against a fixed threshold could only say "32% -- unlikely to last the night", which
is a guess dressed as a finding. The band reports its level once per connection, so two
connections give a discharge rate, and a rate plus the wake deadline answers the real question.
"""

from datetime import datetime, timedelta, timezone

import app.diagnostics as diag


class _Repo:
    conn = None


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _patch(monkeypatch, pct, history, until_wake_h):
    monkeypatch.setattr("app.services.wearable_battery",
                        lambda repo: {"pct": pct, "age_h": 0.1, "low": pct <= 40})
    monkeypatch.setattr("app.services._kv_get_json",
                        lambda repo, key: history if "history" in key else None)
    if until_wake_h is None:
        monkeypatch.setattr("app.services.schedule_brief", lambda repo: {})
    else:
        wake = datetime.now(timezone.utc) + timedelta(hours=until_wake_h)
        monkeypatch.setattr("app.services.schedule_brief",
                            lambda repo: {"required_wake_time": wake.isoformat()})
    return diag._check_wearable_battery(_Repo())


_DISCHARGING = [{"pct": 50, "ts": _iso(5.0)}, {"pct": 40, "ts": _iso(1.0)}]   # 2.5 %/h


def test_a_band_that_will_die_before_wake_is_a_failure(monkeypatch):
    """The projection is compared against the WAKE TIME, not a nominal eight hours: a band that
    dies at 05:30 has lost the back half of the night, which is the half that matters here."""
    r = _patch(monkeypatch, pct=20, history=_DISCHARGING, until_wake_h=12.0)
    assert r["status"] == "fail"
    assert "BEFORE you wake" in r["detail"]


def test_a_comfortable_margin_is_ok(monkeypatch):
    r = _patch(monkeypatch, pct=95, history=_DISCHARGING, until_wake_h=7.0)
    assert r["status"] == "ok"


def test_a_thin_margin_warns(monkeypatch):
    """20% of the 25.5h full-charge runtime is 5.1h; waking in 5.5h leaves under an hour."""
    r = _patch(monkeypatch, pct=20, history=_DISCHARGING, until_wake_h=5.5)
    assert r["status"] == "warn"


def test_an_optimistic_between_connections_rate_is_capped(monkeypatch):
    """The band reports once per CONNECTION, mostly while idle rather than streaming ACC at
    52 Hz plus PPI. Taken at face value that produced "100% -- about 374.5h left (measured
    0.3%/h)": fifteen days from a band measured flat in 25.5 hours."""
    slow = [{"pct": 100, "ts": _iso(4.0)}, {"pct": 99, "ts": _iso(1.0)}]   # 0.33 %/h
    r = _patch(monkeypatch, pct=100, history=slow, until_wake_h=None)
    assert "374" not in r["detail"]
    assert "25.5h full-charge" in r["detail"]
    assert "not measured under streaming load" in r["detail"]


def test_the_measured_rate_is_preferred_over_the_fallback(monkeypatch):
    r = _patch(monkeypatch, pct=40, history=_DISCHARGING, until_wake_h=None)
    assert "measured" in r["detail"]


def test_the_fallback_is_the_night_it_died(monkeypatch):
    """25.5 h full-charge runtime, measured on 2026-08-06."""
    r = _patch(monkeypatch, pct=32, history=[], until_wake_h=None)
    assert "25.5h full-charge" in r["detail"]
    # 32% of 25.5h is ~8.2h
    assert "8.2h" in r["detail"]


def test_a_charge_between_readings_does_not_become_a_negative_discharge(monkeypatch):
    """Averaging across a charge would report a band that gains capacity by being worn."""
    rising = [{"pct": 20, "ts": _iso(5.0)}, {"pct": 90, "ts": _iso(1.0)}]
    r = _patch(monkeypatch, pct=90, history=rising, until_wake_h=None)
    assert "25.5h full-charge" in r["detail"]   # fell back, no negative rate invented


def test_no_reading_at_all_is_info_not_a_guess(monkeypatch):
    monkeypatch.setattr("app.services.wearable_battery", lambda repo: {})
    r = diag._check_wearable_battery(_Repo())
    assert r["status"] == "info"
