#!/usr/bin/env python3
"""Stream-download and reduce the PhysioNet BIDSleep Apple Watch dataset (v1.0.1).

BIDSleep (https://physionet.org/content/bidsleep-dataset/1.0.1/, DOI 10.13026/rees-1092,
Open Data Commons Attribution License v1.0 -- open access, no login) is 253 free-living
nights from 47 subjects (``Bidslab00`` .. ``Bidslab68``, 3-7 nights each). Per night:

    motion.csv   Timestamp,x,y,z   Apple Watch accelerometer, Unix seconds, g units, ~50 Hz
    hr.csv       t,bpm (no header) HealthKit instantaneous HR, Unix seconds, ~0.2 Hz
    labels.mat   recStart (US/Eastern wall-clock string), dreem_label, expert_label
                 (30 s epochs from recStart; 0 W, 1 N1, 2 N2, 3 N3, 4 REM, 5 unknown)

The full set is ~28 GB, almost all of it motion.csv, so every night is streamed: its three
files are downloaded into ``<raw>/<ID>/``, checked against the published SHA256SUMS,
reduced, and DELETED before the worker takes the next night. Several nights are fetched
concurrently (PhysioNet serves ~150 KB/s per connection), so peak raw disk use is about
``--jobs`` nights (~0.1-0.25 GB each).

Outputs, one recording per night (``<ID>`` = ``Bidslab00_n1`` ...), in the SAME text formats
``sleepctl.ml.sleep_staging.dataset`` already parses for sleep-accel / DREAMT:

    <ID>_heartrate.txt          t_seconds,bpm        t relative to recStart (may be < 0)
    <ID>_labeled_sleep.txt      t_seconds stage      0 wake, 1 N1, 2 N2, 3 N3, 5 REM,
                                                     -1 unscored (BIDSleep 5 / missing)
    activity/<ID>_activity.txt  epoch_start_s,pim,zcm,mad,std,pmax,n
                                                     counts over 30 s epochs on the label
                                                     grid, definitions from polar_pmd.py
    <ID>_meta.json              label source (expert / dreem), recStart as Unix time,
                                sampling rates, stage counts, alignment offsets

Labels: ``expert_label`` is used whenever it holds at least one scored epoch; otherwise
``dreem_label`` (the automated Dreem 2 headband staging), and ``label_source`` in the meta
file records which.

Usage:
    python3 scripts/bidsleep_reduce.py --out DIR --no-motion --jobs 16   # HR + labels only
    python3 scripts/bidsleep_reduce.py --out DIR --skip-existing --jobs 20
    python3 scripts/bidsleep_reduce.py --out DIR --limit 3                # smoke test
    python3 scripts/bidsleep_reduce.py --out DIR --verify
    # throttled link: one 6.35 GB project ZIP instead of 27.9 GB of CSV, then reduce from it
    python3 scripts/bidsleep_reduce.py --download-zip Z.zip --zip-connections 16
    python3 scripts/bidsleep_reduce.py --out DIR --from-zip Z.zip --skip-existing --jobs 3

Attribution: BIDSleep Apple Watch Dataset v1.0.1, PhysioNet, ODC-By 1.0,
https://doi.org/10.13026/rees-1092. Keep raw and reduced data OUT of the repository; only
trained weight files and CV reports are committed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polar_pmd as pmd  # noqa: E402  (scripts/polar_pmd.py: actigraphy count definitions)

BASE_URL = "https://physionet.org/files/bidsleep-dataset/1.0.1"
SCRATCH = "/tmp/claude-0/-home-user-SleepController/e6ce5980-b2d3-50b8-a237-9df8d193f1a3/scratchpad"
DEFAULT_OUT = os.path.join(SCRATCH, "bidsleep", "reduced")
DEFAULT_RAW = os.path.join(SCRATCH, "bidsleep", "raw")
EPOCH_S = 30.0
TZ_NAME = "US/Eastern"
#: BIDSleep stage code -> trainer code (REM is 5 in the sleep-accel format, unknown -> -1)
STAGE_MAP = {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 5: -1}
HR_MIN, HR_MAX = 25.0, 220.0
#: flag a night whose signals start more than this far from recStart (timezone mix-up guard)
ALIGN_WARN_S = 3600.0


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


# --------------------------------------------------------------------------- listing
def fetch_manifest(raw_dir: str) -> Dict[str, str]:
    """``{"Bidslab00/1/hr.csv": sha256, ...}`` from the published SHA256SUMS (cached)."""
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, "SHA256SUMS.txt")
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        _curl(f"{BASE_URL}/SHA256SUMS.txt", path)
    out: Dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2:
                out[parts[1]] = parts[0]
    return out


def nights_from_manifest(manifest: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """Sorted ``(night_id, subject, night)`` for every folder holding labels.mat."""
    out = []
    for rel in manifest:
        parts = rel.split("/")
        if len(parts) == 3 and parts[2] == "labels.mat":
            subj, night = parts[0], parts[1]
            out.append((f"{subj}_n{night}", subj, night))
    return sorted(out, key=lambda x: (x[1], int(x[2]) if x[2].isdigit() else x[2]))


def subject_of(night_id: str) -> str:
    """``Bidslab00_n3`` -> ``Bidslab00`` (the cross-validation group)."""
    return night_id.split("_n")[0]


# --------------------------------------------------------------------------- download
def _curl(url: str, dest: str, attempts: int = 6) -> None:
    """Resumable download (``curl -C -``) with retries on dropped connections."""
    last = None
    for i in range(attempts):
        cmd = ["curl", "-sS", "-f", "-L", "-C", "-", "--retry", "5", "--retry-delay", "3",
               "--retry-all-errors", "--connect-timeout", "30", "-o", dest, url]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
        # 33 = range not satisfiable (file already complete); treat as done
        if r.returncode == 33:
            return
        last = r.stderr.strip()
        time.sleep(3 + 5 * i)
    raise RuntimeError(f"download failed {url}: {last}")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_verified(rel: str, dest: str, manifest: Dict[str, str]) -> None:
    want = manifest.get(rel)
    if want is not None and os.path.exists(dest) and _sha256(dest) == want:
        return  # already here (e.g. extracted from the project ZIP by --from-zip)
    for attempt in range(3):
        _curl(f"{BASE_URL}/{rel}", dest)
        if want is None or _sha256(dest) == want:
            return
        _log(f"  {rel}: checksum mismatch (attempt {attempt + 1}), refetching")
        os.remove(dest)
    raise RuntimeError(f"{rel}: checksum mismatch after retries")


# --------------------------------------------------------------------------- project ZIP
#: PhysioNet also serves the whole project as one ZIP -- 6.35 GB instead of 27.9 GB of CSV,
#: which is the faster route when a connection is throttled (~150 KB/s each here).
ZIP_URL = "https://physionet.org/content/bidsleep-dataset/get-zip/1.0.1/"
ZIP_CHUNK = 32 << 20


def download_zip(dest: str, connections: int = 16) -> None:
    """Fetch the project ZIP with parallel HTTP range requests (resumable via a state file)."""
    import threading
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    req = urllib.request.Request(ZIP_URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        size = int(r.headers["Content-Length"])
    state_path = dest + ".state.json"
    done: Dict[str, int] = {}
    if os.path.exists(state_path) and os.path.exists(dest):
        with open(state_path) as fh:
            done = json.load(fh)
    if not os.path.exists(dest):
        with open(dest, "wb") as fh:
            fh.truncate(size)
    lock = threading.Lock()
    chunks = [(o, min(size, o + ZIP_CHUNK) - 1) for o in range(0, size, ZIP_CHUNK)]
    todo = [c for c in chunks if done.get(str(c[0]), 0) < c[1] - c[0] + 1]
    _log(f"ZIP {size / 1e9:.2f} GB: {len(todo)}/{len(chunks)} chunks to fetch, "
         f"{connections} connections")
    fd = os.open(dest, os.O_WRONLY)
    t0 = time.time()
    got = [0]

    def fetch(chunk):
        lo, hi = chunk
        pos = lo + done.get(str(lo), 0)
        last: Optional[BaseException] = None
        for attempt in range(40):
            try:
                r = urllib.request.Request(ZIP_URL, headers={"Range": f"bytes={pos}-{hi}"})
                with urllib.request.urlopen(r, timeout=120) as resp:
                    if resp.status != 206:
                        raise RuntimeError(f"HTTP {resp.status} for a range request")
                    while pos <= hi:
                        block = resp.read(1 << 20)
                        if not block:
                            break
                        os.pwrite(fd, block, pos)
                        pos += len(block)
                        with lock:
                            done[str(lo)] = pos - lo
                            got[0] += len(block)
                if pos > hi:
                    return
            except Exception as exc:  # noqa: BLE001 -- dropped tunnels are routine; resume
                last = exc
            time.sleep(min(30, 2 + attempt))
        raise RuntimeError(f"chunk {lo}: gave up ({last!r})")

    def checkpoint():
        with lock:
            snap = dict(done)
        with open(state_path + ".tmp", "w") as fh:
            json.dump(snap, fh)
        os.replace(state_path + ".tmp", state_path)

    with ThreadPoolExecutor(connections) as pool:
        futs = [pool.submit(fetch, c) for c in todo]
        n_done = 0
        for f in futs:
            f.result()
            n_done += 1
            checkpoint()
            if n_done % 10 == 0 or n_done == len(futs):
                el = time.time() - t0
                _log(f"  {n_done}/{len(futs)} chunks, {got[0] / 1e9:.2f} GB in {el:.0f}s "
                     f"({got[0] / 1e6 / max(1, el):.1f} MB/s)")
    os.close(fd)
    checkpoint()
    _log(f"ZIP complete -> {dest}")


def extract_night_from_zip(zf, names: Dict[str, str], subj: str, night: str, raw: str,
                           want_motion: bool) -> None:
    """Copy one night's members out of the project ZIP into ``raw`` (checksummed later)."""
    os.makedirs(raw, exist_ok=True)
    for fname in ("labels.mat", "hr.csv") + (("motion.csv",) if want_motion else ()):
        member = names.get(f"{subj}/{night}/{fname}")
        if member is None:
            raise FileNotFoundError(f"{subj}/{night}/{fname} not in the ZIP")
        with zf.open(member) as src, open(os.path.join(raw, fname), "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)


def _zip_names(zf) -> Dict[str, str]:
    """``"Bidslab00/1/hr.csv" -> member name`` whatever top-level folder the ZIP uses."""
    out = {}
    for n in zf.namelist():
        parts = n.rstrip("/").split("/")
        for i, p in enumerate(parts):
            if p.startswith("Bidslab") and len(parts) - i == 3:
                out["/".join(parts[i:])] = n
    return out


def reduce_night_from_zip(task) -> Dict[str, object]:
    """Worker: extract one night from the ZIP, then reduce it exactly like a download."""
    import zipfile

    zip_path, inner = task[0], task[1:]
    nid, subj, night, out_dir, raw_root, _manifest, want_motion, _keep = inner
    try:
        with zipfile.ZipFile(zip_path) as zf:
            extract_night_from_zip(zf, _zip_names(zf), subj, night,
                                   os.path.join(raw_root, nid), want_motion)
    except Exception as exc:  # noqa: BLE001
        return dict(id=nid, ok=False, error=repr(exc), seconds=0.0)
    return reduce_night(inner)


# --------------------------------------------------------------------------- parsing
def rec_start_unix(value: str) -> float:
    """recStart (US/Eastern wall-clock string) -> Unix seconds."""
    from zoneinfo import ZoneInfo

    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%d-%b-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            naive = _dt.datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unrecognised recStart {s!r}")
    for name in (TZ_NAME, "America/New_York"):
        try:
            return naive.replace(tzinfo=ZoneInfo(name)).timestamp()
        except Exception:  # noqa: BLE001 -- no tz database: fall back to the US rule below
            continue
    return _eastern_fallback(naive)


def _eastern_fallback(naive: "_dt.datetime") -> float:
    """US/Eastern -> Unix without a tz database (post-2007 rule: EDT from the 2nd Sunday of
    March 02:00 to the 1st Sunday of November 02:00, else EST)."""
    def nth_sunday(year: int, month: int, n: int) -> _dt.datetime:
        d = _dt.datetime(year, month, 1)
        d += _dt.timedelta(days=(6 - d.weekday()) % 7)
        return d + _dt.timedelta(weeks=n - 1)
    y = naive.year
    dst = nth_sunday(y, 3, 2).replace(hour=2) <= naive < nth_sunday(y, 11, 1).replace(hour=2)
    offset_h = 4 if dst else 5
    return (naive + _dt.timedelta(hours=offset_h)).replace(tzinfo=_dt.timezone.utc).timestamp()


def _mat_str(v) -> str:
    import numpy as np

    a = np.asarray(v)
    if a.dtype.kind in ("U", "S"):
        return str(a.ravel()[0])
    if a.dtype.kind in ("u", "i") and a.size > 1:  # v7.3 char arrays come back as uint16
        return "".join(chr(int(c)) for c in a.ravel())
    return str(a.ravel()[0])


def load_labels(path: str) -> Dict[str, object]:
    """recStart + both label vectors (scipy for v5 MAT files, h5py for v7.3)."""
    import numpy as np

    try:
        import scipy.io as sio

        m = sio.loadmat(path)
        get = lambda k: m.get(k)  # noqa: E731
    except NotImplementedError:  # MATLAB v7.3 (HDF5)
        import h5py

        f = h5py.File(path, "r")
        get = lambda k: (f[k][()] if k in f else None)  # noqa: E731
    rec = get("recStart")
    if rec is None:
        raise ValueError(f"{path}: no recStart")
    out: Dict[str, object] = {"recStart": _mat_str(rec)}
    for k in ("expert_label", "dreem_label"):
        v = get(k)
        out[k] = [] if v is None else [int(x) for x in np.asarray(v).ravel()]
    return out


def choose_labels(lab: Dict[str, object]) -> Tuple[List[int], str]:
    """Expert labels when they hold any scored epoch, else Dreem; codes mapped to trainer's."""
    expert = list(lab.get("expert_label") or [])
    dreem = list(lab.get("dreem_label") or [])
    if expert and any(c in (0, 1, 2, 3, 4) for c in expert):
        src, raw = "expert", expert
    elif dreem and any(c in (0, 1, 2, 3, 4) for c in dreem):
        src, raw = "dreem", dreem
    else:
        return [], "none"
    return [STAGE_MAP.get(int(c), -1) for c in raw], src


def load_hr(path: str) -> List[Tuple[float, float]]:
    out = []
    with open(path) as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                t, v = float(parts[0]), float(parts[1])
            except ValueError:
                continue  # header or junk
            if math.isfinite(t) and math.isfinite(v) and HR_MIN <= v <= HR_MAX:
                out.append((t, v))
    out.sort()
    return out


def load_motion(path: str):
    """``(t, magnitude_g)`` numpy arrays from motion.csv, read in chunks (fast, low memory)."""
    import numpy as np

    ts: List["np.ndarray"] = []
    ms: List["np.ndarray"] = []
    with open(path, "rb") as fh:
        head = fh.readline()
        if head and not head[:1].isalpha() and head.strip():
            fh.seek(0)  # no header row
        tail = b""
        while True:
            block = fh.read(16 << 20)
            if not block:
                break
            block = tail + block
            cut = block.rfind(b"\n")
            if cut < 0:
                tail = block
                continue
            tail = block[cut + 1:]
            arr = _parse_rows(block[:cut])
            if arr.size:
                ts.append(arr[:, 0])
                ms.append(np.sqrt(arr[:, 1] ** 2 + arr[:, 2] ** 2 + arr[:, 3] ** 2))
        if tail.strip():
            arr = _parse_rows(tail)
            if arr.size:
                ts.append(arr[:, 0])
                ms.append(np.sqrt(arr[:, 1] ** 2 + arr[:, 2] ** 2 + arr[:, 3] ** 2))
    if not ts:
        return np.zeros(0), np.zeros(0)
    t = np.concatenate(ts)
    m = np.concatenate(ms)
    ok = np.isfinite(t) & np.isfinite(m)
    t, m = t[ok], m[ok]
    order = np.argsort(t, kind="stable")
    return t[order], m[order]


def _parse_rows(buf: bytes):
    import numpy as np

    txt = buf.decode("ascii", "replace").replace("\r", "")
    lines = [ln for ln in txt.split("\n") if ln.count(",") == 3]
    if not lines:
        return np.zeros((0, 4))
    try:
        flat = np.array(",".join(lines).split(","), dtype=float)
    except ValueError:  # a malformed row: fall back to a per-line parse
        rows = []
        for ln in lines:
            try:
                rows.append([float(x) for x in ln.split(",")])
            except ValueError:
                continue
        return np.asarray(rows, dtype=float).reshape(-1, 4)
    return flat.reshape(-1, 4)


def activity_lines(t_rel, mag) -> Tuple[List[str], float]:
    """Actigraphy counts per 30 s epoch (grid anchored at recStart) and the sample rate."""
    import numpy as np

    if len(t_rel) == 0:
        return [], 0.0
    d = np.diff(t_rel)
    d = d[(d > 0) & (d < 1.0)]
    fs = float(1.0 / np.median(d)) if d.size else 0.0
    k = np.floor(t_rel / EPOCH_S).astype(np.int64)
    bounds = np.flatnonzero(np.diff(k)) + 1
    starts = np.concatenate(([0], bounds))
    ends = np.concatenate((bounds, [len(k)]))
    lines = []
    for s, e in zip(starts, ends):
        if e - s < pmd.MIN_EPOCH_SAMPLES:
            continue
        c = pmd.actigraphy_counts(mag[s:e].tolist())
        lines.append(f"{int(k[s]) * EPOCH_S:.0f},{c['pim']},{c['zcm']},{c['mad']},"
                     f"{c['std']},{c['pmax']},{c['n']}")
    return lines, fs


# --------------------------------------------------------------------------- reduce
def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _paths(out_dir: str, nid: str) -> Dict[str, str]:
    return dict(hr=os.path.join(out_dir, f"{nid}_heartrate.txt"),
                labels=os.path.join(out_dir, f"{nid}_labeled_sleep.txt"),
                meta=os.path.join(out_dir, f"{nid}_meta.json"),
                act=os.path.join(out_dir, "activity", f"{nid}_activity.txt"))


def hr_done(out_dir: str, nid: str) -> bool:
    p = _paths(out_dir, nid)
    return all(os.path.exists(p[k]) for k in ("hr", "labels", "meta"))


def motion_done(out_dir: str, nid: str) -> bool:
    return os.path.exists(_paths(out_dir, nid)["act"])


def reduce_night(task) -> Dict[str, object]:
    nid, subj, night, out_dir, raw_root, manifest, want_motion, keep_raw = task
    t0 = time.time()
    p = _paths(out_dir, nid)
    raw = os.path.join(raw_root, nid)
    os.makedirs(raw, exist_ok=True)
    os.makedirs(os.path.dirname(p["act"]), exist_ok=True)
    rel = f"{subj}/{night}"
    try:
        meta: Dict[str, object]
        if hr_done(out_dir, nid):
            with open(p["meta"]) as fh:
                meta = json.load(fh)
        else:
            download_verified(f"{rel}/labels.mat", os.path.join(raw, "labels.mat"), manifest)
            download_verified(f"{rel}/hr.csv", os.path.join(raw, "hr.csv"), manifest)
            lab = load_labels(os.path.join(raw, "labels.mat"))
            rec0 = rec_start_unix(lab["recStart"])
            codes, src = choose_labels(lab)
            hr = load_hr(os.path.join(raw, "hr.csv"))
            n_exp = len(lab.get("expert_label") or [])
            n_dr = len(lab.get("dreem_label") or [])
            dist: Dict[str, int] = {}
            for c in codes:
                dist[str(c)] = dist.get(str(c), 0) + 1
            agree = None
            if n_exp and n_exp == n_dr:
                pairs = [(a, b) for a, b in zip(lab["expert_label"], lab["dreem_label"])
                         if a != 5 and b != 5]
                agree = round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None
            meta = dict(
                id=nid, subject=subj, night=night, label_source=src,
                recStart=lab["recStart"], recStart_tz=TZ_NAME, recStart_unix=rec0,
                n_epochs=len(codes), n_expert=n_exp, n_dreem=n_dr,
                expert_dreem_agreement=agree, stage_counts=dist,
                hr_n=len(hr),
                hr_first_rel_s=round(hr[0][0] - rec0, 2) if hr else None,
                hr_last_rel_s=round(hr[-1][0] - rec0, 2) if hr else None,
                hr_median_dt_s=(sorted(b[0] - a[0] for a, b in zip(hr, hr[1:]))[len(hr) // 2 - 1]
                                if len(hr) > 2 else None),
            )
            warn = []
            if hr and abs(hr[0][0] - rec0) > ALIGN_WARN_S:
                warn.append("hr_start_far_from_recStart")
            meta["warnings"] = warn
            _write_atomic(p["hr"], "".join(f"{t - rec0:.2f},{v:.1f}\n" for t, v in hr))
            _write_atomic(p["labels"], "".join(f"{k * EPOCH_S:.0f} {c}\n"
                                               for k, c in enumerate(codes)))
            _write_atomic(p["meta"], json.dumps(meta, indent=1))
        if want_motion and not motion_done(out_dir, nid):
            import numpy as np

            mpath = os.path.join(raw, "motion.csv")
            download_verified(f"{rel}/motion.csv", mpath, manifest)
            t, mag = load_motion(mpath)
            rec0 = float(meta["recStart_unix"])
            lines, fs = activity_lines(t - rec0, mag)
            meta["motion_n"] = int(len(t))
            meta["motion_fs_hz"] = round(fs, 3)
            meta["motion_first_rel_s"] = round(float(t[0] - rec0), 2) if len(t) else None
            meta["motion_last_rel_s"] = round(float(t[-1] - rec0), 2) if len(t) else None
            meta["motion_mag_median_g"] = round(float(np.median(mag)), 4) if len(mag) else None
            meta["activity_epochs"] = len(lines)
            if len(t) and abs(float(t[0]) - rec0) > ALIGN_WARN_S:
                meta.setdefault("warnings", []).append("motion_start_far_from_recStart")
            _write_atomic(p["meta"], json.dumps(meta, indent=1))
            _write_atomic(p["act"], "# epoch_start_s,pim,zcm,mad,std,pmax,n  "
                                    f"(BIDSleep Apple Watch wrist, g units, ~{fs:.0f} Hz)\n"
                          + "\n".join(lines) + ("\n" if lines else ""))
        if not keep_raw:
            shutil.rmtree(raw, ignore_errors=True)
        return dict(id=nid, ok=True, seconds=round(time.time() - t0, 1),
                    src=meta.get("label_source"), epochs=meta.get("n_epochs"),
                    act=meta.get("activity_epochs"), fs=meta.get("motion_fs_hz"))
    except Exception as exc:  # noqa: BLE001 -- keep the pool going; the rerun resumes
        return dict(id=nid, ok=False, error=repr(exc), seconds=round(time.time() - t0, 1))


# --------------------------------------------------------------------------- verify
def verify(out_dir: str) -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from sleepctl.ml.sleep_staging.dataset import (_parse_labels, _parse_pairs,
                                                   discover_subjects, parse_activity)
    ids = discover_subjects(out_dir)
    subs = sorted({subject_of(i) for i in ids})
    names = {0: "W", 1: "N1", 2: "N2", 3: "N3", 5: "REM", -1: "unk"}
    tot: Dict[str, int] = {}
    srcs: Dict[str, int] = {}
    n_act = 0
    warns = []
    for nid in ids:
        labels = _parse_labels(os.path.join(out_dir, f"{nid}_labeled_sleep.txt"))
        hr = _parse_pairs(os.path.join(out_dir, f"{nid}_heartrate.txt"))
        act = parse_activity(os.path.join(out_dir, "activity", f"{nid}_activity.txt"))
        n_act += 1 if act else 0
        try:
            with open(os.path.join(out_dir, f"{nid}_meta.json")) as fh:
                meta = json.load(fh)
        except OSError:
            meta = {}
        srcs[meta.get("label_source", "?")] = srcs.get(meta.get("label_source", "?"), 0) + 1
        if meta.get("warnings"):
            warns.append((nid, meta["warnings"]))
        dist: Dict[str, int] = {}
        for _t, c in labels:
            dist[names.get(c, str(c))] = dist.get(names.get(c, str(c)), 0) + 1
            tot[names.get(c, str(c))] = tot.get(names.get(c, str(c)), 0) + 1
        print(f"  {nid}: {len(labels)} epochs, {len(hr)} HR, {len(act)} act "
              f"[{meta.get('label_source', '?')}, fs={meta.get('motion_fs_hz')}] {dist}")
    print(f"{len(ids)} nights from {len(subs)} subjects in {out_dir}; "
          f"{n_act} with activity; label sources {srcs}")
    scored = sum(v for k, v in tot.items() if k != "unk")
    print("stage totals: " + ", ".join(f"{k}={v} ({100.0 * v / max(1, scored):.1f}%)"
                                       for k, v in sorted(tot.items())))
    for nid, w in warns:
        print(f"  WARNING {nid}: {w}")
    return 0


# --------------------------------------------------------------------------- main
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT, help="reduced output folder (keep OUT of the repo)")
    ap.add_argument("--raw", default=DEFAULT_RAW, help="transient raw download folder")
    ap.add_argument("--limit", type=int, default=None, help="only the first N nights")
    ap.add_argument("--nights", nargs="*", default=None, help="night IDs, e.g. Bidslab00_n1")
    ap.add_argument("--subjects", nargs="*", default=None, help="subject IDs, e.g. Bidslab00")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip nights whose requested outputs already exist (resume)")
    ap.add_argument("--no-motion", action="store_true",
                    help="only hr.csv + labels.mat (fast; enough for the HR-only models)")
    ap.add_argument("--jobs", type=int, default=8, help="nights downloaded concurrently")
    ap.add_argument("--keep-raw", action="store_true", help="do not delete raw files")
    ap.add_argument("--max-raw-gb", type=float, default=7.0,
                    help="warn when the raw download folder grows past this")
    ap.add_argument("--verify", action="store_true", help="only summarise the reduced folder")
    ap.add_argument("--download-zip", metavar="PATH", default=None,
                    help="fetch the whole-project ZIP (6.35 GB) to PATH with parallel ranges")
    ap.add_argument("--zip-connections", type=int, default=16)
    ap.add_argument("--from-zip", metavar="PATH", default=None,
                    help="reduce nights from the project ZIP instead of per-file downloads")
    args = ap.parse_args(argv)
    if args.verify:
        return verify(args.out)
    if args.download_zip:
        download_zip(args.download_zip, args.zip_connections)
        if not args.from_zip:
            return 0

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.raw, exist_ok=True)
    manifest = fetch_manifest(args.raw)
    nights = nights_from_manifest(manifest)
    if args.subjects:
        want = set(args.subjects)
        nights = [n for n in nights if n[1] in want]
    if args.nights:
        want = set(args.nights)
        nights = [n for n in nights if n[0] in want]
    if args.limit:
        nights = nights[: args.limit]
    want_motion = not args.no_motion
    if args.skip_existing:
        nights = [n for n in nights if not (hr_done(args.out, n[0])
                                            and (not want_motion or motion_done(args.out, n[0])))]
    _log(f"{len(nights)} night(s) to reduce -> {args.out} "
         f"({'HR + labels only' if args.no_motion else 'HR + labels + motion'}, jobs={args.jobs})")
    if not nights:
        return 0
    tasks = [(nid, subj, night, args.out, args.raw, manifest, want_motion, args.keep_raw)
             for nid, subj, night in nights]
    ok = fail = 0
    t0 = time.time()
    with multiprocessing.Pool(max(1, args.jobs)) as pool:
        worker = reduce_night
        if args.from_zip:
            tasks = [(args.from_zip,) + t for t in tasks]
            worker = reduce_night_from_zip
        for i, res in enumerate(pool.imap_unordered(worker, tasks), 1):
            if res["ok"]:
                ok += 1
                _log(f"[{i}/{len(tasks)}] {res['id']}: {res['epochs']} epochs "
                     f"[{res['src']}] act={res.get('act')} fs={res.get('fs')} ({res['seconds']}s)")
            else:
                fail += 1
                _log(f"[{i}/{len(tasks)}] {res['id']}: FAILED {res['error']}")
            raw_gb = _du_gb(args.raw)
            if raw_gb > args.max_raw_gb:
                _log(f"  raw folder at {raw_gb:.1f} GB (> {args.max_raw_gb}); workers keep "
                     "going but check disk")
    _log(f"done: {ok} reduced, {fail} failed in {time.time() - t0:.0f}s "
         f"(rerun with --skip-existing to retry failures)")
    return 0 if fail == 0 else 2


def _du_gb(path: str) -> float:
    tot = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot / 1e9


if __name__ == "__main__":
    sys.exit(main())
