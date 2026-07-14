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
    }
    return scrub(snapshot)


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
