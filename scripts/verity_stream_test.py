#!/usr/bin/env python3
"""Stream EVERY channel the Verity offers for a fixed window, then report exactly what arrived.

The question this answers is "is all the information actually streaming?", which the forwarder
cannot answer for you — it is built to run silently for eight hours and stay out of the way, so a
stream that never starts looks the same as one that is merely quiet between batches.

This connects once, starts every stream the armband will give us, listens for a fixed window, and
prints a per-stream report: did it start, how many frames/samples arrived, at what rate, and a few
real values. Nothing is written to the database and no device command is ever sent to the Pod —
it is purely a listening test.

    python scripts/verity_stream_test.py                 # 60s, auto-discover
    python scripts/verity_stream_test.py --seconds 120   # longer (PPI needs ~25s to warm up)
    python scripts/verity_stream_test.py --address AA:BB:CC:DD:EE:FF

Wear the armband on your upper forearm and single-press until the LED shows the Bluetooth/HR mode
BEFORE running this — a device sitting on a desk streams HR happily and tells you nothing about
whether it works on a body.

The output ends with a PASTE-READY block. That block is the whole point: it contains no personal
identifiers, just stream names, counts, rates and a handful of sample values, so it can be handed
to someone helping you debug.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polar_pmd as pmd  # noqa: E402

HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
_NAME_HINTS = ("polar", "verity", "sense", "h10", "oh1")


class Stream:
    """One channel's tally."""

    def __init__(self, key, label, why):
        self.key, self.label, self.why = key, label, why
        self.started = None          # True / False / None (never attempted)
        self.frames = 0
        self.samples = 0
        self.first_at = None
        self.last_at = None
        self.examples = []
        self.note = ""

    def hit(self, n_samples=1, example=None):
        now = time.monotonic()
        self.frames += 1
        self.samples += n_samples
        self.first_at = self.first_at or now
        self.last_at = now
        if example is not None and len(self.examples) < 5:
            self.examples.append(example)

    def rate_hz(self):
        if self.first_at is None or self.last_at is None or self.last_at <= self.first_at:
            return None
        return self.samples / (self.last_at - self.first_at)


def _log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


async def _discover(BleakScanner, hint):
    if hint:
        _log(f"connecting to pinned address {hint}")
        return hint
    _log("scanning for a Polar device (10s)...")
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        name = (getattr(d, "name", "") or "").lower()
        if any(h in name for h in _NAME_HINTS):
            _log(f"found {d.name} @ {d.address}")
            return d.address
    return None


async def run(args) -> int:
    try:
        from bleak import BleakClient, BleakScanner
    except Exception:
        print("ERROR: 'bleak' is not installed. Run:  .venv\\Scripts\\python.exe -m pip install bleak")
        return 2

    streams = {
        "hr": Stream("hr", "Heart rate (0x180D)",
                     "the authoritative cardiac signal — onset, arousal, wake-risk, staging"),
        "rr": Stream("rr", "RR intervals (0x180D)",
                     "beat-to-beat variability -> HRV; the irreplaceable training data"),
        "acc": Stream("acc", "Accelerometer (PMD)",
                      "actigraphy WITHOUT the phone — motion for onset + arousal"),
        "ppi": Stream("ppi", "Pulse-to-pulse intervals (PMD)",
                      "Polar's own beat intervals with a per-beat error estimate"),
    }

    address = await _discover(BleakScanner, args.address)
    if address is None:
        print("\nNo Polar device found. Check: armband ON (single press -> blue LED), worn,")
        print("in range, and not already connected to the phone app (it holds the link).")
        return 1

    def on_hr(_h, data: bytearray):
        try:
            hr, rr = _parse_hr(data)
        except Exception:
            return
        if hr is not None:
            streams["hr"].hit(1, example=hr)
        if rr:
            streams["rr"].hit(len(rr), example=round(rr[0], 1))

    def on_pmd(_h, data: bytearray):
        try:
            mtype = pmd.frame_measurement_type(data)
        except Exception:
            return
        try:
            if mtype == pmd.MEAS_ACC:
                _ts, _res, samples = pmd.parse_acc_frame(data)
                if samples:
                    mags = pmd.acc_magnitudes_g(samples)
                    streams["acc"].hit(len(samples),
                                       example=round(mags[0], 3) if mags else None)
            elif mtype == pmd.MEAS_PPI:
                _ts, samples = pmd.parse_ppi_frame(data)
                good = pmd.usable_ppi(samples)
                if samples:
                    streams["ppi"].hit(len(samples),
                                       example=round(good[0], 1) if good else None)
        except pmd.PmdParseError:
            pass
        except Exception:
            pass

    _log(f"connecting to {address} ...")
    async with BleakClient(address, timeout=20.0) as client:
        _log("connected")

        # --- generic HR service -----------------------------------------------------------
        try:
            await client.start_notify(HR_MEASUREMENT_UUID, on_hr)
            streams["hr"].started = streams["rr"].started = True
            _log("HR service: subscribed")
        except Exception as exc:
            streams["hr"].started = streams["rr"].started = False
            streams["hr"].note = streams["rr"].note = f"subscribe failed: {exc}"
            _log(f"HR service: FAILED ({exc})")

        # --- Polar PMD: ACC + PPI ---------------------------------------------------------
        pmd_ok = False
        try:
            await client.start_notify(pmd.PMD_DATA_UUID, on_pmd)
            pmd_ok = True
            _log("PMD data: subscribed")
        except Exception as exc:
            for k in ("acc", "ppi"):
                streams[k].started = False
                streams[k].note = f"PMD service unavailable: {exc}"
            _log(f"PMD data: unavailable ({exc})")

        if pmd_ok:
            for key, meas, settings, label in (
                ("acc", pmd.MEAS_ACC,
                 {"sample_rate": args.acc_rate, "resolution": args.acc_resolution,
                  "range": args.acc_range, "channels": 3}, "ACC"),
                ("ppi", pmd.MEAS_PPI, None, "PPI"),
            ):
                try:
                    await client.write_gatt_char(
                        pmd.PMD_CONTROL_UUID, pmd.build_start_command(meas, settings), response=True)
                    streams[key].started = True
                    _log(f"PMD {label}: start accepted")
                except Exception as exc:
                    streams[key].started = False
                    streams[key].note = f"start refused: {exc}"
                    _log(f"PMD {label}: start REFUSED ({exc})")

        _log(f"listening for {args.seconds:.0f}s — keep the armband ON and worn...")
        remaining = float(args.seconds)
        while remaining > 0:
            await asyncio.sleep(min(10.0, remaining))
            remaining -= 10.0
            got = ", ".join(f"{s.label.split(' (')[0]}={s.samples}" for s in streams.values())
            _log(f"  ...{got}")

        for key, meas in (("acc", pmd.MEAS_ACC), ("ppi", pmd.MEAS_PPI)):
            if streams[key].started:
                try:
                    await client.write_gatt_char(
                        pmd.PMD_CONTROL_UUID, pmd.build_stop_command(meas), response=True)
                except Exception:
                    pass
        try:
            await client.stop_notify(HR_MEASUREMENT_UUID)
        except Exception:
            pass

    return _report(streams, args)


def _parse_hr(data: bytearray):
    """GATT Heart Rate Measurement -> (bpm, [rr_ms]). Same decode the forwarder uses."""
    if not data:
        return None, []
    flags, idx, hr = data[0], 1, None
    if flags & 0x01:
        if len(data) >= idx + 2:
            hr = int.from_bytes(data[idx:idx + 2], "little")
            idx += 2
    else:
        if len(data) >= idx + 1:
            hr = data[idx]
            idx += 1
    if flags & 0x08:
        idx += 2
    rr = []
    if flags & 0x10:
        while idx + 1 < len(data):
            rr.append(int.from_bytes(data[idx:idx + 2], "little") * 1000.0 / 1024.0)
            idx += 2
    return hr, rr


def _report(streams, args) -> int:
    print()
    print("=" * 78)
    print("  VERITY STREAM TEST")
    print("=" * 78)
    missing = []
    for s in streams.values():
        if s.started is False:
            state = "REFUSED"
        elif s.samples > 0:
            state = "STREAMING"
        elif s.started:
            state = "SILENT"
        else:
            state = "NOT TRIED"
        if state != "STREAMING":
            missing.append(s)
        rate = s.rate_hz()
        print()
        print(f"  {s.label:<28} {state}")
        print(f"    role     : {s.why}")
        print(f"    received : {s.samples} samples in {s.frames} frames"
              + (f", ~{rate:.1f}/s" if rate else ""))
        if s.examples:
            print(f"    examples : {s.examples}")
        if s.note:
            print(f"    note     : {s.note}")

    print()
    print("=" * 78)
    if not missing:
        print("  ALL FOUR STREAMS DELIVERED DATA. The armband is fully working.")
    else:
        print(f"  {len(missing)} stream(s) delivered nothing:")
        for s in missing:
            print(f"    - {s.label}")
        if streams["ppi"].samples == 0 and streams["ppi"].started:
            print()
            print("  PPI silence: Polar documents ~25s to the FIRST batch, so a short test can")
            print(f"  end before it arrives (this run listened {args.seconds:.0f}s). If it stays")
            print("  silent past ~60s: " + pmd.SDK_MODE_REMEDY)
        if streams["acc"].started is False:
            print()
            print("  ACC refused: some firmware only allows one PMD stream at a time. Re-run with")
            print("  --no-ppi to test ACC alone.")
    print("=" * 78)

    # -------- paste-ready summary (no identifiers, just counts) --------
    print()
    print("----- PASTE THIS BACK -----")
    print(f"verity-stream-test seconds={args.seconds:.0f}")
    for s in streams.values():
        rate = s.rate_hz()
        print(f"  {s.key}: started={s.started} frames={s.frames} samples={s.samples} "
              f"rate={f'{rate:.2f}/s' if rate else 'n/a'} "
              f"examples={s.examples} note={s.note or '-'}")
    print("----- END -----")
    print()
    return 0 if not missing else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seconds", type=float, default=60.0,
                   help="listen window (PPI needs ~25s before its first batch; 60s is a good "
                        "default, 120s is conclusive)")
    p.add_argument("--address", default=None, help="pin a BLE address instead of scanning")
    p.add_argument("--acc-rate", type=int, default=52)
    p.add_argument("--acc-range", type=int, default=8)
    p.add_argument("--acc-resolution", type=int, default=16)
    args = p.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nstopped")
        return 1
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        print("If this is a connection error: make sure the Polar app on your phone is NOT")
        print("connected to the armband — it holds an exclusive link.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
