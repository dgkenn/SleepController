#!/usr/bin/env python3
"""Polar Verity Sense -> SleepController bridge (zero device risk to the Pod).

Reads a dedicated BLE Heart Rate sensor -- a **Polar Verity Sense** armband (or any standard
0x180D Heart Rate Service device, incl. a Polar H10 chest strap) -- and forwards its heart rate
plus beat-to-beat RR intervals to the dashboard's ``/hr/ingest`` endpoint. The API computes HRV
(RMSSD) from the RR intervals and MERGES this authoritative cardiac signal with the iPhone
accelerometer's movement (``/bcg/ingest``) into a single fused frame the controller consumes.

The Verity is a SEPARATE device: nothing here ever touches, modifies, or risks the Eight Sleep
Pod. This is the physiology path that works even when the Pod's own sleep-tracking is unavailable
(e.g. no Eight Sleep membership).

Runs unattended with an auto-reconnect loop; the watchdog can launch it (set SLEEPCTL_VERITY=1 in
deploy\\.env). Run it by hand any time:

    python scripts/verity_forwarder.py                       # auto-discover a Polar sensor
    python scripts/verity_forwarder.py --address AA:BB:...    # pin a specific device
    python scripts/verity_forwarder.py --url http://localhost:8000/hr/ingest --token <TOKEN>

Requires ``bleak`` (``pip install bleak``) and a Bluetooth adapter. On Windows the Verity must
first be paired/available to the OS; put the armband in HR broadcast mode (single press -> the
LED shows the Bluetooth/HR mode).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Standard BLE Heart Rate Measurement characteristic (GATT 0x2A37).
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
# Names we'll auto-match when scanning (case-insensitive substring).
_NAME_HINTS = ("polar", "verity", "sense", "h10", "oh1")


def _repo_root() -> Path:
    here = Path(__file__).resolve().parent.parent
    return here


def _load_env(root: Path) -> dict:
    """Parse deploy\\.env into a dict (same KEY=VALUE style as the PowerShell scripts). Missing
    file -> empty dict. Used only to pick up BCG_INGEST_TOKEN and a URL override; never printed."""
    out: dict[str, str] = {}
    env_path = root / "deploy" / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def _parse_hr_measurement(data: bytearray) -> tuple[int | None, list[float]]:
    """Decode a Heart Rate Measurement notification into (hr_bpm, [rr_ms, ...]) per the GATT spec.

    flags byte: bit0 = HR value format (0: uint8, 1: uint16); bit3 = Energy Expended present;
    bit4 = RR-Interval(s) present. RR intervals are uint16 little-endian in units of 1/1024 s.
    """
    if not data:
        return None, []
    flags = data[0]
    idx = 1
    hr: int | None = None
    if flags & 0x01:  # 16-bit HR
        if len(data) >= idx + 2:
            hr = int.from_bytes(data[idx:idx + 2], "little")
            idx += 2
    else:  # 8-bit HR
        if len(data) >= idx + 1:
            hr = data[idx]
            idx += 1
    if flags & 0x08:  # Energy Expended present (uint16) -> skip it
        idx += 2
    rr_ms: list[float] = []
    if flags & 0x10:  # RR intervals present
        while idx + 1 < len(data):
            raw = int.from_bytes(data[idx:idx + 2], "little")
            idx += 2
            rr_ms.append(raw * 1000.0 / 1024.0)  # 1/1024 s units -> milliseconds
    return hr, rr_ms


def _post(url: str, payload: dict, timeout: float = 5.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local URL)
        resp.read()


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


async def _discover(BleakScanner, address_hint: str | None):
    if address_hint:
        return address_hint
    _log("scanning for a Polar/BLE heart-rate sensor (10s)...")
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        name = (d.name or "").lower()
        if any(h in name for h in _NAME_HINTS):
            _log(f"found '{d.name}' at {d.address}")
            return d.address
    # Fall back to any device advertising the HR service, if the backend exposes it.
    for d in devices:
        try:
            uuids = [u.lower() for u in (d.metadata.get("uuids") or [])]
        except Exception:
            uuids = []
        if any("180d" in u for u in uuids):
            _log(f"found HR-service device '{d.name}' at {d.address}")
            return d.address
    return None


async def _run_once(args, env) -> None:
    from bleak import BleakClient, BleakScanner  # lazy: only needed at runtime

    address = await _discover(BleakScanner, args.address)
    if not address:
        _log("no Polar/HR sensor found this scan; will retry")
        await asyncio.sleep(args.retry_seconds)
        return

    # Coalesce notifications into small batches so we POST a few times a second, not per-beat.
    batch_rr: list[float] = []
    last_hr: dict = {"v": None}
    last_flush = {"t": time.monotonic()}

    def _on_hr(_handle, data: bytearray) -> None:
        hr, rr = _parse_hr_measurement(data)
        if hr is not None:
            last_hr["v"] = hr
        if rr:
            batch_rr.extend(rr)

    async def _flusher(client) -> None:
        while client.is_connected:
            await asyncio.sleep(args.batch_seconds)
            hr = last_hr["v"]
            rr = batch_rr[:]
            batch_rr.clear()
            if hr is None and not rr:
                continue
            payload = {"source": args.source}
            if hr is not None:
                payload["hr"] = float(hr)
            if rr:
                payload["rr"] = rr
            try:
                _post(args.url, payload)
                last_flush["t"] = time.monotonic()
            except Exception as exc:  # network blip -> drop this batch, keep streaming
                _log(f"POST failed ({exc}); dropping batch")

    _log(f"connecting to {address} ...")
    async with BleakClient(address) as client:
        _log(f"connected; subscribing to HR notifications; forwarding to {args.url}")
        await client.start_notify(HR_MEASUREMENT_UUID, _on_hr)
        try:
            await _flusher(client)
        finally:
            try:
                await client.stop_notify(HR_MEASUREMENT_UUID)
            except Exception:
                pass
    _log("disconnected")


async def _main_async(args, env) -> None:
    while True:
        try:
            await _run_once(args, env)
        except Exception as exc:
            _log(f"session error ({exc}); reconnecting in {args.retry_seconds}s")
            await asyncio.sleep(args.retry_seconds)


def main(argv=None) -> int:
    root = _repo_root()
    env = _load_env(root)
    token = os.environ.get("BCG_INGEST_TOKEN") or env.get("BCG_INGEST_TOKEN", "")

    p = argparse.ArgumentParser(description="Forward a Polar Verity Sense (BLE HR) to /hr/ingest")
    p.add_argument("--address", default=os.environ.get("SLEEPCTL_VERITY_ADDRESS"),
                   help="BLE MAC/address of the sensor (skip auto-discovery)")
    p.add_argument("--url", default=None, help="ingest URL (default localhost API + token)")
    p.add_argument("--token", default=token, help="BCG_INGEST_TOKEN (defaults from env/deploy\\.env)")
    p.add_argument("--source", default="verity", help="source tag stored with the samples")
    p.add_argument("--batch-seconds", type=float, default=2.0, help="POST cadence")
    p.add_argument("--retry-seconds", type=float, default=10.0, help="reconnect backoff")
    args = p.parse_args(argv)

    if not args.url:
        base = os.environ.get("SLEEPCTL_HR_URL", "http://localhost:8000/hr/ingest")
        args.url = base + (f"?token={args.token}" if args.token else "")

    try:
        import bleak  # noqa: F401
    except Exception:
        _log("ERROR: 'bleak' is not installed. Run:  pip install bleak")
        return 2

    _log(f"Polar Verity forwarder starting (source={args.source})")
    try:
        asyncio.run(_main_async(args, env))
    except KeyboardInterrupt:
        _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
