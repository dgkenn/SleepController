"""The daemon link that arms accelerometer wake detection.

``_actigraphy_wake`` REFUSES to fire unless ``frame.activity_units == "counts"`` -- the armband's
PIM and the iPhone's 0..1 index differ by ~17x on real data, so an absolute motion threshold is
meaningless without knowing which one is in play. That units tag is set in exactly one place:
``LiveDaemon._read_frame`` copying it off the dense history dict. Everything downstream of it was
already covered; that copy was not, and if it silently stops happening the accelerometer -- the
best wake signal we have, 6/6 vs the HR stager's 2/6 -- goes dark with no error anywhere.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

from sleepctl.config import AppConfig                       # noqa: E402
from sleepctl.loop.live import SimulatedLiveClient          # noqa: E402
from sleepctl.storage.repository import Repository          # noqa: E402

import live_daemon                                          # noqa: E402


def _daemon(history: dict | None, *, sample=True):
    start = datetime(2026, 8, 28, 23, 0)

    class Wearable:
        def read_sample(self):
            if not sample:
                return None
            from sleepctl.models import WearableSample
            return WearableSample(timestamp=datetime.now(), heart_rate=58.0,
                                  hrv=45.0, movement=0.1, age_seconds=2.0)

        def read_history(self, minutes: float = 45.0):
            if history is None:
                raise RuntimeError("bridge unavailable")
            return history

    return live_daemon.LiveDashboardDaemon(
        AppConfig.default(), SimulatedLiveClient(scenario="normal", seed=3, start=start),
        Repository(":memory:"), dry_run=True, verbose=False, wearable=Wearable())


def _hist(units, activity=((1.0, 7.5), (2.0, 9.0))):
    return {"hr": [(1.0, 58.0), (2.0, 57.0)], "activity": list(activity),
            "activity_units": units, "excluded": 0}


def test_armband_counts_reach_the_frame_so_the_wake_detector_can_arm():
    frame = _daemon(_hist("counts"))._read_frame()
    assert frame.activity_units == "counts"
    assert frame.activity_history and frame.activity_history[-1][1] == 9.0
    assert frame.hr_history


def test_phone_index_is_labelled_as_such_and_not_passed_off_as_counts():
    frame = _daemon(_hist("phone_index"))._read_frame()
    assert frame.activity_units == "phone_index"


def test_a_history_failure_leaves_units_unset_rather_than_defaulting_to_counts():
    """Guessing "counts" here would let a phone-scale series trip an armband-scale threshold on
    every quiet minute. Unknown must stay unknown."""
    frame = _daemon(None)._read_frame()
    assert getattr(frame, "activity_units", None) is None
    assert not getattr(frame, "activity_history", None)


def test_a_units_tag_never_survives_without_the_series_it_describes():
    frame = _daemon(_hist("counts", activity=()))._read_frame()
    assert not getattr(frame, "activity_history", None)
    assert getattr(frame, "activity_units", None) is None


def test_the_units_tag_is_carried_end_to_end_into_a_wake_decision():
    """The point of the tag: the same motion series arms the detector as armband counts and is
    correctly refused as a phone index."""
    from sleepctl.controller.state_estimator import _actigraphy_wake

    busy = [(float(i), 12.0) for i in range(1, 40)]
    counts = _daemon(_hist("counts", activity=busy))._read_frame()
    phone = _daemon(_hist("phone_index", activity=busy))._read_frame()
    cfg = AppConfig.default()
    assert _actigraphy_wake(counts, cfg) is True
    assert _actigraphy_wake(phone, cfg) is not True


def test_bridge_source_history_failure_returns_the_full_shape():
    """``BridgeWearableSource.read_history`` degrades to empty series on any failure; it must keep
    the same KEYS as the success path so a caller reading ``activity_units`` sees None, not a
    KeyError or a missing key it has to guess about."""
    from sleepctl.adapters.bcg import BridgeWearableSource

    class BadRepo:
        conn = None

    out = BridgeWearableSource(BadRepo()).read_history()
    assert out["hr"] == [] and out["activity"] == []
    assert out["activity_units"] is None
