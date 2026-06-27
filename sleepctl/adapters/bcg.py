"""Independent ballistocardiogram (BCG) sensor — the ZERO-DEVICE-RISK path to the raw signal.

The Pod's own piezo waveform can't be reached without rooting (TLS upload / local Frank socket
both need root). But the *same physiology* — the ballistocardiogram (heartbeat + respiration +
movement imparted to the bed) — can be captured by our OWN sensor that never touches the Pod, so
it cannot ruin it. A cheap option (load cells under the bed legs, a piezo strip, an accelerometer
on the mattress, or a non-contact radar) streams a high-rate signal; this processes it into the
beat-to-beat HR / HRV and the SUB-SECOND movement the 60s cloud bins away, then fuses it onto the
Pod frame via the existing ``FusedPodSensorSource`` hook — controller unchanged.

This is the software path: it makes any high-rate bed sensor plug in. Validated on a synthetic
BCG (recovers HR, computes HRV, flags movement bursts); a real sensor feeds ``BCGProcessor.ingest``.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import List, Optional

from sleepctl.adapters.wearable import RealtimeWearableSource, WearableSample
from sleepctl.recon.frame_decoder import find_beats, heart_rate_from_bcg, movement_index


class BCGProcessor:
    """Rolling-window processor for a raw bed BCG/piezo/accelerometer stream."""

    def __init__(self, fs: float = 100.0, window_s: float = 30.0, move_scale: float = 0.5) -> None:
        self.fs = float(fs)
        self.move_scale = move_scale          # RMS that maps to movement≈1.0 (sensor-specific)
        self._buf: deque = deque(maxlen=int(fs * window_s))

    def ingest(self, samples) -> None:
        """Append raw samples (a chunk from the sensor, in native units)."""
        self._buf.extend(float(s) for s in samples)

    def vitals(self) -> Optional[dict]:
        """Beat-to-beat HR (bpm), HRV (RMSSD, ms), and a 0..1 movement index, or None if the
        window is too short / too noisy to trust."""
        x = list(self._buf)
        if len(x) < int(self.fs * 5):         # need >=5 s to estimate a rate
            return None
        beats = find_beats(x, self.fs)
        hr = heart_rate_from_bcg(x, self.fs)
        rms = movement_index(x, self.fs)
        movement = max(0.0, min(1.0, rms / self.move_scale)) if self.move_scale else 0.0
        hrv = self._rmssd_ms(beats)
        return {"hr": round(hr, 1) if hr else None, "hrv": hrv,
                "movement": round(movement, 3), "n_beats": len(beats)}

    def _rmssd_ms(self, beats: List[int]) -> Optional[float]:
        if len(beats) < 4:
            return None
        ibis = [(beats[i] - beats[i - 1]) / self.fs for i in range(1, len(beats))]  # seconds
        diffs = [ibis[i] - ibis[i - 1] for i in range(1, len(ibis))]
        if not diffs:
            return None
        return round(1000.0 * math.sqrt(sum(d * d for d in diffs) / len(diffs)), 1)


class BCGWearableSource(RealtimeWearableSource):
    """Adapts a ``BCGProcessor`` to the wearable-fusion interface so an independent bed sensor's
    fast HR/HRV/movement overlays the Pod frame (``FusedPodSensorSource`` / the live daemon).
    Zero device risk — it's a separate sensor; the Pod is never touched."""

    def __init__(self, processor: BCGProcessor) -> None:
        self.processor = processor

    def read_sample(self) -> Optional[WearableSample]:
        v = self.processor.vitals()
        if v is None or v["hr"] is None:
            return None
        return WearableSample(timestamp=datetime.now(), heart_rate=v["hr"], hrv=v["hrv"],
                              movement=v["movement"], age_seconds=0.0)


def synthesize_bcg(fs: float = 100.0, secs: float = 20.0, bpm: float = 60.0,
                   move_window=None) -> List[float]:
    """Fabricate a plausible BCG for tests/demo: peaky heartbeat + slow respiration + optional
    gross-movement burst over ``move_window`` = (start_s, end_s)."""
    f_beat = bpm / 60.0
    out: List[float] = []
    n = int(fs * secs)
    for i in range(n):
        t = i / fs
        beat = math.sin(2 * math.pi * f_beat * t) ** 7          # sharp heartbeat ballistic
        resp = 0.2 * math.sin(2 * math.pi * 0.25 * t)            # ~15 breaths/min
        noise = 0.02 * math.sin(2 * math.pi * 31 * t)
        s = beat + resp + noise
        if move_window and move_window[0] <= t <= move_window[1]:
            s += 3.0 * math.sin(2 * math.pi * 7 * t)             # large gross-motion artifact
        out.append(s)
    return out
