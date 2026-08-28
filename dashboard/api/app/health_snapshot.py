"""Health-snapshot publisher for the always-on Windows control machine.

The machine can PUSH to GitHub but the operator (off-site) can't reach the machine's
Tailscale funnel. So this module turns the operational self-diagnosis battery
(``app.diagnostics.run_diagnostics``) into a small, SCRUBBED JSON snapshot that
``scripts/publish-health.ps1`` commits to a public ``health`` branch of the same repo.
An off-box Claude then reads the machine's operational health straight from GitHub.

What's published is OPERATIONAL ONLY -- component up/down, heartbeat/tick ages, water
loop, thermal response, cloud errors, log sizes, credential PRESENCE (never values). No
passwords/tokens/emails, no HR/HRV/biometrics. ``run_diagnostics`` already avoids those,
but this module adds a belt-and-suspenders ``scrub`` pass so nothing secret-shaped can ever
leak into the public branch even if a future check starts echoing a value it shouldn't.

Everything here is defensive in the same spirit as ``diagnostics.py``: a diag hiccup, an
import failure, a bad db path -- none of it should stop a snapshot from being written. On a
hard failure ``write_snapshot`` still writes a minimal error snapshot to ``out_path`` before
signalling failure via ``SystemExit(1)`` (the PS layer decides success by exit code).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

# diagnostics.py is itself defensive (never raises out of run_diagnostics), so importing it at
# module top is safe -- but every CALL is still guarded so a broken import at runtime can't stop
# a snapshot from being written.
try:
    from app.diagnostics import run_diagnostics as _run_diagnostics
except Exception:  # pragma: no cover - import-time defensiveness
    _run_diagnostics = None


SCHEMA = "sleepctl.health/v1"

# ------------------------------------------------------------------ scrub (belt-and-suspenders)
# Dict keys whose VALUE must always be redacted regardless of shape (case-insensitive substring).
_SECRET_KEY_SUBSTRINGS = (
    "password", "secret", "token", "email", "recipient", "authorization", "cookie", "bearer",
)

# String VALUES that LOOK secret-shaped get replaced wholesale with "[redacted]". Kept
# conservative so ordinary words / short git shas / statuses ("ok") are never mangled.
_SECRET_VALUE_PATTERNS = (
    # email address
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # age1... public/secret key (bech32-ish; age keys are long)
    re.compile(r"age1[0-9a-z]{16,}", re.IGNORECASE),
    # bearer / authorization header value
    re.compile(r"\b(?:bearer|authorization)\b\s*[:=]?\s*\S+", re.IGNORECASE),
    # JWT: three dot-separated base64url segments starting with the classic eyJ header
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # any run of 32+ hex chars (api keys, full sha256/hmac, session ids, ...)
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # inline password=/pwd=/secret=/token= style secrets
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_\-]?key)\s*=\s*\S+"),
)

_REDACTED = "[redacted]"


def _key_is_secret(key) -> bool:
    if not isinstance(key, str):
        return False
    low = key.lower()
    return any(sub in low for sub in _SECRET_KEY_SUBSTRINGS)


def _scrub_string(value: str) -> str:
    for pat in _SECRET_VALUE_PATTERNS:
        if pat.search(value):
            return _REDACTED
    return value


def scrub(obj):
    """Recursively return a NEW structure with secret-shaped data redacted.

    - The value of any dict key whose name contains a secret substring
      (password/secret/token/email/recipient/authorization/cookie/bearer) becomes "[redacted]".
    - Any string value that matches a secret-shaped pattern (email, age1 key, bearer/auth
      header, JWT, 32+ hex run, inline password=) becomes "[redacted]".
    - Everything else (ordinary words, numbers, booleans, None, short hex like a 7-char git
      sha, statuses like "ok") is preserved unchanged.

    Never mutates ``obj`` in place -- builds and returns new containers.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _key_is_secret(k):
                out[k] = _REDACTED
            else:
                out[k] = scrub(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj)
    # int / float / bool / None / other scalars pass through untouched
    return obj


# ------------------------------------------------------------------ snapshot assembly
# Per-check whitelist: copy ONLY these five keys from each diagnostics check.
_CHECK_KEYS = ("id", "title", "status", "detail", "remedy")
# Per-playbook-match whitelist: copy only these scalar/string fields if present.
_PLAYBOOK_KEYS = ("id", "title", "summary", "severity", "remedy", "confidence")


def _iso(now: datetime | None) -> str:
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _copy_checks(checks) -> list:
    out = []
    if not isinstance(checks, list):
        return out
    for c in checks:
        if not isinstance(c, dict):
            continue
        out.append({k: c.get(k) for k in _CHECK_KEYS})
    return out


def _copy_playbook_matches(matches) -> list:
    out = []
    if not isinstance(matches, list):
        return out
    for m in matches:
        if not isinstance(m, dict):
            continue
        entry = {k: m[k] for k in _PLAYBOOK_KEYS if k in m}
        out.append(entry)
    return out


#: Operational log tails published alongside the checks. Checks say WHAT is wrong; these say WHY,
#: and without them a remote operator has to ask someone to read a file off the box -- which is
#: exactly the loop this whole GitHub relay exists to remove. 2026-08-28: the wearable had been
#: silent for 26 h and the only record of what the BLE bridge was actually seeing each scan
#: ("no Polar/HR sensor found" vs "connecting to AA:BB..." vs a PMD refusal) sat in .run\verity.log
#: where nothing could reach it.
#:
#: Operational only, and everything still goes through ``scrub`` afterwards like every other
#: field. These logs carry device addresses and connection state, never physiology -- the
#: forwarder POSTs samples onward rather than logging them, and it already redacts the ingest
#: token (see verity_forwarder._redact).
_LOG_TAILS = (
    ("verity", "verity.log", 40),
    ("verity_err", "verity.err", 10),
)


def _log_tails(run_dir: str | None) -> dict:
    """Last N lines of each operational log, best-effort. Never raises, never blocks publishing."""
    out: dict = {}
    if not run_dir:
        # publish-health.ps1 invokes this with (db_path, out_path) and no run_dir, so it arrives
        # None in production -- resolving the same default diagnostics uses is what makes this
        # work on the real box rather than only when a caller happens to pass one.
        try:
            from app.diagnostics import _default_run_dir
            run_dir = _default_run_dir()
        except Exception:
            db = os.environ.get("SLEEPCTL_DB", "")
            run_dir = os.path.join(os.path.dirname(db) if db else os.getcwd(), ".run")
    if not run_dir:
        return out
    for label, name, n in _LOG_TAILS:
        path = os.path.join(run_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-n:]
            text = [ln.rstrip("\n") for ln in lines if ln.strip()]
            if text:
                out[label] = text
        except FileNotFoundError:
            continue
        except Exception as exc:
            out[label] = [f"<unreadable: {exc!r}>"]
    return out



def _funnel_url(run_dir: str | None) -> str | None:
    """The public Tailscale Funnel URL, if the watchdog recorded one.

    Published deliberately and with the operator's explicit approval. It IS an attack surface --
    the health branch is public -- but it is the difference between polling a 3-minute snapshot
    and actually debugging: with it, an off-site operator can query ``/api/diag?token=...`` live
    and on demand instead of only seeing whatever was pre-published. The dashboard behind it
    requires auth, and /diag returns 404 without DIAG_TOKEN, so the URL alone grants nothing.

    The scrub pass deliberately does NOT redact this: it is a hostname, not a credential, and
    the whole point is for it to travel.
    """
    if not run_dir:
        try:
            from app.diagnostics import _default_run_dir
            run_dir = _default_run_dir()
        except Exception:
            db = os.environ.get("SLEEPCTL_DB", "")
            run_dir = os.path.join(os.path.dirname(db) if db else os.getcwd(), ".run")
    try:
        with open(os.path.join(run_dir, "funnel.url"), "r", encoding="utf-8") as fh:
            url = fh.read().strip()
        return url or None
    except Exception:
        return None


def build_health_snapshot(repo, run_dir: str | None = None, now: datetime | None = None) -> dict:
    """Build the scrubbed operational-health snapshot dict for publishing.

    Runs the diagnostics battery, whitelist-copies only operational fields into a fixed schema,
    then runs the whole thing through ``scrub`` so nothing secret-shaped can leak. If diagnostics
    can't run (import broken / it somehow raised), returns a minimal ``verdict="unknown"``
    snapshot so publishing never fails on a diag hiccup.
    """
    generated = _iso(now)
    if _run_diagnostics is None:
        return {
            "schema": SCHEMA,
            "verdict": "unknown",
            "error": "run_diagnostics unavailable (app.diagnostics import failed)",
            "generated_utc": generated,
        }
    try:
        diag = _run_diagnostics(repo, run_dir)
    except Exception as exc:  # run_diagnostics shouldn't raise, but never let publishing fail
        return {
            "schema": SCHEMA,
            "verdict": "unknown",
            "error": repr(exc),
            "generated_utc": generated,
        }

    if not isinstance(diag, dict):
        diag = {}

    version = diag.get("version")
    if not isinstance(version, dict):
        version = {}

    snapshot = {
        "schema": SCHEMA,
        "generated_utc": generated,
        "verdict": diag.get("verdict"),
        "headline": diag.get("headline"),
        "primary_remedy": diag.get("primary_remedy"),
        "version": {"sha": version.get("sha"), "branch": version.get("branch")},
        "checks": _copy_checks(diag.get("checks")),
        "playbook_matches": _copy_playbook_matches(diag.get("playbook_matches")),
        "preflight": _preflight_block(repo, diag.get("checks")),
        "log_tails": _log_tails(run_dir),
        "funnel_url": _funnel_url(run_dir),
    }
    return scrub(snapshot)


def _preflight_block(repo, checks) -> dict:
    """The GO / NO-GO verdict, alongside the raw checks.

    This branch is the ONLY window into the box from off-site, and a list of check statuses is not
    an answer to the question an off-site reader actually has: can it do its job tonight? The
    battery's own verdict doesn't answer it either — DEGRADED spans "the web UI is down" and "the
    bed is receiving no commands at all", and HEALTHY covers dry-run mode, where every check is
    green and nothing is being steered.

    Built from the checks ALREADY computed above rather than a second battery, so the verdict can
    never disagree with the checks published beside it. Operational metadata only: ids, titles and
    the same details already in ``checks``. Best-effort — a failure here degrades to
    ``available: false`` rather than costing us the whole snapshot."""
    try:
        from sleepctl.preflight import evaluate
    except Exception as exc:
        return {"available": False, "error": f"preflight unavailable: {exc!r}"}
    try:
        rep = evaluate(repo, want_sensor=True, checks=checks)
    except Exception as exc:
        return {"available": False, "error": repr(exc)}

    def _items(xs):
        return [{"id": i.id, "title": i.title, "detail": i.detail, "remedy": i.remedy}
                for i in xs]

    return {
        "available": True,
        "verdict": rep.verdict,
        "blocking": _items(rep.blocking),
        "degraded": _items(rep.degraded),
    }


def snapshot_json_bytes(snapshot: dict) -> bytes:
    """Deterministic, stable-ordered JSON encoding (+ trailing newline) for git-friendly diffs."""
    return json.dumps(snapshot, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"


def _build_repo(db_path: str):
    """Open a sleepctl ``Repository`` (with the dashboard-only tables applied) over ``db_path``.

    Mirrors how the API's test fixtures / ``app.db`` build a repo: a ``Repository`` for the
    sleep-data reads (``.recent_events``/``.state_history``/``.conn``) with the dashboard DDL +
    migrations layered on so ``runtime_state`` and friends exist for ``read_runtime_state``.
    """
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    repo = Repository(db_path, check_same_thread=False)
    try:
        repo.conn.executescript(app_db._DASHBOARD_DDL)
        app_db._apply_migrations(repo.conn)
        repo.conn.commit()
    except Exception:
        # dashboard tables are best-effort here -- diagnostics degrades gracefully if a table is
        # missing, so never fail snapshot construction over a DDL hiccup.
        pass
    return repo


def _write_error_snapshot(out_path: str, exc: BaseException) -> None:
    payload = {
        "schema": SCHEMA,
        "verdict": "unknown",
        "error": repr(exc),
        "generated_utc": _iso(None),
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    except Exception:
        pass
    with open(out_path, "wb") as fh:
        fh.write(snapshot_json_bytes(payload))


def write_snapshot(db_path: str, out_path: str, run_dir: str | None = None) -> str:
    """Build the snapshot from ``db_path`` and write the JSON to ``out_path``; return ``out_path``.

    A normal empty/degraded snapshot is a success (exit 0). A HARD failure (couldn't open the DB,
    couldn't write the file, etc.) still writes a minimal error snapshot to ``out_path`` and then
    raises ``SystemExit(1)`` so the PowerShell layer sees a non-zero exit and records a FAIL.
    """
    repo = None
    try:
        repo = _build_repo(db_path)
        snapshot = build_health_snapshot(repo, run_dir=run_dir)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as fh:
            fh.write(snapshot_json_bytes(snapshot))
        return out_path
    except Exception as exc:
        # hard failure: still leave an error snapshot behind, then signal failure via exit code
        try:
            _write_error_snapshot(out_path, exc)
        except Exception:
            pass
        raise SystemExit(1)
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("usage: python -m app.health_snapshot <db_path> <out_path> [run_dir]\n")
        raise SystemExit(2)
    _db_path = sys.argv[1]
    _out_path = sys.argv[2]
    _run_dir = sys.argv[3] if len(sys.argv) > 3 else None
    _result = write_snapshot(_db_path, _out_path, run_dir=_run_dir)
    print(_result)
