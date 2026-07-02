"""Diagnostic bundle builder -- "send this to Claude" (Feature #5).

Gathers everything needed to diagnose an issue into ONE artifact instead of a maintainer
manually pulling ``/diag``, ``/diag/events``, and several ``/diag/logs`` calls by hand: the
full ``/diag`` JSON verdict, recent structured events, tails of every whitelisted log,
``.run/*.result``/``.run/*.alert`` files, the daemon/watchdog heartbeat ages, and a REDACTED
snapshot of the deploy config (env keys present + non-secret values only).

Used by two callers that must stay in sync on sections + redaction rules:
  * ``GET /diag/bundle`` (``dashboard/api/app/main.py``) -- when the API is reachable.
  * ``scripts/collect-diagnostics.ps1`` -- a standalone PowerShell re-implementation for when
    the API itself is down and can't be asked to build its own bundle.

Redaction is conservative and purely key-name-based (case-insensitive substring match) so it
never depends on recognizing what a particular secret looks like: any env var whose KEY
contains PASSWORD, SECRET, TOKEN, ICS_URL, CLIENT_SECRET, or JWT is rendered as
``<redacted>`` -- its value is never read into the bundle at all.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

# ------------------------------------------------------------------ redaction
_SECRET_KEY_PATTERN = re.compile(r"PASSWORD|SECRET|TOKEN|ICS_URL|CLIENT_SECRET|JWT", re.IGNORECASE)

# Every env var this project actually reads (see deploy/.env.example, app/config.py, and the
# sleepctl adapters) -- a curated whitelist, NOT the full process environment, so unrelated
# host/system env vars can never leak into the bundle.
ENV_KEYS_OF_INTEREST = [
    "SLEEPCTL_DB", "SLEEPCTL_LIVE", "SLEEPCTL_DRY_RUN", "SLEEPCTL_LAT", "SLEEPCTL_LON",
    "SLEEPCTL_WEATHER", "SLEEPCTL_PHONE_SENSOR",
    "DASHBOARD_USER", "DASHBOARD_PASSWORD",
    "JWT_SECRET", "JWT_TTL_HOURS", "JWT_REMEMBER_HOURS", "JWT_SESSION_HOURS",
    "CORS_ORIGINS", "BCG_INGEST_OPEN",
    "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT",
    "EIGHTSLEEP_EMAIL", "EIGHTSLEEP_PASSWORD", "EIGHTSLEEP_SIDE", "EIGHTSLEEP_TIMEZONE",
    "EIGHTSLEEP_CLIENT_ID", "EIGHTSLEEP_CLIENT_SECRET", "EIGHTSLEEP_CREDENTIALS",
    "CALENDAR_ICS_URL", "DIAG_TOKEN", "TZ",
]

# Same whitelist ``app.main._DIAG_LOG_FILES`` uses -- duplicated (not imported) so this module
# has no import-time dependency on main.py (avoids a circular import: main.py imports THIS
# module to serve /diag/bundle). Keep the two lists in sync if either changes.
_DIAG_LOG_FILES = {
    "daemon": "daemon.log",
    "daemon-err": "daemon.err",
    "daemon-crash": "daemon-crash.log",
    "watchdog": "watchdog.log",
    "api": "api.log",
    "api-err": "api.err",
    "web": "web.log",
    "web-build": "web-build.log",
}

DEFAULT_TAIL_LINES = 150
DEFAULT_MAX_BYTES = 1_000_000  # ~1MB text-doc cap


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_PATTERN.search(key or ""))


def redacted_env_lines(env: Optional[dict] = None) -> list[str]:
    """One ``KEY = value`` line per key of interest. Secret-looking keys always render as
    ``<redacted>`` (or ``(unset)`` if absent) -- their real value is never read into this
    list. Everything else shows its actual (non-secret) value, or ``(unset)``."""
    env = env if env is not None else os.environ
    lines = []
    for key in ENV_KEYS_OF_INTEREST:
        raw = env.get(key)
        present = bool(raw)
        if is_secret_key(key):
            lines.append(f"{key} = {'<redacted>' if present else '(unset)'}")
        else:
            lines.append(f"{key} = {raw if present else '(unset)'}")
    return lines


# ------------------------------------------------------------------ small file helpers
def _tail_text(path: str, n: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:]).rstrip() or "(empty)"
    except FileNotFoundError:
        return "(file not found)"
    except Exception as exc:
        return f"(could not read: {exc})"


def _read_small_file(path: str, max_chars: int = 20_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_chars + 1)
        if len(data) > max_chars:
            data = data[:max_chars] + "\n...(truncated)"
        return data
    except Exception as exc:
        return f"(could not read: {exc})"


def _heartbeat_age(run_dir: str, name: str, now: float) -> Optional[float]:
    try:
        return now - os.path.getmtime(os.path.join(run_dir, f"{name}.heartbeat"))
    except OSError:
        return None


def _result_and_alert_files(run_dir: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        paths = sorted(glob.glob(os.path.join(run_dir, "*.result"))) + \
                sorted(glob.glob(os.path.join(run_dir, "*.alert")))
    except Exception:
        paths = []
    for p in paths:
        out[os.path.basename(p)] = _read_small_file(p)
    return out


# ------------------------------------------------------------------ collection
def collect_bundle(repo, run_dir: str, tail_lines: int = DEFAULT_TAIL_LINES,
                   events_limit: int = 200, env: Optional[dict] = None) -> dict:
    """Gather every section's raw data as a plain dict (JSON-friendly) -- the single source of
    truth both ``render_bundle_text`` and ``render_bundle_files`` format from, so the text and
    zip outputs can never drift apart."""
    from app.diagnostics import run_diagnostics

    now = time.time()
    diag = run_diagnostics(repo, run_dir=run_dir)

    try:
        events = repo.recent_events(limit=events_limit)
    except Exception:
        events = []

    logs = {name: _tail_text(os.path.join(run_dir, fname), tail_lines)
            for name, fname in _DIAG_LOG_FILES.items()}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "diag": diag,
        "events": events,
        "logs": logs,
        "result_and_alert_files": _result_and_alert_files(run_dir),
        "heartbeats": {
            "daemon_heartbeat_age_s": _heartbeat_age(run_dir, "daemon", now),
            "watchdog_heartbeat_age_s": _heartbeat_age(run_dir, "watchdog", now),
        },
        "config_redacted": redacted_env_lines(env),
    }


# ------------------------------------------------------------------ text rendering
def render_bundle_text(data: dict, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Render ``collect_bundle()``'s data as one clearly-sectioned plain-text document, capped
    at ``max_bytes`` (default ~1MB) so a huge log dump can't produce an unusable paste."""
    from app.diagnostics import render_diagnosis_text

    parts: list[str] = []

    def section(title: str, body: str) -> None:
        parts.append(f"===== {title} =====\n{body}".rstrip())

    section("SLEEPCONTROLLER DIAGNOSTIC BUNDLE",
             f"generated_at = {data.get('generated_at')}\n"
             "This is a single self-contained diagnostic snapshot -- paste/upload the whole "
             "thing when asking for help debugging.")

    section("DIAGNOSIS (summary)", render_diagnosis_text(data.get("diag") or {}))
    section("DIAG (full JSON)", json.dumps(data.get("diag") or {}, indent=2, default=str))

    hb = data.get("heartbeats") or {}
    da = hb.get("daemon_heartbeat_age_s")
    wa = hb.get("watchdog_heartbeat_age_s")
    section("HEARTBEATS",
            f"daemon.heartbeat age = {'%.1fs' % da if da is not None else 'MISSING'}\n"
            f"watchdog.heartbeat age = {'%.1fs' % wa if wa is not None else 'MISSING'}")

    section("RECENT EVENTS (JSON)", json.dumps(data.get("events") or [], indent=2, default=str))

    result_files = data.get("result_and_alert_files") or {}
    if result_files:
        body = "\n\n".join(f"-- {name} --\n{content}" for name, content in result_files.items())
    else:
        body = "(none found -- no .run/*.result or .run/*.alert files)"
    section("RESULT / ALERT FILES", body)

    for name, content in (data.get("logs") or {}).items():
        section(f"LOG: {name}", content)

    section("CONFIG SNAPSHOT (redacted -- secret values NEVER included)",
            "\n".join(data.get("config_redacted") or []))

    out = "\n\n".join(parts) + "\n"
    encoded = out.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        out = encoded[:max_bytes].decode("utf-8", errors="ignore") + \
              "\n\n===== TRUNCATED: bundle exceeded ~1MB; use ?format=zip for the untruncated " \
              "individual files =====\n"
    return out


# ------------------------------------------------------------------ zip file rendering
def render_bundle_files(data: dict) -> dict[str, bytes]:
    """Render ``collect_bundle()``'s data as individual files (name -> bytes), suitable for
    zipping. No size cap here -- the zip route is explicitly for "give me everything,
    untruncated"."""
    files: dict[str, bytes] = {}
    files["diag.json"] = json.dumps(data.get("diag") or {}, indent=2, default=str).encode()
    files["diagnosis_summary.txt"] = _diag_summary_text(data).encode()
    files["events.json"] = json.dumps(data.get("events") or [], indent=2, default=str).encode()
    files["heartbeats.txt"] = _heartbeats_text(data).encode()
    files["config_redacted.txt"] = ("\n".join(data.get("config_redacted") or []) + "\n").encode()

    for name, content in (data.get("result_and_alert_files") or {}).items():
        files[f"results/{name}"] = content.encode(errors="replace")

    for name, content in (data.get("logs") or {}).items():
        files[f"logs/{name}.log.tail.txt"] = content.encode(errors="replace")

    return files


def _diag_summary_text(data: dict) -> str:
    from app.diagnostics import render_diagnosis_text
    return render_diagnosis_text(data.get("diag") or {})


def _heartbeats_text(data: dict) -> str:
    hb = data.get("heartbeats") or {}
    da = hb.get("daemon_heartbeat_age_s")
    wa = hb.get("watchdog_heartbeat_age_s")
    return (f"daemon.heartbeat age = {'%.1fs' % da if da is not None else 'MISSING'}\n"
            f"watchdog.heartbeat age = {'%.1fs' % wa if wa is not None else 'MISSING'}\n")
