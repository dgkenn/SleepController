#!/usr/bin/env python3
"""Download the PhysioNet "sleep-accel" (Walch et al. 2019) small text files.

For each subject we fetch three small text files:
  - heart_rate/<ID>_heartrate.txt     "<t_seconds>,<bpm>"
  - steps/<ID>_steps.txt              "<t_seconds>,<count>"
  - labels/<ID>_labeled_sleep.txt     "<t_seconds> <stage>"  (30 s epochs)

The big raw accelerometer files under motion/ are intentionally skipped.

Data is written OUTSIDE the repo (default: the session scratchpad). Files already
present are skipped, so re-running is cheap. Pure standard library.

Usage:
    python scripts/fetch_sleep_accel.py [--out DIR] [--subjects ID ID ...]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

BASE_URL = "https://physionet.org/files/sleep-accel/1.0.0/"

SUBJECT_IDS = [
    "1066528", "1360686", "1449548", "1455390", "1818471", "2598705",
    "2638030", "3509524", "3997827", "4018081", "4314139", "4426783",
    "46343", "5132496", "5383425", "5498603", "5797046", "6220552",
    "759667", "7749105", "781756", "8000685", "8173033", "8258170",
    "844359", "8530312", "8686948", "8692923", "9106476", "9618981",
    "9961348",
]

DEFAULT_OUT = (
    "/tmp/claude-0/-home-user-SleepController/"
    "e6ce5980-b2d3-50b8-a237-9df8d193f1a3/scratchpad/sleep_accel"
)

# (subdir on server, filename suffix)
KINDS = [
    ("heart_rate", "_heartrate.txt"),
    ("steps", "_steps.txt"),
    ("labels", "_labeled_sleep.txt"),
]


def _download(url: str, dest: str, retries: int = 3) -> None:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"failed to download {url}: {last_err}")


def fetch(out_dir: str, subjects: list[str]) -> int:
    os.makedirs(out_dir, exist_ok=True)
    n_ok = 0
    n_skip = 0
    n_fail = 0
    total = len(subjects) * len(KINDS)
    done = 0
    for sid in subjects:
        for subdir, suffix in KINDS:
            done += 1
            fname = f"{sid}{suffix}"
            dest = os.path.join(out_dir, fname)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                n_skip += 1
                print(f"[{done}/{total}] skip  {fname}")
                continue
            url = f"{BASE_URL}{subdir}/{fname}"
            try:
                _download(url, dest)
                n_ok += 1
                print(f"[{done}/{total}] ok    {fname} ({os.path.getsize(dest)} bytes)")
            except Exception as exc:  # noqa: BLE001
                n_fail += 1
                print(f"[{done}/{total}] FAIL  {fname}: {exc}", file=sys.stderr)
    print(f"\nDone. downloaded={n_ok} skipped={n_skip} failed={n_fail} -> {out_dir}")
    return n_fail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=DEFAULT_OUT, help="target directory")
    ap.add_argument("--subjects", nargs="*", default=SUBJECT_IDS, help="subject IDs")
    args = ap.parse_args(argv)
    return fetch(args.out, args.subjects)


if __name__ == "__main__":
    raise SystemExit(main())
