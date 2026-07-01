"""Controller/bed health evaluator.

Goal #2: the live path can fail SILENTLY (a daemon crash, a stalled thermal loop, an
empty water tank) and nobody notices for hours because nothing pushes a signal to the
user. This module is the pure decision layer: given the current ``runtime_state`` (the
same dict ``bridge.read_runtime_state`` returns) it answers "what's wrong right now?"
as a list of issues with a stable ``code`` (for de-dupe/clear tracking), a
``severity``, and a human ``message``.

Kept dependency-free and side-effect-free on purpose: no DB, no network, no imports
from ``app.*`` or ``sleepctl.*`` beyond stdlib. That makes it trivial to unit-test
every issue type in isolation and lets ``services.py`` (or anything else) call it with
a hand-built dict in tests without a database at all.

Callers (``services.generate_alerts``) are responsible for turning these issues into
persisted alert rows (raise-on-appear / clear-on-resolve) and for deciding which
severities are "critical enough" to push to the phone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Severity ordering, weakest to strongest — used by callers that need to pick "the worst".
SEVERITY_ORDER = ("info", "warning", "critical")

# How many consecutive cloud/device errors before we treat it as a real outage rather
# than a one-off transient hiccup (matches the kind of blip the live daemon already
# retries through silently).
_REPEATED_ERROR_THRESHOLD = 3


def _age_seconds(updated: str | None) -> float | None:
    if not updated:
        return None
    try:
        ts = datetime.fromisoformat(updated)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def evaluate_health(
    runtime_state: dict[str, Any],
    recent_errors: list[str] | None = None,
    stale_seconds: int = 180,
) -> list[dict[str, str]]:
    """Pure function: runtime_state (+ optional recent error signals) -> health issues.

    ``runtime_state`` is the dict shape produced by ``bridge.read_runtime_state``:
    top-level ``daemon_alive``/``stale``/``updated`` plus an ``extra`` dict carrying
    ``thermal_health``, ``device``, ``data_age_s``, ``telemetry_stale``, ``device_error``.

    ``recent_errors`` is an optional list of recent error strings/codes (e.g. from a
    command/error log) used for the "repeated cloud errors" check when the daemon
    itself doesn't already surface it via ``extra.device_error``.

    Returns a list of ``{"code": str, "severity": "info"|"warning"|"critical",
    "message": str}`` dicts. Empty list == healthy. Never raises: a malformed/partial
    runtime_state degrades to "unknown" issues rather than crashing the request path
    that calls this on every poll.
    """
    issues: list[dict[str, str]] = []
    if not isinstance(runtime_state, dict):
        return issues

    extra = runtime_state.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}

    # ---- daemon DOWN / stale -------------------------------------------------------
    stale = bool(runtime_state.get("stale"))
    daemon_alive = runtime_state.get("daemon_alive")
    age = _age_seconds(runtime_state.get("updated"))
    if runtime_state.get("updated") is None:
        issues.append({
            "code": "daemon_down",
            "severity": "critical",
            "message": "No controller data has ever been reported — the daemon may not be running.",
        })
    elif daemon_alive is False or stale:
        age_txt = f"{int(age // 60)} min" if age is not None and age >= 60 else (
            f"{int(age)}s" if age is not None else "an unknown time"
        )
        issues.append({
            "code": "daemon_down",
            "severity": "critical",
            "message": f"Controller daemon hasn't reported in {age_txt} — it may be down.",
        })

    # ---- thermal STALLED -------------------------------------------------------------
    thermal = extra.get("thermal_health") or {}
    if isinstance(thermal, dict) and thermal.get("state") == "stalled":
        reason = thermal.get("reason") or "bed temperature isn't responding to commands"
        issues.append({
            "code": "thermal_stalled",
            "severity": "critical",
            "message": f"Thermal control appears stalled: {reason}.",
        })

    # ---- NO WATER ----------------------------------------------------------------
    device = extra.get("device") or {}
    if isinstance(device, dict) and device.get("has_water") is False:
        issues.append({
            "code": "no_water",
            "severity": "critical",
            "message": "Water reservoir is empty — the bed can't heat or cool. Refill and prime.",
        })
    if isinstance(device, dict) and device.get("online") is False:
        issues.append({
            "code": "device_offline",
            "severity": "critical",
            "message": "The bed/hub is reporting offline.",
        })

    # ---- telemetry stale (device is reachable but data is old) --------------------
    if extra.get("telemetry_stale"):
        data_age = extra.get("data_age_s")
        age_txt = f"{int(data_age)}s" if isinstance(data_age, (int, float)) else "a while"
        issues.append({
            "code": "telemetry_stale",
            "severity": "warning",
            "message": f"Sensor telemetry is stale (last update {age_txt} ago).",
        })

    # ---- repeated cloud/device errors ----------------------------------------------
    device_error = extra.get("device_error")
    if device_error:
        issues.append({
            "code": "device_error",
            "severity": "warning",
            "message": f"Device/cloud error reported: {device_error}.",
        })
    if recent_errors and len(recent_errors) >= _REPEATED_ERROR_THRESHOLD:
        issues.append({
            "code": "repeated_cloud_errors",
            "severity": "critical",
            "message": (
                f"{len(recent_errors)} consecutive cloud/device errors — the connection to "
                "the bed looks broken."
            ),
        })

    return issues


def worst_severity(issues: list[dict[str, str]]) -> str | None:
    """The single worst severity present, or None if ``issues`` is empty."""
    if not issues:
        return None
    return max(issues, key=lambda i: SEVERITY_ORDER.index(i.get("severity", "info"))).get("severity")


def is_critical(issue: dict[str, str]) -> bool:
    return issue.get("severity") == "critical"
