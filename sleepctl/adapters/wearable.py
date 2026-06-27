"""Real-time wearable fusion — the GUARANTEED zero-device-risk path to fast physiology.

The Eight Sleep cloud floor is ~60s for vitals (even a rooted Pod only derives HR/HRV per
minute). A separate sensor — a BLE chest strap / wrist HR + accelerometer, or a bedside
non-contact radar — gives **sub-second movement and faster HR** with ZERO risk to the Pod
(it is a different device; the Pod is never touched or modified).

``FusedPodSensorSource`` wraps the existing Pod source and OVERLAYS a fresher wearable sample
(HR / movement / HRV) onto the frame the controller already consumes — so the precursor /
wake-risk detectors see fast movement and HR with no controller changes. When the wearable is
absent or its sample is stale, it transparently falls back to the Pod frame.

Movement is the highest-value fast signal for awakening pre-emption (it is the immediate
arousal precursor the 60s cloud bins away). A concrete BLE reader is provided as a lazily
-imported, documented adapter; the simulated source backs the tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sleepctl.adapters.base import PodSensorSource
from sleepctl.models import NightSummary, SensorFrame


@dataclass
class WearableSample:
    """One fast sample from a separate (non-Pod) sensor."""

    timestamp: datetime
    heart_rate: Optional[float] = None
    hrv: Optional[float] = None
    movement: Optional[float] = None      # 0..1 restlessness/motion index (sub-second source)
    age_seconds: Optional[float] = None    # freshness of this sample


class RealtimeWearableSource(ABC):
    """A separate fast sensor (BLE strap / bedside radar). Distinct from the daytime-context
    ``WearableSource`` in base.py — this one streams live samples for fusion."""

    @abstractmethod
    def read_sample(self) -> Optional[WearableSample]:
        """Return the freshest wearable sample, or None if unavailable."""


class SimulatedWearableSource(RealtimeWearableSource):
    """Scripted source for tests/demo. ``samples`` is consumed one per read; a single
    ``fixed`` sample is returned every read when no script is given."""

    def __init__(self, samples=None, fixed: Optional[WearableSample] = None) -> None:
        self._samples = list(samples or [])
        self._fixed = fixed

    def read_sample(self) -> Optional[WearableSample]:
        if self._samples:
            return self._samples.pop(0)
        return self._fixed


class FusedPodSensorSource(PodSensorSource):
    """Drop-in ``PodSensorSource`` that overlays a fresher wearable sample onto the Pod frame.

    Zero device risk: the wearable is a separate device and the Pod source is untouched. The
    controller consumes this exactly like the bare Pod source — fusion is invisible upstream.
    """

    def __init__(self, pod: PodSensorSource, wearable: RealtimeWearableSource,
                 max_sample_age_s: float = 30.0) -> None:
        self.pod = pod
        self.wearable = wearable
        self.max_sample_age_s = max_sample_age_s
        self.last_fused = False  # whether the most recent frame used wearable data

    def read_frame(self) -> SensorFrame:
        frame = self.pod.read_frame()
        self.last_fused = False
        sample = None
        try:
            sample = self.wearable.read_sample()
        except Exception:
            sample = None
        if sample is None:
            return frame
        age = sample.age_seconds
        # Only trust a sample we know is fresh enough; unknown age -> don't override.
        if age is None or age > self.max_sample_age_s:
            return frame
        if sample.heart_rate is not None:
            frame.heart_rate = sample.heart_rate
        if sample.movement is not None:
            frame.movement = sample.movement
        if sample.hrv is not None:
            frame.hrv = sample.hrv
        # The fused physiology is as fresh as the wearable (sub-minute), not the ~60s Pod data.
        prior = frame.data_age_seconds
        frame.data_age_seconds = age if prior is None else min(prior, age)
        self.last_fused = True
        return frame

    # Non-frame reads delegate to the wrapped Pod source (it owns night summaries + caps).
    def fetch_night_summary(self, date: str) -> NightSummary:
        return self.pod.fetch_night_summary(date)

    def capabilities(self) -> dict:
        caps = dict(self.pod.capabilities() or {})
        caps["wearable_fusion"] = True
        caps["wearable_max_sample_age_s"] = self.max_sample_age_s
        return caps


class BLEHeartRateSource(RealtimeWearableSource):
    """Reads a standard Bluetooth LE Heart Rate Service (0x180D) device — a $20 chest strap or
    most wrist HR sensors — via ``bleak`` (lazy-imported so the package needs no BLE stack to
    import). Real hardware; not exercised by the offline tests.

    Notify on characteristic 0x2A37 (Heart Rate Measurement) gives ~1 Hz HR; many straps also
    expose RR-intervals there for beat-to-beat HRV. Pair an accelerometer characteristic (or a
    motion-capable sensor) to drive the sub-second ``movement`` index — the key fast precursor.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        self._client = None
        self._last: Optional[WearableSample] = None

    async def connect(self) -> None:  # pragma: no cover - requires BLE hardware
        from bleak import BleakClient  # lazy: only needed for real hardware

        self._client = BleakClient(self.address)
        await self._client.connect()

        def _on_hr(_handle, data: bytearray) -> None:
            # Heart Rate Measurement format (Bluetooth GATT): flags byte then HR (8/16-bit).
            from datetime import datetime as _dt

            flags = data[0]
            hr = data[1] if not (flags & 0x01) else int.from_bytes(data[1:3], "little")
            self._last = WearableSample(timestamp=_dt.now(), heart_rate=float(hr),
                                        age_seconds=0.0)

        await self._client.start_notify("00002a37-0000-1000-8000-00805f9b34fb", _on_hr)

    def read_sample(self) -> Optional[WearableSample]:  # pragma: no cover - hardware
        return self._last

    async def close(self) -> None:  # pragma: no cover - requires BLE hardware
        if self._client is not None:
            await self._client.stop_notify("00002a37-0000-1000-8000-00805f9b34fb")
            await self._client.disconnect()
