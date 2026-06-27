"""Independent BCG sensor processing — beat-to-beat HR/HRV + sub-second movement, fused in."""

from datetime import datetime

from sleepctl.adapters.bcg import BCGProcessor, BCGWearableSource, synthesize_bcg
from sleepctl.adapters.wearable import FusedPodSensorSource
from sleepctl.adapters.base import PodSensorSource
from sleepctl.models import NightSummary, SensorFrame, SleepStage


def test_recovers_heart_rate_from_synthetic_bcg():
    fs = 100.0
    proc = BCGProcessor(fs=fs)
    proc.ingest(synthesize_bcg(fs=fs, secs=20, bpm=60))
    v = proc.vitals()
    assert v is not None
    assert abs(v["hr"] - 60) <= 6          # beat-to-beat HR recovered from the raw waveform
    assert v["hrv"] is not None            # HRV (RMSSD) computed from beat intervals


def test_movement_burst_is_flagged_subsecond():
    fs = 100.0
    calm = BCGProcessor(fs=fs); calm.ingest(synthesize_bcg(fs=fs, secs=20, bpm=60))
    moving = BCGProcessor(fs=fs)
    moving.ingest(synthesize_bcg(fs=fs, secs=20, bpm=60, move_window=(15, 20)))
    assert moving.vitals()["movement"] > calm.vitals()["movement"]


def test_short_window_returns_none():
    proc = BCGProcessor(fs=100.0)
    proc.ingest([0.0] * 100)               # 1 s — too short to trust
    assert proc.vitals() is None


class _StubPod(PodSensorSource):
    def read_frame(self):
        return SensorFrame(timestamp=datetime(2026, 6, 27, 3, 0), stage=SleepStage.LIGHT,
                           heart_rate=58.0, movement=0.05, presence=True, data_age_seconds=58.0)

    def fetch_night_summary(self, date):
        return NightSummary(date=date)

    def capabilities(self):
        return {}


def test_bcg_source_fuses_onto_the_pod_frame():
    proc = BCGProcessor(fs=100.0)
    proc.ingest(synthesize_bcg(fs=100.0, secs=20, bpm=72))
    fused = FusedPodSensorSource(_StubPod(), BCGWearableSource(proc), max_sample_age_s=30.0)
    f = fused.read_frame()
    assert fused.last_fused is True
    assert abs(f.heart_rate - 72) <= 6     # the independent sensor's beat-to-beat HR wins
    assert f.data_age_seconds == 0.0       # sub-minute freshness, not the Pod's ~60s
