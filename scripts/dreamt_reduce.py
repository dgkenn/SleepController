#!/usr/bin/env python3
"""Reduce PhysioNet DREAMT (v2.2.0) 64 Hz wearable CSVs to the staging trainer's inputs.

DREAMT is 100 overnight polysomnographies with a wrist Empatica E4 recorded alongside
(Duke, IRB Pro00108961). Each ``data_64Hz/<ID>_whole_df.csv`` row is one 64 Hz sample with
BVP, ACC_X/Y/Z (1/64 g, 32 Hz repeated), TEMP, EDA, HR (1 Hz), IBI (ms, present only on the
row where a beat occurred) and Sleep_Stage (P, W, N1, N2, N3, R, Missing; one label per
30 s repeated per row). A night is ~150 MB, so every file is streamed row by row.

Outputs, per participant, in the SAME text formats ``sleepctl.ml.sleep_staging.dataset``
already parses for the PhysioNet sleep-accel corpus:

    <ID>_heartrate.txt          t_seconds,bpm            one HR sample per second
    <ID>_labeled_sleep.txt      t_seconds stage          30 s epochs; -1 unscored, 0 wake,
                                                         1 N1, 2 N2, 3 N3, 5 REM (P = wake)
    <ID>_ibi.txt                t_seconds,ibi_ms         beat time and interval
    activity/<ID>_activity.txt  epoch_start_s,pim,zcm,mad,std,pmax,n
                                                         actigraphy counts over 30 s epochs,
                                                         definitions from scripts/polar_pmd.py

Usage:
    python3 scripts/dreamt_reduce.py --data-dir /path/to/dreamt/2.2.0 --out /path/to/reduced
    python3 scripts/dreamt_reduce.py --out /path/to/reduced --verify

The raw and the reduced data are covered by the PhysioNet Credentialed Health Data Use
Agreement: keep both OUT of the repository. Only trained weight files may be committed.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import statistics
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polar_pmd as pmd  # noqa: E402  (scripts/polar_pmd.py: actigraphy count definitions)

EPOCH_S = 30.0
STAGE_CODES = {"W": 0, "P": 0, "N1": 1, "N2": 2, "N3": 3, "R": 5, "REM": 5, "MISSING": -1}
#: DREAMT documents IBI in ms; some exports carry seconds. Below this median the unit is s.
IBI_SECONDS_IF_MEDIAN_BELOW = 5.0
ACC_UNITS_PER_G = 64.0
#: DREAMT rows are 64 Hz; the E4 accelerometer is 32 Hz, each sample repeated on two rows.
ROW_HZ = 64.0
ACC_SOURCE_HZ = 32.0


def _find_col(fieldnames, *cands) -> Optional[str]:
    low = {f.lower().strip(): f for f in fieldnames if f is not None}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    for c in cands:
        for k, f in low.items():
            if k.startswith(c.lower()):
                return f
    return None


def _f(v) -> Optional[float]:
    """A finite float, or None for blanks, "nan"/"none"/"null", junk and infinities. float()
    already strips whitespace and rejects the words, so this is one call on the hot path."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def participant_id(path: str) -> str:
    base = os.path.basename(path.split(ZIP_SEP, 1)[-1])
    return base.split("_")[0].split(".")[0]


#: Separator between a ZIP archive and a member inside it (``dreamt.zip!dreamt/data_64Hz/S002_whole_df.csv``).
ZIP_SEP = "!"


def _open_text(path: str):
    """Open a participant CSV for streaming -- a plain file, or a member of the PhysioNet ZIP.

    The DREAMT ZIP is 20 GB compressed and 113.7 GB unpacked; reading members straight out of
    it means the box never needs room for the unpacked copy (only ~15 GB of it is used)."""
    if ZIP_SEP in path and path.split(ZIP_SEP, 1)[0].lower().endswith(".zip"):
        import io
        import zipfile
        archive, member = path.split(ZIP_SEP, 1)
        zf = zipfile.ZipFile(archive)
        raw = zf.open(member, "r")
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        text._zf = zf            # keep the archive open for as long as the stream lives
        return text
    return open(path, "r", newline="")


def discover(data_dir: str) -> List[str]:
    """Every participant CSV under data_dir (data_64Hz/ preferred, else any *.csv).

    ``data_dir`` may also be the PhysioNet ZIP itself: its data_64Hz members are returned as
    ``<zip>!<member>`` paths and streamed without extracting."""
    if data_dir.lower().endswith(".zip") and os.path.isfile(data_dir):
        import zipfile
        with zipfile.ZipFile(data_dir) as zf:
            names = [n for n in zf.namelist()
                     if n.lower().endswith(".csv") and "/data_64hz/" in ("/" + n.lower())
                     and not os.path.basename(n).lower().startswith("participant_info")]
        return [f"{data_dir}{ZIP_SEP}{n}" for n in sorted(names)]
    cands = []
    for pat in ("data_64Hz/*.csv", "*/data_64Hz/*.csv", "*.csv"):
        cands = sorted(glob.glob(os.path.join(data_dir, pat)))
        cands = [c for c in cands if not os.path.basename(c).lower().startswith("participant_info")]
        if cands:
            break
    return cands


def reduce_file(path: str, out_dir: str, verbose: bool = True, stream=None) -> Dict[str, object]:
    """Stream one DREAMT CSV into the four reduced files. Returns a small summary.

    ``stream`` is an already-open text stream to read instead of opening ``path`` (the remote
    ZIP reader passes one); ``path`` still names the participant."""
    pid = participant_id(path)
    os.makedirs(os.path.join(out_dir, "activity"), exist_ok=True)
    t0 = time.time()
    n_rows = 0
    hr_rows: List[Tuple[float, float]] = []
    ibi_rows: List[Tuple[float, float]] = []
    labels: Dict[int, int] = {}                # epoch index -> code
    label_votes: Dict[int, Dict[int, int]] = {}
    acc_epoch: Dict[int, List[Tuple[float, float, float]]] = {}
    act_lines: List[str] = []
    last_hr_sec: Optional[int] = None
    last_acc_slot: Optional[int] = None
    cur_epoch: Optional[int] = None
    # Plain csv.reader with column indices (not DictReader) and lazy parsing: a column is only
    # converted to float when its value can matter -- HR once per second, ACC once per 32 Hz slot,
    # IBI only on the rows carrying a beat, the stage string only when it changes. Same output,
    # a fraction of the per-row work; memory stays one epoch of samples plus the 1 Hz series.
    with (stream if stream is not None else _open_text(path)) as fh:
        rd = csv.reader(fh)
        fn = next(rd, None) or []
        c_t = _find_col(fn, "TIMESTAMP", "timestamp", "time", "t")
        c_hr = _find_col(fn, "HR", "heart_rate", "heartrate")
        c_ibi = _find_col(fn, "IBI", "ibi", "rr")
        c_x, c_y, c_z = (_find_col(fn, "ACC_X", "acc_x", "accx"), _find_col(fn, "ACC_Y", "acc_y", "accy"),
                         _find_col(fn, "ACC_Z", "acc_z", "accz"))
        c_st = _find_col(fn, "Sleep_Stage", "sleep_stage", "stage", "label")
        if c_t is None or c_st is None:
            raise ValueError(f"{path}: no TIMESTAMP / Sleep_Stage column among {fn[:12]}")
        i_t, i_st = fn.index(c_t), fn.index(c_st)
        i_hr = fn.index(c_hr) if c_hr is not None else None
        i_ibi = fn.index(c_ibi) if c_ibi is not None else None
        i_acc = ((fn.index(c_x), fn.index(c_y), fn.index(c_z))
                 if c_x is not None and c_y is not None and c_z is not None else None)
        width = len(fn)
        acc_div = int(ROW_HZ / ACC_SOURCE_HZ)

        def _flush_epoch(k: int) -> None:
            trip = acc_epoch.pop(k, None)
            if not trip or len(trip) < pmd.MIN_EPOCH_SAMPLES:
                return
            c = pmd.actigraphy_counts(trip)
            act_lines.append(f"{k * EPOCH_S:.0f},{c['pim']},{c['zcm']},{c['mad']},{c['std']},{c['pmax']},{c['n']}")

        last_st_raw: Optional[str] = None
        last_code = -1
        votes: Optional[Dict[int, int]] = None
        for row in rd:
            if not row:                           # blank line: DictReader skipped these too
                continue
            n_rows += 1
            if len(row) < width:                  # a short row: pad like DictReader did
                row = row + [""] * (width - len(row))
            t = _f(row[i_t])
            if t is None:
                continue
            k = int(t // EPOCH_S)
            if cur_epoch is None:
                cur_epoch = k
            elif k != cur_epoch:
                # epochs are contiguous in time; flush everything older than the new one
                for old in [e for e in acc_epoch if e < k]:
                    _flush_epoch(old)
                cur_epoch = k
                votes = None
            # label: majority of the rows in the epoch (they are constant per epoch anyway)
            st_raw = row[i_st]
            if st_raw != last_st_raw:
                last_st_raw = st_raw
                st = st_raw.strip().upper()
                last_code = (STAGE_CODES.get(st, STAGE_CODES.get(st.replace(" ", ""), -1))
                             if st else None)
            if last_code is not None:
                if votes is None:
                    votes = label_votes.setdefault(k, {})
                votes[last_code] = votes.get(last_code, 0) + 1
            # HR: one sample per second
            if i_hr is not None:
                sec = int(t)
                if sec != last_hr_sec:
                    hr = _f(row[i_hr])
                    if hr is not None and 25.0 <= hr <= 220.0:
                        hr_rows.append((float(sec), hr))
                        last_hr_sec = sec
            # IBI: only rows carrying a beat
            if i_ibi is not None:
                v = row[i_ibi]
                if v:
                    ibi = _f(v)
                    if ibi is not None and ibi > 0:
                        ibi_rows.append((t, ibi))
            # ACC: 32 Hz values repeated at 64 Hz -> keep ONE row per 32 Hz sample slot.
            #
            # BUG FIXED (audit 2026-09-25): this used to drop a row whenever its value equalled
            # the previous one. A still wrist reads the SAME quantised value (1/64 g) for
            # minutes, so a motionless epoch collapsed to a single sample, fell under
            # MIN_EPOCH_SAMPLES and got no activity line at all -- the stillest epochs of every
            # night, i.e. deep sleep, vanished from the training counts instead of reading
            # pim=0. The slot comes from the source TIMESTAMP (the 64 Hz row index, halved), so
            # the dedupe no longer depends on what the value is.
            if i_acc is not None:
                slot = int(round(t * ROW_HZ)) // acc_div
                if slot != last_acc_slot:
                    x, y, z = _f(row[i_acc[0]]), _f(row[i_acc[1]]), _f(row[i_acc[2]])
                    if x is not None and y is not None and z is not None:
                        trip = (x / ACC_UNITS_PER_G, y / ACC_UNITS_PER_G, z / ACC_UNITS_PER_G)
                        acc_epoch.setdefault(k, []).append(trip)
                        last_acc_slot = slot
        for old in sorted(acc_epoch):
            _flush_epoch(old)

    for k, votes in label_votes.items():
        labels[k] = max(votes.items(), key=lambda kv: kv[1])[0]
    # IBI unit guard
    if ibi_rows:
        med = statistics.median(v for _t, v in ibi_rows)
        if med < IBI_SECONDS_IF_MEDIAN_BELOW:
            ibi_rows = [(t, v * 1000.0) for t, v in ibi_rows]
        ibi_rows = [(t, v) for t, v in ibi_rows if 300.0 <= v <= 2000.0]

    with open(os.path.join(out_dir, f"{pid}_heartrate.txt"), "w") as fh:
        fh.write("".join(f"{t:.0f},{v:.2f}\n" for t, v in hr_rows))
    with open(os.path.join(out_dir, f"{pid}_labeled_sleep.txt"), "w") as fh:
        fh.write("".join(f"{k * EPOCH_S:.0f} {labels[k]}\n" for k in sorted(labels)))
    with open(os.path.join(out_dir, "activity", f"{pid}_activity.txt"), "w") as fh:
        fh.write("# epoch_start_s,pim,zcm,mad,std,pmax,n  (DREAMT E4 wrist, g units)\n")
        fh.write("\n".join(act_lines) + ("\n" if act_lines else ""))
    # written LAST: the pipeline treats <ID>_ibi.txt as "this participant is done", so an
    # interrupted reduce never leaves a participant that looks complete but is missing a file
    with open(os.path.join(out_dir, f"{pid}_ibi.txt"), "w") as fh:
        fh.write("".join(f"{t:.3f},{v:.1f}\n" for t, v in ibi_rows))
    dist: Dict[int, int] = {}
    for c in labels.values():
        dist[c] = dist.get(c, 0) + 1
    summ = {"id": pid, "rows": n_rows, "hr": len(hr_rows), "ibi": len(ibi_rows),
            "epochs": len(labels), "activity_epochs": len(act_lines), "labels": dist,
            "seconds": round(time.time() - t0, 1)}
    if verbose:
        print(f"  {pid}: {n_rows} rows -> {len(labels)} epochs, {len(hr_rows)} HR, "
              f"{len(ibi_rows)} IBI, {len(act_lines)} activity epochs, labels {dist} "
              f"[{summ['seconds']}s]", flush=True)
    return summ


def verify(out_dir: str) -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from sleepctl.ml.sleep_staging.dataset import discover_subjects, _parse_labels, _parse_pairs
    sids = discover_subjects(out_dir)
    print(f"{len(sids)} participants in {out_dir}")
    names = {0: "wake", 1: "N1", 2: "N2", 3: "N3", 5: "REM", -1: "unscored"}
    for sid in sids:
        labels = _parse_labels(os.path.join(out_dir, f"{sid}_labeled_sleep.txt"))
        ibi = _parse_pairs(os.path.join(out_dir, f"{sid}_ibi.txt"))
        hr = _parse_pairs(os.path.join(out_dir, f"{sid}_heartrate.txt"))
        dist: Dict[str, int] = {}
        for _t, c in labels:
            dist[names.get(c, str(c))] = dist.get(names.get(c, str(c)), 0) + 1
        print(f"  {sid}: {len(labels)} epochs, {len(hr)} HR, {len(ibi)} IBI, {dist}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="folder holding data_64Hz/ (or the CSVs themselves), "
                                       "or the PhysioNet dreamt ZIP itself (streamed, never unpacked)")
    ap.add_argument("--out", required=True, help="reduced output folder (keep OUT of the repo)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--participants", nargs="*", default=None, help="IDs to reduce (default all)")
    ap.add_argument("--verify", action="store_true", help="only print what the reduced folder holds")
    ap.add_argument("--skip-existing", action="store_true", help="skip IDs already reduced")
    args = ap.parse_args(argv)
    if args.verify:
        return verify(args.out)
    if not args.data_dir:
        ap.error("--data-dir is required unless --verify")
    files = discover(args.data_dir)
    if args.participants:
        want = set(args.participants)
        files = [f for f in files if participant_id(f) in want]
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no participant CSVs found under {args.data_dir}")
        return 1
    os.makedirs(args.out, exist_ok=True)
    print(f"reducing {len(files)} participant file(s) -> {args.out}", flush=True)
    done = 0
    for f in files:
        pid = participant_id(f)
        if args.skip_existing and os.path.exists(os.path.join(args.out, f"{pid}_ibi.txt")):
            print(f"  {pid}: already reduced, skipping", flush=True)
            continue
        try:
            reduce_file(f, args.out)
            done += 1
        except Exception as exc:
            print(f"  {pid}: FAILED {exc!r}", flush=True)
    print(f"done: {done} reduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
