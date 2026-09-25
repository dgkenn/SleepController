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
  * Disk is scarce on this box. Preferred source: the project ZIP read in place over HTTP
    Range requests -- only the data_64Hz members' compressed bytes are transferred and nothing
    raw touches the disk (each member's CRC-32 is checked as it streams). If the server will
    not do range reads, each participant's 64 Hz CSV is downloaded, checked against
    SHA256SUMS.txt, reduced and deleted: peak use is one raw file per worker. A DREAMT ZIP
    already in Downloads is read in place instead of downloading anything.
  * A few participants are reduced at once (``--workers``, default 3): each worker streams one
    night and holds ~50 MB, so network waits and parsing overlap without a real RAM cost.

Installed weights are only used if they beat the bundled model (see ``_better_than_bundled``).

Usage (normally launched by the watchdog):
    python scripts/dreamt_pipeline.py [--work D:\\sleepctl-cache\\dreamt] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import io
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
#: The whole project as one ZIP. Read over HTTP Range requests, member by member, it is the
#: cheapest source: only the data_64Hz members' COMPRESSED bytes cross the network (CSV deflates
#: roughly 3-5x) and nothing raw is written to disk at all.
ZIP_URL = "https://physionet.org/content/dreamt/get-zip/2.2.0/"
#: Participants reduced at once. Each worker streams one night and holds ~50 MB (one epoch of
#: samples plus the 1 Hz series), so 3 overlap network waits and parsing without real RAM cost.
DEFAULT_WORKERS = 3
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


class HttpRangeFile(io.RawIOBase):
    """A seekable, read-only view of a remote file over HTTP Range requests.

    Sequential reads share one open response; a seek elsewhere opens a new one at that offset.
    A dropped connection reopens at the current position, so a long member read survives it.
    ``resolve`` re-resolves the URL if a signed/redirected link expires mid-run."""

    def __init__(self, fetch: "Fetcher", url: str, size: int, resolve=None) -> None:
        super().__init__()
        self.f, self.url, self.size, self.resolve = fetch, url, int(size), resolve
        self.pos = 0
        self._r = None
        self._rpos: Optional[int] = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = max(0, base + int(off))
        return self.pos

    def _drop(self) -> None:
        if self._r is not None:
            try:
                self._r.close()
            except Exception:
                pass
        self._r, self._rpos = None, None

    def _open_at(self, pos: int) -> None:
        self._drop()
        r = self.f.get(self.url, stream=True, headers={"Range": f"bytes={pos}-"})
        if r.status_code in (401, 403, 404, 410) and self.resolve is not None:
            r.close()
            self.url = self.resolve()[0]
            r = self.f.get(self.url, stream=True, headers={"Range": f"bytes={pos}-"})
        if r.status_code != 206:
            code = r.status_code
            r.close()
            raise PermissionError(f"HTTP {code} to a range request")
        self._r, self._rpos = r, pos

    def readinto(self, b) -> int:
        if self.pos >= self.size or len(b) == 0:
            return 0
        delay = 2.0
        for _ in range(8):
            try:
                if self._r is None or self._rpos != self.pos:
                    self._open_at(self.pos)
                data = self._r.raw.read(min(len(b), self.size - self.pos), decode_content=True)
                if not data:
                    raise IOError("connection ended early")
                n = len(data)
                b[:n] = data
                self.pos += n
                self._rpos = self.pos
                return n
            except PermissionError:
                raise
            except Exception:
                self._drop()
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise IOError(f"range read at byte {self.pos} kept failing")

    def close(self) -> None:
        self._drop()
        super().close()


def resolve_zip(fetch: "Fetcher") -> Optional[tuple]:
    """(final_url, size) of the project ZIP if its server honours Range requests, else None."""
    try:
        r = fetch.get(ZIP_URL, stream=True, headers={"Range": "bytes=0-0"})
    except Exception:
        return None
    try:
        m = re.match(r"bytes\s+0-0/(\d+)", r.headers.get("Content-Range", ""))
        if r.status_code != 206 or not m:
            return None
        return r.url, int(m.group(1))
    finally:
        r.close()


def open_remote_zip(fetch: "Fetcher", url: str, size: int):
    import zipfile
    raw = HttpRangeFile(fetch, url, size, resolve=lambda: resolve_zip(fetch) or (url, size))
    return zipfile.ZipFile(io.BufferedReader(raw, buffer_size=1 << 20))


def zip_members(zf) -> List[str]:
    """The data_64Hz participant CSVs in a DREAMT ZIP (same rule as dreamt_reduce.discover)."""
    return sorted(n for n in zf.namelist()
                  if n.lower().endswith(".csv") and "/data_64hz/" in ("/" + n.lower())
                  and not os.path.basename(n).lower().startswith("participant_info"))


# ---- worker processes (top level so Windows' spawn start method can import them) ----------
_W: Dict[str, object] = {}


def _init_worker(mode: str, arg: object, reduced: str, raw: str) -> None:
    """One per process: the remote ZIP's central directory is read once, not once per night."""
    _W.clear()
    _W.update(mode=mode, reduced=reduced, raw=raw)
    if mode in ("remote_zip", "files"):
        netrc = arg[0] if mode == "remote_zip" else arg
        _W["fetch"] = Fetcher(netrc)
    if mode == "remote_zip":
        _W["zf"] = open_remote_zip(_W["fetch"], arg[1], arg[2])


def _reduce_one(name: str, sha: Optional[str] = None) -> tuple:
    """Reduce one participant. Returns (participant, error or None, compressed bytes read)."""
    import dreamt_reduce as R
    mode, reduced = _W["mode"], _W["reduced"]
    pid = R.participant_id(name)
    try:
        if mode == "remote_zip":
            zf = _W["zf"]
            info = zf.getinfo(name)
            with zf.open(info) as member:     # CRC-32 is checked at the end of the member
                text = io.TextIOWrapper(member, encoding="utf-8", newline="")
                R.reduce_file(name, reduced, verbose=False, stream=text)
            return pid, None, info.compress_size
        if mode == "files":
            os.makedirs(_W["raw"], exist_ok=True)
            dest = os.path.join(_W["raw"], os.path.basename(name))
            _W["fetch"].download(BASE_URL + "data_64Hz/" + name, dest)
            try:
                if sha and _sha256(dest) != sha:
                    raise RuntimeError("checksum mismatch")
                R.reduce_file(dest, reduced, verbose=False)
                return pid, None, 0
            finally:
                os.remove(dest)               # disk is scarce: raw files never accumulate
        R.reduce_file(name, reduced, verbose=False)
        return pid, None, 0
    except Exception as exc:
        # a half-written participant must not look done on the next run
        for fn in (f"{pid}_ibi.txt",):
            try:
                os.remove(os.path.join(reduced, fn))
            except OSError:
                pass
        return pid, f"{type(exc).__name__}: {exc}"[:200], 0


def _run_jobs(jobs: List[tuple], workers: int, init: tuple, on_done) -> None:
    """Reduce ``jobs`` [(name, sha)] with ``workers`` processes (inline when 1)."""
    if workers <= 1 or len(jobs) <= 1:
        _init_worker(*init)
        for name, sha in jobs:
            on_done(*_reduce_one(name, sha))
        return
    from concurrent.futures import ProcessPoolExecutor, as_completed
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=init) as ex:
        futs = [ex.submit(_reduce_one, name, sha) for name, sha in jobs]
        for fut in as_completed(futs):
            on_done(*fut.result())


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
    # The shipped HR / HR+motion weights are now the BIDSleep + sleep-accel retrain: its held-out
    # kappas (HR 0.454, HR+motion 0.471) are the bar too, not just the older cv_report's.
    try:
        from sleepctl.ml.sleep_staging.infer import WEIGHTS_DIR
        with open(os.path.join(os.path.dirname(WEIGHTS_DIR), "cv_report_bidsleep.json")) as fh:
            bid = json.load(fh)
        picked = bid["gate"]["picked"]
        for k in (f"{picked}/hr_dense", str(bid["gate"].get("motion_variant", ""))):
            try:
                base = max(base, float(bid["new_cv_on_bidsleep"][k]["sm"]["kappa4"]))
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
    ap.add_argument("--workers", type=int, default=None,
                    help=f"participants reduced at once (default {DEFAULT_WORKERS}, env "
                         "SLEEPCTL_DREAMT_WORKERS)")
    ap.add_argument("--source", choices=("auto", "zip", "files"), default="auto",
                    help="auto: the remote ZIP over Range requests, else per-file downloads")
    args = ap.parse_args(argv)

    import dreamt_reduce as R

    work = args.work or default_work_dir()
    reduced = os.path.join(work, "reduced")
    raw = os.path.join(work, "raw")
    os.makedirs(reduced, exist_ok=True)
    write_status(stage="starting", started=_now(), work_dir_on=work[:3], error=None)

    workers = args.workers or int(os.environ.get("SLEEPCTL_DREAMT_WORKERS") or 0) or \
        min(DEFAULT_WORKERS, max(1, (os.cpu_count() or 2) - 1))
    local_zip = find_local_zip()
    files: List[str] = []
    fetch = None
    init: tuple = ("local", None, reduced, raw)
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
        remote = resolve_zip(fetch) if args.source in ("auto", "zip") else None
        if remote:
            with open_remote_zip(fetch, *remote) as zf:
                files = zip_members(zf)
                need = sum(zf.getinfo(n).compress_size for n in files)
            init = ("remote_zip", (netrc, remote[0], remote[1]), reduced, raw)
            write_status(stage="reducing", source="physionet.org zip (range reads)",
                         n_participants=len(files), zip_gb=round(remote[1] / 1e9, 1),
                         transfer_gb=round(need / 1e9, 1))
        elif args.source == "zip":
            write_status(stage="failed", error="the ZIP server does not honour range requests")
            return 5
        else:
            files = list_64hz(fetch)
            init = ("files", netrc, reduced, raw)
            write_status(stage="reducing", source="physionet.org files",
                         n_participants=len(files))
    if args.limit:
        files = files[: args.limit]
    if args.dry_run:
        write_status(stage="dry-run", planned=len(files), workers=workers)
        return 0

    shas = sha_table(fetch) if init[0] == "files" else {}
    todo = [(n, shas.get(f"data_64Hz/{n}")) for n in files
            if not os.path.exists(os.path.join(reduced, f"{R.participant_id(n)}_ibi.txt"))]
    tally = {"done": len(files) - len(todo), "failed": 0, "gb": 0.0}
    t0 = time.time()
    retry: List[tuple] = []
    by_pid = {R.participant_id(n): (n, sha) for n, sha in todo}

    def on_done(pid: str, err: Optional[str], nbytes: int) -> None:
        if err:
            tally["failed"] += 1
            retry.append(by_pid[pid])
            write_status(last_error=f"{pid}: {err}")
        else:
            tally["done"] += 1
            tally["gb"] += nbytes / 1e9
        write_status(stage="reducing", reduced=tally["done"], failed=tally["failed"],
                     of=len(files), workers=workers, gb_read=round(tally["gb"], 2),
                     minutes=round((time.time() - t0) / 60, 1))

    _run_jobs(todo, workers, init, on_done)
    if retry:                                  # one more pass for transient failures
        again, retry = list(retry), []
        tally["failed"] = 0
        _run_jobs(again, workers, init, on_done)
    done, failed = tally["done"], tally["failed"]
    shutil.rmtree(raw, ignore_errors=True)
    if done < 10:
        write_status(stage="failed", error=f"only {done} participants reduced")
        return 4

    write_status(stage="training", reduced=done)
    import train_dreamt as TD
    out = os.path.join(work, "weights_candidate")
    # featurising runs one process per subject; capped so a many-core box doesn't multiply RAM
    report = TD.train(reduced, out, quick=args.quick, jobs=min(4, max(1, (os.cpu_count() or 2) - 1)),
                      cache_dir=os.path.join(work, "cache"), exportable_only=True)
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
