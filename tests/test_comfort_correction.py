"""The stored comfort band excluded the user's own best-sleep temperature.

`observed_night_2026-08-04_v2` -- neutral 66.9 / cool 65.5 / warm 68.0 -- was derived, by its
own ratings caveat, from a single night. The same blob records best sleep at 69 F water (160
min unbroken), cold-end awakenings at 63-64 F and warm-end awakenings at 70-72 F. 69 F is above
the band's ceiling and therefore unreachable.

Every in-maintenance command is clamped to this band, so on 2026-08-27 the water sat at 64-67 F
all night, pinned at the floor for two thirds of maintenance ticks across 16 wake events, and
pre-emption tried to warm 57 times without ever getting above 67 F.
"""
import json

from sleepctl.controller.comfort_correction import (
    BEST_SLEEP_F, CORRECTED, SUPERSEDED_SOURCE, apply_comfort_correction,
    corrected_profile, needs_correction)
from sleepctl.storage.repository import Repository

SUPERSEDED = {"neutral_f": 66.9, "cool_edge_f": 65.5, "warm_edge_f": 68.0,
              "source": SUPERSEDED_SOURCE,
              "ratings": json.dumps({"evidence": {"best_sleep_median_level": "-55 (69F water)"}})}


def test_the_superseded_band_is_recognised():
    assert needs_correction(SUPERSEDED) is True


def test_the_corrected_band_can_actually_reach_the_best_sleep_temperature():
    """The entire point: 69 F must be inside the band."""
    assert CORRECTED["cool_edge_f"] <= BEST_SLEEP_F <= CORRECTED["warm_edge_f"]


def test_the_corrected_band_clears_both_recorded_awakening_zones():
    """Cold awakenings at 63-64 F, warm at 70-72 F. Keep clearance from both."""
    assert CORRECTED["cool_edge_f"] >= 66.0
    assert CORRECTED["warm_edge_f"] <= 70.0


def test_a_different_band_is_never_touched():
    """Surgical by construction: only the one superseded profile is replaced."""
    other = dict(SUPERSEDED, source="comfort_sweep_2026-09-01")
    assert needs_correction(other) is False


def test_a_band_that_already_reaches_best_sleep_is_left_alone():
    """Self-cancelling: if the stored band can already reach 69 F there is nothing to fix."""
    ok = dict(SUPERSEDED, warm_edge_f=70.0)
    assert needs_correction(ok) is False


def test_a_missing_or_empty_profile_is_not_corrected():
    assert needs_correction(None) is False
    assert needs_correction({}) is False


def test_the_original_band_is_preserved_for_audit():
    out = corrected_profile(SUPERSEDED)
    sup = out["ratings"]["superseded"]
    assert sup["cool_edge_f"] == 65.5 and sup["warm_edge_f"] == 68.0
    assert sup["source"] == SUPERSEDED_SOURCE
    assert "correction_reason" in out["ratings"]


def test_the_original_evidence_survives_the_correction():
    out = corrected_profile(SUPERSEDED)
    assert "evidence" in out["ratings"]


def test_applying_it_persists_and_is_idempotent():
    repo = Repository(":memory:")
    repo.save_comfort_profile(SUPERSEDED)
    first = apply_comfort_correction(repo)
    assert first is not None and first["cool_edge_f"] == CORRECTED["cool_edge_f"]
    stored = repo.get_comfort_profile()
    assert stored["warm_edge_f"] == CORRECTED["warm_edge_f"]
    assert apply_comfort_correction(repo) is None, "must not re-apply"


def test_nothing_happens_when_no_profile_exists():
    assert apply_comfort_correction(Repository(":memory:")) is None


def test_a_broken_repo_never_raises():
    class Broken:
        def get_comfort_profile(self):
            raise RuntimeError("no table")
    assert apply_comfort_correction(Broken()) is None
