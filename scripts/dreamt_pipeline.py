#!/usr/bin/env python3
"""DREAMT on the box, hands-off: check access, stream, reduce, train, install if better.

Run by the watchdog once a day until it succeeds (see ``Ensure-DreamtModel`` in
scripts/windows-watchdog.ps1). Every step is resumable and every outcome is written to a small
status file the health snapshot publishes -- counts and scores only, never data.

Rules this follows (PhysioNet credentialed data under the Restricted Health Data Use Agreement):

  * Authentication comes from the user's existing ``.netrc`` ("machine physionet.org"). This
    script never reads the password itself, never prints, logs or copies it, and never prompts
    for one: requests picks the entry up through the NETRC variable. No .netrc entry, or a 403
    from the file server, is reported as a status and the run stops.
  * It never signs an agreement or requests access. It only uses access already granted.
  * The data never enters the repository. Raw and reduced files live in a work folder OUTSIDE
    the repo (D:\\sleepctl-cache\\dreamt by default when D: exists), and only the trained weight
    files are installed -- into ``.run/staging_weights``, which is local and git-ignored.
  * Disk is scarce on this box, so each participant's 64 Hz CSV (~150 MB) is streamed to disk,
    checked against SHA256SUMS.txt, reduced, and deleted before the next one: peak use is one
    raw file plus the small reduced outputs. A DREAMT ZIP already in Downloads is read in place
    instead of downloading anything.

Installed weights are only used if they beat the bundled model (see ``_better_than_bundled``).

Usage (normally launched by the watchdog):
    python scripts/dreamt_pipeline.py [--work D:\\sleepctl-cache\\dreamt] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

BASE_URL = "https://physionet.org/files/dreamt/2.2.0/"
RUN_DIR = os.path.join(ROOT, ".run")
STATUS_PATH = os.path.join(RUN_DIR, "dreamt.status.json")
INSTALL_DIR = os.path.join(RUN_DIR, "staging_weights")
#: The DREAMT variants the trainer writes; the only files ever installed.
WEIGHT_FILES = ("wake_hrv.json", "stage4_hrv.json", "wake_hrvonly.json", "stage4_hrvonly.json")
#: A model below this held-out 4-class kappa is not installed whatever the bundled one scores.
MIN_KAPPA4 = 0.45


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(**fields) -> Dict[str, object]:
    """Merge ``fields`` into the status file. Counts, stages and scores only -- never data."""
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        with open(STATUS_PATH) as fh:
            status = json.load(fh)
    except Exception:
        status = {}
    status.update(fields)
    status["updated"] = _now()
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(status, fh, indent=1, default=str)
    os.replace(tmp, STATUS_PATH)
    return status


def netrc_path() -> Optional[str]:
    """The user's .netrc (or Windows _netrc) holding a physionet.org entry -- found, not read
    beyond checking the machine name is present."""
    home = os.path.expanduser("~")
    cands = [os.environ.get("NETRC"), os.path.join(home, ".netrc"), os.path.join(home, "_netrc")]
    # The watchdog's scheduled task may run under a different profile than the user's; the
    # user's own home can be named explicitly. Other accounts' files are never looked at.
    prof = os.environ.get("SLEEPCTL_NETRC_HOME") or os.environ.get("USERPROFILE")
    if prof:
        cands += [os.path.join(prof, ".netrc"), os.path.join(prof, "_netrc")]
    for p in cands:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                if re.search(r"machine\s+physionet\.org\b", fh.read()):
                    return p
        except Exception:
            continue
    return None


def default_work_dir() -> str:
    if os.name == "nt" and os.path.isdir("D:\\"):
        return "D:\\sleepctl-cache\\dreamt"
    return os.path.join(ROOT, "cache", "dreamt")      # cache/ is git-ignored


class Fetcher:
    """requests with .netrc auth (never handled here), backoff on 429/5xx, resumable streams."""

    def __init__(self, netrc: str) -> None:
        import requests
        os.environ["NETRC"] = netrc        # requests reads the entry; we never do
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "sleepctl-dreamt/1.0"

    def get(self, url: str, stream: bool = False, headers=None, tries: int = 6):
        delay = 2.0
        last = None
        for _ in range(tries):
            try:
                r = self.s.get(url, stream=stream, headers=headers or {}, timeout=60)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = r.status_code
                    r.close()
                    time.sleep(delay)
                    delay = min(delay * 2, 120)
                    continue
                return r
            except Exception as exc:          # network drop: wait and retry
                last = type(exc).__name__
                time.sleep(delay)
                delay = min(delay * 2, 120)
        raise RuntimeError(f"giving up on {url.rsplit('/', 1)[-1]} after {tries} tries ({last})")

    def download(self, url: str, dest: str) -> None:
        """Stream to ``dest`` with resume (HTTP Range) so a dropped connection continues."""
        part = dest + ".part"
        for _ in range(8):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            hdr = {"Range": f"bytes={have}-"} if have else {}
            r = self.get(url, stream=True, headers=hdr)
            if r.status_code == 416:        # already complete
                r.close()
                break
            if r.status_code not in (200, 206):
                code = r.status_code
                r.close()
                raise RuntimeError(f"HTTP {code} for {url.rsplit('/', 1)[-1]}")
            mode = "ab" if (have and r.status_code == 206) else "wb"
            try:
                with open(part, mode) as fh:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            fh.write(chunk)
                break
            except Exception:
                time.sleep(5)
                continue
            finally:
                r.close()
        os.replace(part, dest)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_64hz(f: Fetcher) -> List[str]:
    r = f.get(BASE_URL + "data_64Hz/")
    r.raise_for_status()
    names = sorted(set(re.findall(r'href="([^"/?]+\.csv)"', r.text)))
    return names


def sha_table(f: Fetcher) -> Dict[str, str]:
    try:
        r = f.get(BASE_URL + "SHA256SUMS.txt")
        if r.status_code != 200:
            return {}
        out = {}
        for line in r.text.splitlines():
            parts = line.strip().split()
            if len(parts) == 2:
                out[parts[1].lstrip("*").lstrip("./")] = parts[0].lower()
        return out
    except Exception:
        return {}


def find_local_zip() -> Optional[str]:
    """A DREAMT ZIP the user already downloaded (checked before downloading anything)."""
    homes = {os.path.expanduser("~"), os.environ.get("SLEEPCTL_NETRC_HOME") or "",
             os.environ.get("USERPROFILE") or ""}
    dirs = [os.path.join(h, "Downloads") for h in homes if h]
    if os.name == "nt" and os.path.isdir("D:\\"):
        dirs += ["D:\\", "D:\\Downloads", "D:\\sleepctl-cache"]
    for d in dirs:
        for z in glob.glob(os.path.join(d, "*.zip")):
            if "dreamt" in os.path.basename(z).lower() and os.path.getsize(z) > 1 << 30:
                return z
    return None


def _better_than_bundled(report: dict) -> Dict[str, object]:
    """Install only if the HRV model's held-out, HMM-smoothed 4-class kappa clears
    MIN_KAPPA4 and beats the bundled HR / HR+motion models' own CV kappa. (Different corpora,
    so this is a floor, not a head-to-head; the staging plausibility audit in the night export
    and the morning reviews judge it on this user.)"""
    try:
        cand = float(report["hrv"]["sm"]["kappa4"])
    except Exception:
        return {"install": False, "why": "no HRV kappa in the training report"}
    base = 0.0
    try:
        from sleepctl.ml.sleep_staging.infer import WEIGHTS_DIR
        with open(os.path.join(os.path.dirname(WEIGHTS_DIR), "cv_report.json")) as fh:
            bundled = json.load(fh)
        for k in ("hr", "hrmotion", "hrmotion_scalefree"):
            try:
                base = max(base, float(bundled[k]["sm"]["kappa4"]))
            except Exception:
                continue
    except Exception:
        pass
    ok = cand >= MIN_KAPPA4 and cand > base
    return {"install": ok, "candidate_kappa4": round(cand, 3), "bundled_kappa4": round(base, 3),
            "why": ("beats the bundled model" if ok else
                    f"kappa {cand:.3f} does not clear max({MIN_KAPPA4}, bundled {base:.3f})")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=None, help="work folder OUTSIDE the repo (default: D: cache)")
    ap.add_argument("--limit", type=int, default=None, help="participants (smoke test)")
    ap.add_argument("--dry-run", action="store_true", help="check access and plan only")
    ap.add_argument("--quick", action="store_true", help="tiny model grid (smoke test)")
    args = ap.parse_args(argv)

    import dreamt_reduce as R

    work = args.work or default_work_dir()
    reduced = os.path.join(work, "reduced")
    raw = os.path.join(work, "raw")
    os.makedirs(reduced, exist_ok=True)
    write_status(stage="starting", started=_now(), work_dir_on=work[:3], error=None)

    local_zip = find_local_zip()
    files: List[str] = []
    fetch = None
    if local_zip:
        files = R.discover(local_zip)
        write_status(stage="reducing", source="local zip", n_participants=len(files))
    else:
        netrc = netrc_path()
        if not netrc:
            write_status(stage="blocked", error="no physionet.org entry in .netrc on this box")
            return 2
        fetch = Fetcher(netrc)
        probe = fetch.get(BASE_URL + "participant_info.csv")
        if probe.status_code in (401, 403):
            write_status(stage="blocked", error=f"PhysioNet answered {probe.status_code}: "
                         "access to DREAMT is not live for this account yet (credentialing "
                         "and the project's data use agreement must both be approved)")
            return 3
        probe.raise_for_status()
        with open(os.path.join(work, "participant_info.csv"), "wb") as fh:
            fh.write(probe.content)
        files = list_64hz(fetch)
        write_status(stage="reducing", source="physionet.org", n_participants=len(files))
    if args.limit:
        files = files[: args.limit]
    if args.dry_run:
        write_status(stage="dry-run", planned=len(files))
        return 0

    shas = sha_table(fetch) if fetch else {}
    done = failed = 0
    for i, name in enumerate(files):
        pid = R.participant_id(name)
        if os.path.exists(os.path.join(reduced, f"{pid}_ibi.txt")):
            done += 1
            continue
        try:
            if fetch:
                os.makedirs(raw, exist_ok=True)
                dest = os.path.join(raw, os.path.basename(name))
                fetch.download(BASE_URL + "data_64Hz/" + name, dest)
                want = shas.get(f"data_64Hz/{name}")
                if want and _sha256(dest) != want:
                    os.remove(dest)
                    raise RuntimeError("checksum mismatch")
                R.reduce_file(dest, reduced, verbose=False)
                os.remove(dest)                    # disk is scarce: one raw file at a time
            else:
                R.reduce_file(name, reduced, verbose=False)
            done += 1
        except Exception as exc:
            failed += 1
            write_status(last_error=f"{pid}: {exc}"[:200])
        write_status(stage="reducing", reduced=done, failed=failed, of=len(files))
    shutil.rmtree(raw, ignore_errors=True)
    if done < 10:
        write_status(stage="failed", error=f"only {done} participants reduced")
        return 4

    write_status(stage="training", reduced=done)
    import train_dreamt as TD
    out = os.path.join(work, "weights_candidate")
    report = TD.train(reduced, out, quick=args.quick, jobs=max(1, (os.cpu_count() or 2) - 1),
                      cache_dir=os.path.join(work, "cache"))
    verdict = _better_than_bundled(report or {})
    scores = {k: {"kappa4_smoothed": round(float(v["sm"]["kappa4"]), 3),
                  "wake_kappa_smoothed": round(float(v["sm"].get("wake_kappa", 0.0)), 3)}
              for k, v in (report or {}).items()
              if isinstance(v, dict) and isinstance(v.get("sm"), dict) and "kappa4" in v["sm"]}
    if verdict["install"]:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        for fn in WEIGHT_FILES:
            src = os.path.join(out, fn)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(INSTALL_DIR, fn))
    write_status(stage="installed" if verdict["install"] else "trained_not_installed",
                 finished=_now(), scores=scores, verdict=verdict,
                 participants=(report or {}).get("participants"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:              # the status file is the only report the user sees
        write_status(stage="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        raise
