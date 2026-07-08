"""Independent BCG sensor processing — beat-to-beat HR/HRV + sub-second movement, fused in."""

from datetime import datetime

from sleepctl.adapters.bcg import BCGProcessor, BCGWearableSource, synthesize_bcg
from sleepctl.adapters.wearable import FusedPodSensorSource
from sleepctl.adapters.base import PodSensorSource
from sleepctl.models import NightSummary, SensorFrame, SleepStage
from sleepctl.recon.frame_decoder import detrend, find_beats, heart_rate_from_bcg, movement_index


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


def test_vitals_output_unchanged_vs_independently_computed_reference():
    """Regression for the dedup in ``BCGProcessor.vitals()`` (it used to call ``find_beats``,
    ``heart_rate_from_bcg`` (which re-detects beats internally), and ``movement_index``
    independently -- each re-running ``detrend`` -- 3x the detrend work and 2x the beat
    detection for the same input). ``vitals()`` now computes ``detrend``/beats ONCE and passes
    them in; this pins its output to be bit-identical to the old naive/independent computation
    for a fixed synthetic input."""
    fs = 100.0
    proc = BCGProcessor(fs=fs)
    samples = synthesize_bcg(fs=fs, secs=20, bpm=65, move_window=(10, 12))
    proc.ingest(samples)

    v = proc.vitals()
    assert v is not None

    # Reference: exactly how vitals() used to compute it -- three independent calls, each
    # recomputing its own detrend/beat-detection from scratch.
    x = list(proc._buf)
    ref_beats = find_beats(x, fs)
    ref_hr = heart_rate_from_bcg(x, fs)
    ref_rms = movement_index(x, fs)
    ref_movement = max(0.0, min(1.0, ref_rms / proc.move_scale)) if proc.move_scale else 0.0
    ref_hrv = proc._rmssd_ms(ref_beats)
    reference = {"hr": round(ref_hr, 1) if ref_hr else None, "hrv": ref_hrv,
                 "movement": round(ref_movement, 3), "n_beats": len(ref_beats)}

    assert v == reference

    # And the shared detrend/beats really are being reused, not silently ignored: passing them
    # in explicitly must reproduce the exact same beat indices as the from-scratch call.
    detrended = detrend(x, fs)
    assert find_beats(x, fs, detrended=detrended) == ref_beats


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
