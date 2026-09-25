#!/usr/bin/env python3
"""MESA on the box, hands-off: check NSRR access, stream, reduce, train, install if better.

Meant to be run by the watchdog once a day until it has trained (the ``Ensure-MesaModel``
function in docs/MESA_TRAINING.md). Every step is resumable and every outcome is written to
.run/mesa.status.json -- counts and scores only, never data, never the token.

Rules this follows (MESA is released by the NSRR under a data use agreement):

  * Authentication is the user's own NSRR token (https://sleepdata.org/token), read from
    SLEEPCTL_NSRR_TOKEN / NSRR_TOKEN or a token file (see ``token_candidates``). It is never
    printed, logged, written to the status or copied anywhere; every message that could carry
    it (an exception quoting a URL) is scrubbed first. No token, a token NSRR rejects, or a
    token whose MESA data request is not approved yet, is reported as a status and the run
    stops.
  * It never requests access or signs anything. It only uses access already granted.
  * The data never enters the repository. Raw and reduced files live in a work folder OUTSIDE
    the repo (D:\\sleepctl-cache\\mesa when D: exists), and only the trained weight files are
    installed -- into ``.run/staging_weights``, which is local and git-ignored.
  * Disk is scarce. Each record's EDF (~190 MB; 385 GB for all 2,056) is downloaded with
    resume, checked against the MD5 the NSRR file API publishes, reduced and DELETED before
    the worker takes the next one: peak raw use is one EDF per worker.
  * ``--limit`` (default 200, or SLEEPCTL_MESA_LIMIT) takes an evenly spaced subset of the
    records. The trainer keeps every epoch's features in memory (~16 KB an epoch, ~1,200
    epochs a night: ~4 GB for 200 nights), so the whole corpus does not fit a small box.

NSRR mechanics (https://github.com/nsrr/sleepdata.org/wiki/api-v1-datasets, and the nsrr gem):

    GET https://sleepdata.org/api/v1/account/profile.json?auth_token=T     -> {"authenticated": ..}
    GET https://sleepdata.org/api/v1/datasets/mesa/files.json?path=P&auth_token=T
        -> [{"file_name", "full_path", "is_file", "file_size", "file_checksum_md5", ..}]
           (immediate children of folder P only)
    GET https://sleepdata.org/datasets/mesa/files/a/T/m/<client>/<full_path>  -> the file

Installed weights are only used if they beat every shipped model (see ``verdict``).

Usage (normally launched by the watchdog):
    python scripts/mesa_pipeline.py [--work D:\\sleepctl-cache\\mesa] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

SITE = "https://sleepdata.org"
SLUG = "mesa"
API_FILES = f"{SITE}/api/v1/datasets/{SLUG}/files.json"
API_PROFILE = f"{SITE}/api/v1/account/profile.json"
EDF_DIR = "polysomnography/edfs"
XML_DIRS = (("polysomnography/annotations-events-nsrr", "-nsrr.xml"),
            ("polysomnography/annotations-events-profusion", "-profusion.xml"))
RPOINT_DIR = ("polysomnography/annotations-rpoints", "-rpoint.csv")
ACT_DIR = "actigraphy"
OVERLAP_DIR = "overlap"
#: Where the sleepdata.org intermediate certificate lives if the server does not send it
#: (seen 2026-09: the chain stops at the leaf). Only a fallback; the leaf's AIA is read first.
AIA_FALLBACK = "http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt"

DEFAULT_LIMIT = 200
DEFAULT_WORKERS = 3
#: Stop taking new records when the work drive has less than this free beyond the next EDF.
MIN_FREE_BYTES = 3 << 30
#: Train only once this many records reduced.
MIN_RECORDS = 30
RUN_DIR = os.path.join(ROOT, ".run")
STATUS_PATH = os.path.join(RUN_DIR, "mesa.status.json")
DONE_PATH = os.path.join(RUN_DIR, "mesa.done")
INSTALL_DIR = os.path.join(RUN_DIR, "staging_weights")
DREAMT_STATUS = os.path.join(RUN_DIR, "dreamt.status.json")
WEIGHT_FILES = ("wake_hrv.json", "stage4_hrv.json", "wake_hrvonly.json", "stage4_hrvonly.json")
TOKEN_ENV = ("SLEEPCTL_NSRR_TOKEN", "NSRR_TOKEN")
TOKEN_FILE_ENV = "SLEEPCTL_NSRR_TOKEN_FILE"
TOKEN_FILE_NAMES = (".nsrr_token", "nsrr_token.txt", ".nsrr-token")
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{8,256}")

#: Everything secret this process has seen; ``scrub`` removes each from any outgoing text.
_SECRETS: List[str] = []


class Blocked(Exception):
    """Access, TLS or layout problem the user has to fix; reported, never a crash."""

    def __init__(self, code: int, stage: str, msg: str) -> None:
        super().__init__(msg)
        self.code, self.stage = code, stage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def scrub(value):
    """Remove the token (and anything shaped like an NSRR auth segment) from ``value``."""
    if isinstance(value, str):
        for s in _SECRETS:
            if s:
                value = value.replace(s, "***")
        value = re.sub(r"/a/[^/\s'\"]+/", "/a/***/", value)
        return re.sub(r"(auth_token=)[^&\s'\"]+", r"\1***", value)
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def write_status(**fields) -> Dict[str, object]:
    """Merge ``fields`` into the status file. Counts, stages and scores only -- never data,
    never the token (everything is scrubbed on the way in)."""
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        with open(STATUS_PATH) as fh:
            status = json.load(fh)
    except Exception:
        status = {}
    status.update(scrub(fields))
    status["updated"] = _now()
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(status, fh, indent=1, default=str)
    os.replace(tmp, STATUS_PATH)
    return status


# ------------------------------------------------------------------------------- the token
def token_candidates(explicit: Optional[str] = None) -> List[str]:
    """Token files looked at, in order: --token-file, SLEEPCTL_NSRR_TOKEN_FILE, then
    ``.nsrr_token`` / ``nsrr_token.txt`` in the user's home (and in the profile the watchdog
    names with SLEEPCTL_NETRC_HOME, since its scheduled task may run as another account),
    then D:\\sleepctl-cache\\nsrr_token.txt."""
    out = [explicit, os.environ.get(TOKEN_FILE_ENV)]
    homes = [os.path.expanduser("~"), os.environ.get("SLEEPCTL_NETRC_HOME"),
             os.environ.get("USERPROFILE")]
    for h in homes:
        if h:
            out += [os.path.join(h, n) for n in TOKEN_FILE_NAMES]
    if os.name == "nt" and os.path.isdir("D:\\"):
        out.append("D:\\sleepctl-cache\\nsrr_token.txt")
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def load_token(explicit_file: Optional[str] = None) -> Tuple[Optional[str], str]:
    """(token or None, where it came from -- "env" / "file" / a reason). The token itself is
    registered with ``scrub`` before it is returned and never leaves this process otherwise."""
    for var in TOKEN_ENV:
        v = (os.environ.get(var) or "").strip()
        if v:
            _SECRETS.append(v)
            if not TOKEN_RE.fullmatch(v):
                return None, f"{var} is set but does not look like an NSRR token"
            return v, "env"
    for p in token_candidates(explicit_file):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8-sig", errors="ignore") as fh:
                text = fh.read(4096)
        except OSError:
            continue
        v = next((ln.strip() for ln in text.splitlines() if ln.strip()
                  and not ln.lstrip().startswith("#")), "")
        if v:
            _SECRETS.append(v)
        if not TOKEN_RE.fullmatch(v):
            return None, "the token file does not hold an NSRR token (one line, as shown at sleepdata.org/token)"
        return v, "file"
    return None, "no NSRR token on this box"


def download_url(token: str, full_path: str) -> str:
    return f"{SITE}/datasets/{SLUG}/files/a/{token}/m/sleepctl/{full_path.lstrip('/')}"


def default_work_dir() -> str:
    """Always OUTSIDE the repository (MESA may never enter it)."""
    if os.name == "nt" and os.path.isdir("D:\\"):
        return "D:\\sleepctl-cache\\mesa"
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "sleepctl-cache", "mesa")


def _inside_repo(path: str) -> bool:
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(ROOT))
    return a == b or a.startswith(b + os.sep)


# ------------------------------------------------------------------------------ transport
def _fetcher_base():
    import dreamt_pipeline as DP
    return DP.Fetcher


class NsrrFetcher(_fetcher_base()):
    """dreamt_pipeline.Fetcher (backoff on 429/5xx, resumable downloads) without .netrc: the
    NSRR token travels in the URL, so nothing here ever prints a URL."""

    def __init__(self, verify=True) -> None:     # noqa: D401  (no netrc on purpose)
        import requests
        _apply_tls(verify)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "sleepctl-mesa/1.0"
        if verify not in (True, "truststore"):
            self.s.verify = verify


def _apply_tls(mode) -> None:
    if mode == "truststore":
        import truststore
        truststore.inject_into_ssl()


def _aia_url(der: bytes) -> str:
    m = re.search(rb"http://[\x21-\x7e]+?\.(?:crt|cer|der)", der)
    return m.group(0).decode("ascii") if m else AIA_FALLBACK


def _ca_bundle_with_intermediate(work: str, http_get) -> Optional[str]:
    """A CA bundle = certifi's roots + the intermediate named by the server's own certificate
    (Authority Information Access). Verification stays fully on: the intermediate only helps
    if it chains to a root already trusted."""
    import ssl
    try:
        import certifi
        roots = open(certifi.where(), encoding="ascii").read()
    except Exception:
        return None
    try:
        leaf_pem = ssl.get_server_certificate(("sleepdata.org", 443), timeout=30)
        url = _aia_url(ssl.PEM_cert_to_DER_cert(leaf_pem))
    except Exception:
        url = AIA_FALLBACK
    try:
        r = http_get(url)
        if r.status_code != 200 or not r.content:
            return None
        der = r.content
        pem = der.decode("ascii") if der.startswith(b"-----BEGIN") else ssl.DER_cert_to_PEM_cert(der)
    except Exception:
        return None
    path = os.path.join(work, "sleepdata-ca.pem")
    with open(path, "w", encoding="ascii") as fh:
        fh.write(roots.rstrip() + "\n" + pem)
    return path


def tls_mode(work: str):
    """How to verify sleepdata.org: True (the default store), "truststore" (the OS store,
    which fetches a missing intermediate itself on Windows), or a CA-bundle path. Raises
    Blocked if none verifies -- TLS verification is never switched off."""
    import requests
    url = f"{SITE}/api/v1/datasets/{SLUG}.json"

    def ok(verify, strict: bool = False) -> bool:
        try:
            _apply_tls(verify)
            requests.get(url, timeout=30, verify=True if verify == "truststore" else verify)
            return True
        except requests.exceptions.SSLError:
            return False
        except requests.exceptions.RequestException as exc:
            if strict:
                raise Blocked(7, "blocked", f"sleepdata.org unreachable ({type(exc).__name__})") from None
            return False

    if ok(True, strict=True):
        return True
    try:
        import truststore  # noqa: F401
        if ok("truststore"):
            return "truststore"
    except ImportError:
        pass
    bundle = _ca_bundle_with_intermediate(work, lambda u: requests.get(u, timeout=30))
    if bundle and ok(bundle):
        return bundle
    raise Blocked(7, "blocked", "sleepdata.org's TLS certificate chain could not be verified "
                  "(pip install truststore lets Windows complete it)")


class Nsrr:
    """The three NSRR calls this needs. Every failure becomes a scrubbed ``Blocked``."""

    def __init__(self, token: str, fetch) -> None:
        self.token, self.f = token, fetch

    def authenticated(self) -> bool:
        r = self.f.get(API_PROFILE + "?auth_token=" + self.token)
        try:
            return r.status_code == 200 and bool(r.json().get("authenticated"))
        except Exception:
            return False

    def list(self, path: str) -> Optional[List[dict]]:
        """Immediate children of ``path``; None if the API refused or answered non-JSON."""
        from urllib.parse import quote
        r = self.f.get(f"{API_FILES}?path={quote(path)}&auth_token={self.token}")
        if r.status_code != 200:
            return None
        try:
            items = r.json()
        except Exception:
            return None
        return items if isinstance(items, list) else None

    def probe(self, full_path: str) -> bool:
        """True if a real file comes back (not a login page): proof the data request is live."""
        r = self.f.get(download_url(self.token, full_path), stream=True)
        try:
            if r.status_code != 200:
                return False
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = next(r.iter_content(512), b"") or b""
            return "html" not in ctype and not head.lstrip().lower().startswith((b"<!doctype", b"<html"))
        finally:
            r.close()


def _files(items: Optional[List[dict]], suffix: str) -> Dict[str, dict]:
    """{record id: item} for listing entries named <record><suffix>."""
    out = {}
    for it in items or []:
        name = str(it.get("file_name") or "")
        if it.get("is_file", True) and name.lower().endswith(suffix) and it.get("full_path"):
            out[name[: -len(suffix)]] = it
    return out


def evenly_spaced(ids: Sequence[str], limit: Optional[int]) -> List[str]:
    ids = sorted(ids)
    if not limit or limit >= len(ids):
        return ids
    step = len(ids) / float(limit)
    return [ids[int(i * step)] for i in range(limit)]


# ---- worker processes (top level so Windows' spawn start method can import them) ----------
_W: Dict[str, object] = {}


def _init_worker(token: str, tls, reduced: str, raw: str, overlap: Optional[str]) -> None:
    import mesa_reduce as M
    _W.clear()
    _SECRETS.append(token)
    _W.update(token=token, reduced=reduced, raw=raw, fetch=NsrrFetcher(tls), overlap={})
    if overlap and os.path.exists(overlap):
        try:
            _W["overlap"] = M.read_overlap(overlap)
        except Exception:
            _W["overlap"] = {}


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _get(item: dict, dest: str) -> None:
    _W["fetch"].download(download_url(_W["token"], item["full_path"]), dest)
    want = str(item.get("file_checksum_md5") or "").lower()
    if want and _md5(dest) != want:
        os.remove(dest)
        raise RuntimeError("checksum mismatch")


def _reduce_one(job: dict) -> tuple:
    """Download, reduce and delete one record. Returns (record, error or None, bytes, permanent)."""
    import mesa_reduce as M
    rid, raw, reduced = job["id"], _W["raw"], _W["reduced"]
    os.makedirs(raw, exist_ok=True)
    got: List[str] = []
    try:
        need = int((job.get("edf") or {}).get("file_size") or 0)
        if shutil.disk_usage(raw).free < need + MIN_FREE_BYTES:
            return rid, "low disk on the work drive", 0, False

        def fetch(key: str, name: str) -> Optional[str]:
            item = job.get(key)
            if not item:
                return None
            dest = os.path.join(raw, name)
            got.append(dest)
            got.append(dest + ".part")
            _get(item, dest)
            return dest

        xml = fetch("xml", f"{rid}.xml")
        edf = fetch("edf", f"{rid}.edf")
        rpt = fetch("rpoint", f"{rid}-rpoint.csv") if not edf else None
        act = fetch("act", f"{rid}-act.csv")
        M.reduce_record(rid, xml, reduced, edf_path=edf, rpoint_path=rpt, act_path=act,
                        overlap_row=_W["overlap"].get(M.mesa_id(rid) or -1))
        return rid, None, sum(int((job.get(k) or {}).get("file_size") or 0)
                              for k in ("xml", "edf", "rpoint", "act")), False
    except M.LayoutError as exc:
        return rid, scrub(f"{exc}")[:200], 0, True
    except Exception as exc:
        try:
            os.remove(os.path.join(reduced, f"{rid}_ibi.txt"))
        except OSError:
            pass
        return rid, scrub(f"{type(exc).__name__}: {exc}")[:200], 0, False
    finally:
        # disk is scarce: raw files never accumulate (a .part survives only a dropped
        # connection, so the next run resumes it; everything else goes now)
        for p in got:
            if not p.endswith(".part") and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _run_jobs(jobs: List[dict], workers: int, init: tuple, on_done) -> None:
    if workers <= 1 or len(jobs) <= 1:
        _init_worker(*init)
        for job in jobs:
            on_done(*_reduce_one(job))
        return
    from concurrent.futures import ProcessPoolExecutor, as_completed
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=init) as ex:
        futs = [ex.submit(_reduce_one, job) for job in jobs]
        for fut in as_completed(futs):
            on_done(*fut.result())


# ---------------------------------------------------------------------------- the verdict
def _installed_kappa() -> Tuple[Optional[float], str]:
    """Held-out 4-class kappa of the HRV model already in .run/staging_weights, from the
    status of whichever pipeline installed it. (None, why) when there is none or it is
    unknown."""
    if not os.path.exists(os.path.join(INSTALL_DIR, "stage4_hrv.json")):
        return None, "none installed"
    best = None
    for path in (DREAMT_STATUS, STATUS_PATH):
        try:
            with open(path) as fh:
                st = json.load(fh)
            if st.get("stage") == "installed":
                k = float(st["scores"]["hrv"]["kappa4_smoothed"])
                best = k if best is None else max(best, k)
        except Exception:
            continue
    return best, ("known" if best is not None else "unknown")


def verdict(report: dict) -> Dict[str, object]:
    """Install only if the candidate clears dreamt_pipeline's bar (MIN_KAPPA4 and the bundled
    HR / HR+motion models' held-out kappa, from cv_report.json and cv_report_bidsleep.json)
    AND beats a locally installed HRV model (e.g. DREAMT's). An installed model whose score is
    unknown is never overwritten."""
    import dreamt_pipeline as DP
    v = dict(DP._better_than_bundled(report))
    if not v.get("install"):
        return v
    cand = float(report["hrv"]["sm"]["kappa4"])
    inst, why = _installed_kappa()
    if why == "unknown":
        v.update(install=False, why="an HRV model of unknown score is already installed")
    elif inst is not None:
        v["installed_kappa4"] = round(inst, 3)
        if cand <= inst:
            v.update(install=False, why=f"kappa {cand:.3f} does not beat the installed {inst:.3f}")
        else:
            v["why"] = "beats the bundled and the installed models"
    return v


# ----------------------------------------------------------------------------------- main
def _plan(api: Nsrr, beats: str, actigraphy: bool) -> Tuple[List[dict], dict]:
    """Records with their files, from the NSRR listing. Raises Blocked on a layout surprise."""
    top = api.list("")
    if top is None:
        raise Blocked(3, "blocked", "the MESA file list is not available to this token "
                      "(the MESA data request may not be approved yet)")
    folders = sorted(str(i.get("file_name")) for i in top if not i.get("is_file", True))
    psg = api.list("polysomnography")
    if not psg:
        raise Blocked(6, "failed", f"layout differs: no polysomnography/ folder (top level: {folders[:12]})")
    xml: Dict[str, dict] = {}
    for d, suffix in XML_DIRS:
        xml = _files(api.list(d), suffix)
        if xml:
            break
    if not xml:
        subs = sorted(str(i.get("file_name")) for i in psg if not i.get("is_file", True))
        raise Blocked(6, "failed", f"layout differs: no staging XML under polysomnography/ ({subs})")
    if beats == "rpoints":
        src = _files(api.list(RPOINT_DIR[0]), RPOINT_DIR[1])
        key = "rpoint"
    else:
        src = _files(api.list(EDF_DIR), ".edf")
        key = "edf"
    if not src:
        raise Blocked(6, "failed", f"layout differs: no files under {EDF_DIR if key == 'edf' else RPOINT_DIR[0]}")
    act: Dict[str, dict] = {}
    overlap = None
    if actigraphy:
        act = _files(api.list(ACT_DIR), ".csv")
        ov = [i for i in (api.list(OVERLAP_DIR) or []) if "overlap" in str(i.get("file_name", "")).lower()]
        overlap = ov[0] if ov else None
    jobs = [{"id": r, "xml": xml[r], key: src[r], "act": act.get(r)} for r in sorted(set(xml) & set(src))]
    if not jobs:
        raise Blocked(6, "failed", "layout differs: no record has both a staging XML and a "
                      f"{'EDF' if key == 'edf' else 'R-point file'} with a matching name")
    return jobs, {"overlap": overlap, "n_actigraphy": len(act), "source": key}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=None, help="work folder OUTSIDE the repo (default: D: cache)")
    ap.add_argument("--limit", type=int,
                    default=int(os.environ.get("SLEEPCTL_MESA_LIMIT") or DEFAULT_LIMIT),
                    help=f"records, evenly spaced over the corpus (default {DEFAULT_LIMIT}, env "
                         "SLEEPCTL_MESA_LIMIT; 0 = all 2,056)")
    ap.add_argument("--token-file", default=None, help="file holding the NSRR token (one line)")
    ap.add_argument("--dry-run", action="store_true", help="check access and plan only")
    ap.add_argument("--quick", action="store_true", help="tiny model grid (smoke test)")
    ap.add_argument("--workers", type=int, default=None,
                    help=f"records at once (default {DEFAULT_WORKERS}, env SLEEPCTL_MESA_WORKERS)")
    ap.add_argument("--beats", choices=("pleth", "rpoints"), default="pleth",
                    help="pleth: finger PPG from each EDF (default, matches the live PPG band); "
                         "rpoints: NSRR's ECG R-point files, ~1%% of the download")
    ap.add_argument("--no-actigraphy", action="store_true", help="skip the Actiwatch counts")
    args = ap.parse_args(argv)

    work = os.path.abspath(args.work or default_work_dir())
    if _inside_repo(work):
        write_status(stage="failed", error="the work folder must be outside the repository")
        return 5
    reduced, raw = os.path.join(work, "reduced"), os.path.join(work, "raw")
    write_status(stage="starting", started=_now(), error=None, last_error=None,
                 work_dir_on=work[:3])
    token, where = load_token(args.token_file)
    if not token:
        write_status(stage="blocked", error=where + " (see docs/MESA_TRAINING.md)")
        return 2
    os.makedirs(reduced, exist_ok=True)
    workers = args.workers or int(os.environ.get("SLEEPCTL_MESA_WORKERS") or 0) or \
        min(DEFAULT_WORKERS, max(1, (os.cpu_count() or 2) - 1))
    try:
        tls = tls_mode(work)
        api = Nsrr(token, NsrrFetcher(tls))
        if not api.authenticated():
            raise Blocked(3, "blocked", "NSRR rejected the token (copy it again from sleepdata.org/token)")
        jobs, meta = _plan(api, args.beats, not args.no_actigraphy)
        if not api.probe(jobs[0]["xml"]["full_path"]):
            raise Blocked(3, "blocked", "the token is valid but MESA downloads are not authorised "
                          "yet (the MESA data request and its DUA must be approved)")
    except Blocked as exc:
        write_status(stage=exc.stage, error=scrub(str(exc)))
        return exc.code
    except Exception as exc:                  # NSRR down, a network drop that outlasted retries
        write_status(stage="failed", error=scrub(f"checking access: {type(exc).__name__}: {exc}")[:300])
        return 1
    n_all = len(jobs)
    keep = set(evenly_spaced([j["id"] for j in jobs], args.limit or None))
    jobs = [j for j in jobs if j["id"] in keep]
    key = meta["source"]
    transfer = sum(int((j.get(k) or {}).get("file_size") or 0) for j in jobs
                   for k in ("xml", key, "act"))
    write_status(stage="planned", source=f"sleepdata.org ({'pleth' if key == 'edf' else 'rpoints'})",
                 n_records=n_all, of=len(jobs), transfer_gb=round(transfer / 1e9, 1),
                 actigraphy=meta["n_actigraphy"] > 0, workers=workers)
    if args.dry_run:
        write_status(stage="dry-run")
        return 0

    overlap_path = None
    if meta["overlap"]:
        overlap_path = os.path.join(work, "overlap.csv")
        try:
            f = api.f
            f.download(download_url(token, meta["overlap"]["full_path"]), overlap_path)
        except Exception as exc:
            overlap_path = None
            write_status(last_error=scrub(f"overlap file: {type(exc).__name__}"))

    skipped_path = os.path.join(work, "skipped.json")
    try:
        with open(skipped_path) as fh:
            skipped: Dict[str, str] = json.load(fh)
    except Exception:
        skipped = {}
    todo = [j for j in jobs if j["id"] not in skipped
            and not os.path.exists(os.path.join(reduced, f"{j['id']}_ibi.txt"))]
    tally = {"done": len(jobs) - len(todo) - sum(1 for j in jobs if j["id"] in skipped),
             "failed": 0, "skipped": sum(1 for j in jobs if j["id"] in skipped), "gb": 0.0}
    retry: List[dict] = []
    by_id = {j["id"]: j for j in todo}
    t0 = time.time()

    def on_done(rid: str, err: Optional[str], nbytes: int, permanent: bool) -> None:
        if err and permanent:
            tally["skipped"] += 1
            skipped[rid] = err
            with open(skipped_path, "w") as fh:
                json.dump(skipped, fh)
        elif err:
            tally["failed"] += 1
            retry.append(by_id[rid])
        else:
            tally["done"] += 1
            tally["gb"] += nbytes / 1e9
        if err:
            write_status(last_error=scrub(err))
        write_status(stage="reducing", reduced=tally["done"], failed=tally["failed"],
                     skipped=tally["skipped"], of=len(jobs), gb_downloaded=round(tally["gb"], 2),
                     minutes=round((time.time() - t0) / 60, 1))

    init = (token, tls, reduced, raw, overlap_path)
    _run_jobs(todo, workers, init, on_done)
    if retry:                                   # one more pass for transient failures
        again = list(retry)
        retry.clear()
        tally["failed"] = 0
        _run_jobs(again, workers, init, on_done)
    shutil.rmtree(raw, ignore_errors=True)
    done = tally["done"]
    if done < MIN_RECORDS:
        write_status(stage="failed", error=f"only {done} records reduced (need {MIN_RECORDS})")
        return 4

    write_status(stage="training", reduced=done)
    import train_dreamt as TD
    out = os.path.join(work, "weights_candidate")
    report = TD.train(reduced, out, quick=args.quick, jobs=min(2, max(1, (os.cpu_count() or 2) - 1)),
                      cache_dir=os.path.join(work, "cache"), exportable_only=True)
    dreamt_named = os.path.join(work, "cv_report_dreamt.json")    # the trainer's file name
    if os.path.exists(dreamt_named):
        os.replace(dreamt_named, os.path.join(work, "cv_report_mesa.json"))
    v = verdict(report or {})
    scores = {k: {"kappa4_smoothed": round(float(r["sm"]["kappa4"]), 3),
                  "wake_kappa_smoothed": round(float(r["sm"].get("wake_kappa", 0.0)), 3)}
              for k, r in (report or {}).items()
              if isinstance(r, dict) and isinstance(r.get("sm"), dict) and "kappa4" in r["sm"]}
    if v["install"]:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        for fn in WEIGHT_FILES:
            src = os.path.join(out, fn)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(INSTALL_DIR, fn))
    stage = "installed" if v["install"] else "trained_not_installed"
    write_status(stage=stage, finished=_now(), scores=scores, verdict=v,
                 participants=(report or {}).get("participants"))
    with open(DONE_PATH, "w") as fh:            # tells the watchdog not to start it again
        fh.write(stage + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:          # the status file is the only report the user sees
        write_status(stage="failed", error=scrub(f"{type(exc).__name__}: {exc}")[:300])
        # the traceback goes to the watchdog's err log: scrubbed, since a URL may be in it
        sys.stderr.write(scrub(traceback.format_exc()))
        sys.exit(1)
