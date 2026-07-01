"""Meta-learning confidence ledger + contradiction-check tests.

Covers: the ledger aggregates cleanly against a bare (empty) repo (resilience — no learner
failure should sink the whole thing), picks up real signal once nights/actions are seeded, and
the contradiction detector flags genuinely opposing same-phase temperature nudges while staying
quiet on small/preset/cross-phase differences.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.learning.coordinator import (
    build_ledger_report,
    detect_contradictions,
    learning_ledger,
)
from sleepctl.models import ActionRecord, NightSummary, SetpointProfile
from sleepctl.storage.repository import Repository


def test_ledger_resilient_on_empty_repo():
    """A brand-new repo (no nights, no learner tables populated) must not raise, and every
    entry returned must carry the full uniform shape."""
    repo = Repository(":memory:")
    cfg = AppConfig.default()
    entries = learning_ledger(repo, cfg)
    assert isinstance(entries, list)
    assert len(entries) > 0   # onset/settle/lead_time/wake* report even with zero history
    for e in entries:
        for key in ("name", "phase", "value", "unit", "source", "maturity", "confidence", "note"):
            assert key in e
        assert e["phase"] in ("onset", "maintenance", "wake", "thermal")
        assert e["source"] in ("preset", "learned", "measured")
        assert 0.0 <= e["confidence"] <= 1.0


def test_ledger_one_bad_learner_does_not_sink_others(monkeypatch):
    """Simulate a single gatherer raising (e.g. a schema/import problem) and confirm the ledger
    still returns entries from the rest."""
    import sleepctl.learning.coordinator as coord

    def _boom(repo, cfg):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(coord, "_gather_onset", _boom)
    repo = Repository(":memory:")
    entries = learning_ledger(repo, AppConfig.default())
    names = [e["name"] for e in entries]
    assert "onset.warm_nudge" not in names
    assert len(entries) > 0  # other learners still reported


def test_ledger_picks_up_setpoints_and_baselines():
    """Once setpoints + baselines exist, the ledger should surface them with real maturity."""
    repo = Repository(":memory:")
    cfg = AppConfig.default()

    sp = SetpointProfile(neutral_f=69.0, deep_bias_f=65.0, rem_warm_offset_f=1.5,
                         wake_ramp_f=74.0, composite_bed_weight=0.6, version=1,
                         source="policy", updated=datetime.now())
    repo.save_setpoints(sp)

    base = datetime(2026, 1, 1)
    for i in range(20):
        n = NightSummary(date=(base + timedelta(days=i)).date().isoformat(),
                         bedtime=base + timedelta(days=i, hours=22),
                         wake_time=base + timedelta(days=i + 1, hours=6),
                         total_sleep_min=420, deep_min=80, rem_min=90,
                         wake_events=2, sleep_efficiency=0.9, avg_hrv=55,
                         sleep_onset_latency_min=15, waso_min=20)
        repo.save_night_summary(n)
    from sleepctl.learning.baselines import BaselineEngine
    repo.save_baselines(BaselineEngine().update(repo.recent_nights(20)))

    entries = learning_ledger(repo, cfg)
    by_name = {e["name"]: e for e in entries}
    assert "setpoints.neutral" in by_name
    assert by_name["setpoints.neutral"]["value"] == 69.0
    assert by_name["setpoints.neutral"]["maturity"] == 20
    assert "baselines.wake_events_median" in by_name
    assert by_name["baselines.wake_events_median"]["source"] == "measured"


def test_build_ledger_report_shape():
    repo = Repository(":memory:")
    report = build_ledger_report(repo, AppConfig.default())
    d = report.to_dict()
    assert set(d.keys()) == {"entries", "contradictions"}
    assert isinstance(d["entries"], list) and isinstance(d["contradictions"], list)


# ------------------------------------------------------------------- contradiction detection

def _e(name, phase, value, unit="f", source="learned"):
    return {"name": name, "phase": phase, "value": value, "unit": unit, "source": source,
            "maturity": 10, "confidence": 0.5, "note": ""}


def test_contradiction_flags_opposing_same_phase_temps():
    entries = [
        _e("a.warm", "maintenance", 1.2),
        _e("b.cool", "maintenance", -1.0),
    ]
    warnings = detect_contradictions(entries)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["phase"] == "maintenance"
    assert {w["a"], w["b"]} == {"a.warm", "b.cool"}
    assert w["combined_spread_f"] == 2.2


def test_contradiction_quiet_on_small_combined_spread():
    entries = [
        _e("c.warm_tiny", "wake", 0.2),
        _e("d.cool_tiny", "wake", -0.3),
    ]
    assert detect_contradictions(entries) == []


def test_contradiction_quiet_on_preset_entries():
    """Only entries that have actually moved off the preset ('learned') are compared — two
    presets disagreeing isn't a real contradiction, it's just two different defaults."""
    entries = [
        _e("a.warm", "maintenance", 3.0, source="preset"),
        _e("b.cool", "maintenance", -3.0, source="preset"),
    ]
    assert detect_contradictions(entries) == []


def test_contradiction_quiet_across_phases():
    """Opposing nudges in DIFFERENT phases (e.g. onset warms, wake cools) are not a
    contradiction -- they govern different parts of the night."""
    entries = [
        _e("onset.warm", "onset", 2.0),
        _e("wake.cool", "wake", -2.0),
    ]
    assert detect_contradictions(entries) == []


def test_contradiction_quiet_on_non_temperature_units():
    entries = [
        _e("wake.window", "wake", 30.0, unit="min"),
        _e("wake.other_window", "wake", -30.0, unit="min"),
    ]
    assert detect_contradictions(entries) == []


def test_contradiction_message_is_advisory_only():
    entries = [
        _e("a.warm", "maintenance", 2.0),
        _e("b.cool", "maintenance", -2.0),
    ]
    w = detect_contradictions(entries)[0]
    assert "advisory" in w["message"].lower()
    assert "no automatic override" in w["message"].lower()
