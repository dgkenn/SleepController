"""Tier 1 (NON-INVASIVE) raw-capture source.

The Pod's firmware ("Frank") uploads its own raw sensor stream to
``raw-api-upload.8slp.net``. By placing the hub behind a controlled gateway and
DNS-redirecting that host to a local capture server (with a TLS proxy), we can ingest
that stream at higher resolution than the cloud API exposes — WITHOUT modifying the
device. Nothing on the Pod changes; removing the DNS override instantly restores normal
cloud operation, so this carries NO bricking risk and is fully reversible.

Go/no-go for this tier is purely TLS certificate pinning (see recon/mitm_probe.md). The
decoder for the proprietary upload payload is a documented TODO that depends on the
pinning result. This class reads already-decoded frames that the capture server appends
to a local JSONL queue file, mapping them into the same SensorFrame contract the
controller consumes — so enabling Tier 1 requires zero controller changes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

from sleepctl.adapters.base import PodSensorSource
from sleepctl.models import NightSummary, SensorFrame, SleepStage


class RawCaptureSource(PodSensorSource):
    def __init__(self, queue_path: str = "raw_capture.jsonl") -> None:
        self.queue_path = queue_path
        self._offset = 0

    def _read_last_record(self) -> Optional[dict]:
        if not os.path.exists(self.queue_path):
            return None
        last = None
        with open(self.queue_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        return json.loads(last) if last else None

    def read_frame(self) -> SensorFrame:
        rec = self._read_last_record()
        if rec is None:
            # No capture yet -> a maximally-stale frame so the controller holds.
            return SensorFrame(timestamp=datetime.now(), data_age_seconds=1e9)
        ts = datetime.fromisoformat(rec["timestamp"])
        return SensorFrame(
            timestamp=ts,
            stage=SleepStage(rec.get("stage", "unknown")),
            stage_confidence=rec.get("stage_confidence"),
            heart_rate=rec.get("heart_rate"),
            hrv=rec.get("hrv"),
            respiratory_rate=rec.get("respiratory_rate"),
            movement=rec.get("movement"),
            presence=rec.get("presence"),
            bed_temp_f=rec.get("bed_temp_f"),
            room_temp_f=rec.get("room_temp_f"),
            commanded_level=rec.get("commanded_level"),
            data_age_seconds=(datetime.now() - ts).total_seconds(),
        )

    def fetch_night_summary(self, date: str) -> NightSummary:
        # Best-effort summary would be derived from the captured stream; left minimal.
        return NightSummary(date=date)

    def capabilities(self) -> dict:
        return {
            "source": "raw_capture",
            "invasive": False,
            "reversible": True,
            "blocked_by": "tls_certificate_pinning (see recon/mitm_probe.md)",
            "note": "Decoder for the raw upload payload is a TODO pending the pinning test.",
        }
