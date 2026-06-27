"""Real-time wearable fusion: a separate fast sensor overlays the Pod frame (zero device risk)."""

from datetime import datetime

from sleepctl.adapters.base import PodSensorSource
from sleepctl.adapters.wearable import (FusedPodSensorSource, SimulatedWearableSource,
                                        WearableSample)
from sleepctl.models import NightSummary, SensorFrame, SleepStage


class _StubPod(PodSensorSource):
    """Pod source with slow (~60s) data."""

    def read_frame(self) -> SensorFrame:
        return SensorFrame(timestamp=datetime(2026, 6, 27, 3, 0), stage=SleepStage.LIGHT,
                           heart_rate=58.0, hrv=45.0, movement=0.05, presence=True,
                           bed_temp_f=70.0, data_age_seconds=58.0)

    def fetch_night_summary(self, date: str) -> NightSummary:
        return NightSummary(date=date)

    def capabilities(self) -> dict:
        return {"cooling": True}


def test_fresh_wearable_overrides_hr_and_movement_and_freshness():
    fused = FusedPodSensorSource(
        _StubPod(),
        SimulatedWearableSource(fixed=WearableSample(
            timestamp=datetime(2026, 6, 27, 3, 0), heart_rate=66.0, movement=0.4,
            age_seconds=2.0)))
    f = fused.read_frame()
    assert f.heart_rate == 66.0          # fast wearable HR wins
    assert f.movement == 0.4             # sub-second movement (the key fast precursor)
    assert f.data_age_seconds == 2.0     # frame is now as fresh as the wearable, not 58s
    assert fused.last_fused is True


def test_stale_wearable_is_ignored():
    fused = FusedPodSensorSource(
        _StubPod(),
        SimulatedWearableSource(fixed=WearableSample(
            timestamp=datetime(2026, 6, 27, 3, 0), heart_rate=99.0, age_seconds=120.0)),
        max_sample_age_s=30.0)
    f = fused.read_frame()
    assert f.heart_rate == 58.0          # stale sample rejected -> Pod value kept
    assert f.data_age_seconds == 58.0
    assert fused.last_fused is False


def test_no_wearable_sample_falls_back_to_pod():
    fused = FusedPodSensorSource(_StubPod(), SimulatedWearableSource(fixed=None))
    f = fused.read_frame()
    assert f.heart_rate == 58.0 and f.movement == 0.05
    assert fused.last_fused is False


def test_fusion_is_a_drop_in_source():
    fused = FusedPodSensorSource(_StubPod(), SimulatedWearableSource())
    assert fused.fetch_night_summary("2026-06-27").date == "2026-06-27"
    caps = fused.capabilities()
    assert caps["cooling"] is True and caps["wearable_fusion"] is True
