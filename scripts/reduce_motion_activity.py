#!/usr/bin/env python3
"""Reduce the PhysioNet sleep-accel RAW accelerometer files to per-epoch ACTIGRAPHY COUNTS.

The dataset ships two motion signals:
  * ``steps/``  -- Apple Watch step counts. ~96% zeros overnight, so it carries almost no
                   information about sleep (this is why the v1 model's HR+motion variant was
                   barely better than HR-only).
  * ``motion/`` -- the RAW triaxial accelerometer (~50 Hz, g units). ~45 MB per subject. This is
                   the signal real actigraphy is computed from, and it is what actually separates
                   wake from sleep.

This script streams each subject: download the raw file -> reduce it to small per-30s-epoch
activity counts -> DELETE the raw file. Peak disk use is therefore one raw file (~45 MB), not the
~1.4 GB full set, and the reduced output is a few hundred KB per subject.

Counts computed per epoch (all standard actigraphy summaries of the gravity-removed magnitude):
  pim   -- Proportional Integral Mode: sum |a - mean| (overall movement energy)
  zcm   -- Zero Crossing Mode: crossings of a small threshold (movement frequency)
  mad   -- Mean Amplitude Deviation
  std   -- standard deviation of magnitude
  pmax  -- peak absolute deviation
  n     -- raw samples in the epoch (data-quality guard)

Output: ``<out_dir>/<ID>_activity.txt``, one line per epoch:
    epoch_start_s,pim,zcm,mad,std,pmax,n

Usage:
    python scripts/reduce_motion_activity.py                 # all subjects
    python scripts/reduce_motion_activity.py --subjects 46343 759667
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import urllib.request

BASE_URL = "https://physionet.org/files/sleep-accel/1.0.0"
DEFAULT_OUT = os.path.join(
    "/tmp/claude-0/-home-user-SleepController/e6ce5980-b2d3-50b8-a237-9df8d193f1a3",
    "scratchpad", "sleep_accel", "activity",
)

SUBJECTS = [
    "1066528", "1360686", "1449548", "1455390", "1818471", "2598705", "2638030",
    "3509524", "3997827", "4018081", "4314139", "4426783", "46343", "5132496",
    "5383425", "5498603", "5797046", "6220552", "759667", "7749105", "781756",
    "8000685", "8173033", "8258170", "844359", "8530312", "8686948", "8692923",
    "9106476", "9618981", "9961348",
]

EPOCH_S = 30.0
ZCM_THRESHOLD = 0.01  # g, deviation magnitude counted as a "crossing"


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def _download(url: str, dest: str, timeout: float = 900.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"  download failed: {exc}")
        return False


def reduce_file(path: str) -> "dict[int, list]":
    """Stream the raw accel file and accumulate per-epoch magnitude statistics.

    Two passes are avoided by accumulating sums; gravity is removed per-epoch by subtracting that
    epoch's own mean magnitude (a per-epoch high-pass that is robust to wrist orientation).
    """
    # epoch -> [n, sum, sumsq, list-free running stats]; we need mean before deviations, so we
    # accumulate magnitudes per epoch in a compact list (30 s at ~50 Hz = ~1500 floats/epoch).
    buckets: "dict[int, list]" = {}
    with open(path, "r") as fh:
        for line in fh:
            # format: "t x y z" (space-separated in this dataset; tolerate commas too)
            parts = line.replace(",", " ").split()
            if len(parts) < 4:
                continue
            try:
                t = float(parts[0])
                x = float(parts[1]); y = float(parts[2]); z = float(parts[3])
            except ValueError:
                continue
            mag = math.sqrt(x * x + y * y + z * z)
            e = int(math.floor(t / EPOCH_S) * EPOCH_S)
            b = buckets.get(e)
            if b is None:
                buckets[e] = [mag]
            else:
                b.append(mag)
    out: "dict[int, list]" = {}
    for e, mags in buckets.items():
        n = len(mags)
        if n < 5:
            continue
        mean = sum(mags) / n
        devs = [m - mean for m in mags]
        abs_devs = [abs(d) for d in devs]
        pim = sum(abs_devs)
        mad = pim / n
        var = sum(d * d for d in devs) / n
        std = math.sqrt(var)
        pmax = max(abs_devs)
        zcm = 0
        above = abs_devs[0] > ZCM_THRESHOLD
        for a in abs_devs[1:]:
            now_above = a > ZCM_THRESHOLD
            if now_above != above:
                zcm += 1
                above = now_above
        out[e] = [round(pim, 5), zcm, round(mad, 6), round(std, 6), round(pmax, 5), n]
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--subjects", nargs="*", default=None)
    p.add_argument("--keep-raw", action="store_true", help="don't delete the raw file after reducing")
    p.add_argument("--tmp-dir", default="/tmp")
    args = p.parse_args(argv)

    subjects = args.subjects or SUBJECTS
    os.makedirs(args.out_dir, exist_ok=True)

    done = 0
    for i, sid in enumerate(subjects, 1):
        out_path = os.path.join(args.out_dir, f"{sid}_activity.txt")
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            _log(f"[{i}/{len(subjects)}] {sid}: already reduced, skipping")
            done += 1
            continue
        raw = os.path.join(args.tmp_dir, f"_accel_{sid}.txt")
        url = f"{BASE_URL}/motion/{sid}_acceleration.txt"
        _log(f"[{i}/{len(subjects)}] {sid}: downloading")
        if not _download(url, raw):
            continue
        try:
            size_mb = os.path.getsize(raw) / 1e6
            _log(f"  reducing {size_mb:.0f} MB")
            epochs = reduce_file(raw)
            with open(out_path, "w") as fh:
                for e in sorted(epochs):
                    pim, zcm, mad, std, pmax, n = epochs[e]
                    fh.write(f"{e},{pim},{zcm},{mad},{std},{pmax},{n}\n")
            _log(f"  wrote {len(epochs)} epochs -> {out_path}")
            done += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"  reduce failed: {exc}")
        finally:
            if not args.keep_raw:
                try:
                    os.remove(raw)
                except OSError:
                    pass
    _log(f"done: {done}/{len(subjects)} subjects reduced into {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
