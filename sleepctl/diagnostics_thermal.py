"""Water-loop / thermal-capacity health, external-controller conflict, and frozen-telemetry
detection.

Three failure modes were only discovered in a live debugging session because nothing was
watching for them automatically:

  * an AIR-BOUND water loop (weak cooling/heating — the Pod is "running" but barely moving
    the bed's actual temperature),
  * a STUCK prime (``priming`` stayed True for many minutes, ``lastPrime`` never advanced),
  * FROZEN telemetry (``bed_temp_f``/``device_level`` stuck at one value for a long time
    because the daemon was crash-looping, not because the bed was actually steady), and
  * a COMPETING controller (Eight Sleep's own app schedule holding a target that fights this
    controller's commanded level).

This module is the pure detection engine for all four: every function takes already-sampled
inputs (a ``device_status()``-shaped dict + a list of ``state_history``-shaped rows, see
``sleepctl.storage.repository.Repository.state_history``) plus an explicit ``now_iso`` where
"now" matters. There is no ``datetime.now()``/``time.time()``/random call anywhere in this
module, so a test can hand it a synthetic history and get an exact, reproducible verdict.
Callers (``dashboard/api/app/diagnostics.py``, ``dashboard/api/app/services.py``) own the
wall-clock and the DB reads; this module only reasons about the data it's given.

History-row shape (matches ``Repository.state_history()``, in ANY order — every function
here sorts by ``ts`` internally so callers don't have to care):
    {"ts": <iso str>, "target_level": <int|None> (OUR commanded level),
     "bed_temp_f": <float|None>,
     "extra": {"device_level": <int|None>, "device_target_level": <int|None>,
               "device": {...device_status()-shaped dict, see eightsleep_cloud.py...}}}

Every function is defensive: missing/malformed fields degrade to "ok"/"insufficient_data"
rather than raising or false-positiving, since a crashing or falsely-alarming health monitor
is worse than a quiet one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

# --------------------------------------------------------------------------------- thresholds
MIN_HISTORY_SAMPLES = 8          # below this, there isn't enough signal to call anything but ok
STUCK_PRIME_SECONDS = 360        # >6 min continuously priming without completing -> stuck
LOW_WATER_RECENT_HOURS = 4.0     # a lastLowWater within this many hours is still "recent"

CAPACITY_WINDOW_MINUTES = 20     # how far back to look for a sustained strong thermal command
STRONG_COMMAND_LEVEL = 70        # |target_level| >= this counts as a "strong" thermal ask
MIN_LEVEL_MOVEMENT = 15          # device_level must move at least this much to count as responding
MAX_BED_TEMP_DRIFT_F = 1.5       # bed_temp_f must move at least this much to count as responding

CONFLICT_LOOKBACK_SAMPLES = 8    # samples examined for a repeated commanded-vs-accepted mismatch
CONFLICT_LEVEL_DELTA = 15        # |commanded - accepted| >= this counts as a disagreement
CONFLICT_MIN_DISAGREEMENTS = 4   # this many disagreements in the lookback window -> conflict

FROZEN_MIN_SPAN_SECONDS = 300    # the frozen window must span at least ~5 min to be meaningful
NEUTRAL_LEVEL_BAND = 5           # |target_level| within this band counts as "no active control"


# --------------------------------------------------------------------------------- helpers
def _result(status: str, reason: str, remedy: str = "") -> dict:
    return {"status": status, "reason": reason, "remedy": remedy}


def _to_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp, normalizing to tz-AWARE (assume UTC when naive).

    Callers in this codebase are inconsistent about naive-vs-aware timestamps (the daemon's
    ``state_history`` rows are typically naive local time via ``datetime.now()``; a caller
    computing "now" may pass a UTC-aware ``datetime.now(timezone.utc).isoformat()``). Without
    normalizing, subtracting a naive from an aware datetime raises ``TypeError`` and would
    crash every duration calculation in this module. Same defensive pattern as
    ``app.diagnostics._age_seconds_iso``."""
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone as _timezone
            dt = dt.replace(tzinfo=_timezone.utc)
        return dt
    except Exception:
        return None


def _row_extra(row: dict) -> dict:
    extra = (row or {}).get("extra") or {}
    return extra if isinstance(extra, dict) else {}


def _row_device(row: dict) -> dict:
    d = _row_extra(row).get("device") or {}
    return d if isinstance(d, dict) else {}


def _row_device_level(row: dict) -> Optional[float]:
    extra = _row_extra(row)
    lvl = extra.get("device_level")
    if lvl is None:
        lvl = _row_device(row).get("device_level")
    return lvl if isinstance(lvl, (int, float)) else None


def _row_device_target_level(row: dict) -> Optional[float]:
    extra = _row_extra(row)
    lvl = extra.get("device_target_level")
    if lvl is None:
        lvl = _row_device(row).get("device_target_level")
    return lvl if isinstance(lvl, (int, float)) else None


def _sorted_by_time(history: list[dict]) -> list[dict]:
    def key(r: dict) -> datetime:
        return _to_dt((r or {}).get("ts")) or datetime.min

    return sorted(history or [], key=key)


def _window(rows: list[dict], now: Optional[datetime], minutes: float) -> list[dict]:
    if now is None:
        return rows
    cutoff = now - timedelta(minutes=minutes)
    return [r for r in rows if (_to_dt((r or {}).get("ts")) or now) >= cutoff]


def _priming_duration_s(rows: list[dict], now: Optional[datetime]) -> Optional[float]:
    """How long (seconds) ``device.priming`` has been continuously True, walking backward
    from the newest sample. ``None`` when the history doesn't confirm a contiguous priming
    episode reaching up to ``now`` (e.g. no history, or the most recent sample isn't priming
    — priming may have JUST started, which isn't "stuck" yet)."""
    if now is None or not rows:
        return None
    start_ts: Optional[datetime] = None
    for r in reversed(rows):  # rows ascending by time -> reversed is newest-first
        if _row_device(r).get("priming"):
            ts = _to_dt(r.get("ts"))
            if ts is not None:
                start_ts = ts
        else:
            break
    if start_ts is None:
        return None
    return (now - start_ts).total_seconds()


# --------------------------------------------------------------------------------- #1 capacity
def analyze_thermal_capacity(device: dict, history: list[dict], now_iso: str) -> dict:
    """Water-loop / thermal-capacity health: stuck prime, low water, or an air-bound loop that
    can't move bed temperature despite a strong commanded target.

    Returns ``{"status": ..., "reason": ..., "remedy": ...}`` where ``status`` is one of
    ``"stuck_prime"``, ``"low_water"``, ``"reduced_capacity"``, ``"ok"``, or
    ``"insufficient_data"``. Checked in that priority order — a stuck prime or empty
    reservoir is a more specific/urgent diagnosis than a general "not responding" verdict.
    """
    device = device or {}
    rows = _sorted_by_time(history or [])
    now = _to_dt(now_iso)
    if now is None and rows:
        now = _to_dt(rows[-1].get("ts"))

    # ---- 1. stuck prime: priming=True for longer than it should ever take -----------------
    if device.get("priming"):
        duration = _priming_duration_s(rows, now)
        if duration is not None and duration >= STUCK_PRIME_SECONDS:
            return _result(
                "stuck_prime",
                f"the Pod has been priming continuously for {duration / 60.0:.1f} min "
                f"(> {STUCK_PRIME_SECONDS / 60.0:.0f} min) without completing "
                f"(last_prime={device.get('last_prime')!r}).",
                "Prime is not completing — the water loop is likely air-bound. Top off the "
                "reservoir with distilled water, reseat the hub↔cover connectors, then "
                "re-prime.",
            )

    # ---- 2. low water: explicit flag, or a recent low-water event -------------------------
    if device.get("needs_priming"):
        return _result(
            "low_water",
            "the device reports needs_priming=true.",
            "Top off the reservoir with distilled water.",
        )
    last_low_water = _to_dt(device.get("last_low_water"))
    if last_low_water is not None and now is not None:
        age_h = (now - last_low_water).total_seconds() / 3600.0
        if 0 <= age_h <= LOW_WATER_RECENT_HOURS:
            return _result(
                "low_water",
                f"a low-water event was reported {age_h:.1f}h ago (last_low_water="
                f"{device.get('last_low_water')!r}).",
                "Top off the reservoir with distilled water.",
            )

    # ---- 3. reduced capacity / air-bound: strong command, weak response -------------------
    windowed = _window(rows, now, CAPACITY_WINDOW_MINUTES) if rows else rows
    if len(windowed) < MIN_HISTORY_SAMPLES:
        return _result(
            "insufficient_data",
            f"only {len(windowed)} history sample(s) in the last {CAPACITY_WINDOW_MINUTES} "
            "min — not enough to assess thermal capacity yet.",
        )

    strong_rows = [
        r for r in windowed
        if isinstance(r.get("target_level"), (int, float))
        and abs(r["target_level"]) >= STRONG_COMMAND_LEVEL
    ]
    if len(strong_rows) >= MIN_HISTORY_SAMPLES:
        levels = [lv for lv in (_row_device_level(r) for r in strong_rows) if lv is not None]
        temps = [t for t in (r.get("bed_temp_f") for r in strong_rows)
                 if isinstance(t, (int, float))]
        if len(levels) >= MIN_HISTORY_SAMPLES and len(temps) >= MIN_HISTORY_SAMPLES:
            level_move = max(levels) - min(levels)
            temp_move = max(temps) - min(temps)
            if level_move < MIN_LEVEL_MOVEMENT and temp_move < MAX_BED_TEMP_DRIFT_F:
                direction = "cool" if strong_rows[-1]["target_level"] < 0 else "warm"
                return _result(
                    "reduced_capacity",
                    f"commanded a strong {direction} target (level ~{strong_rows[-1]['target_level']:.0f}) "
                    f"for {len(strong_rows)} samples, but device_level moved only "
                    f"{level_move:.0f} and bed_temp_f moved only {temp_move:.1f}°F.",
                    "Bed isn't responding to strong thermal commands — reduced heat-transfer "
                    "capacity (often air in the water loop after a leak/low-water event). "
                    "Purge air: top off distilled water, reseat connectors, re-prime 2-4x.",
                )

    return _result("ok", "no water-loop/thermal-capacity issue detected.")


# --------------------------------------------------------------------------------- #2 conflict
def detect_external_conflict(device: dict, history: list[dict]) -> dict:
    """External-controller conflict: the Eight Sleep app's own schedule (or another
    controller) holding a setpoint that fights this controller's commanded level.

    Returns ``{"status": "external_setpoint_conflict"|"ok"|"insufficient_data", "reason",
    "remedy"}``. Two independent signals, checked in order:
      1. the device's own schedule is active AND its target level DISAGREES with what we
         commanded (a schedule/other app holding a different setpoint), or
      2. the device's *accepted* target level repeatedly disagrees with what WE commanded
         over several recent samples (a schedule/other app silently overriding it).

    Note: ``external_schedule.activity`` reads ``"schedule"`` on the Pod whenever ANY smart
    session is active -- including when that session is faithfully HONORING our commanded
    override (its ``target_level`` == ours). An active schedule is therefore not a conflict
    by itself; it only conflicts when its target disagrees with ours.
    """
    device = device or {}
    rows = _sorted_by_time(history or [])

    schedule = device.get("external_schedule") or {}
    if isinstance(schedule, dict) and (schedule.get("activity") == "schedule"
                                        or schedule.get("active") is True):
        target = schedule.get("target_level")
        # our most recently commanded level (freshest history row that has one)
        commanded = next((r.get("target_level") for r in reversed(rows)
                          if isinstance(r.get("target_level"), (int, float))), None)
        honoring = (isinstance(target, (int, float)) and commanded is not None
                    and abs(target - commanded) < CONFLICT_LEVEL_DELTA)
        if not honoring:
            held = f" (holding ~{target})" if target is not None else ""
            return _result(
                "external_setpoint_conflict",
                f"the device's own schedule is active (activity={schedule.get('activity')!r}"
                + (f", target_level={target}" if target is not None else "")
                + (f") and disagrees with our commanded {commanded}." if commanded is not None else ")."),
                f"The Eight Sleep app's own schedule (or another controller) is fighting the "
                f"setpoint{held}. Turn off the schedule/Autopilot in the Eight Sleep app so this "
                "controller has sole control.",
            )
        # schedule is active but honoring our override -> not a conflict; fall through to the
        # multi-sample disagreement check below (which returns ok / insufficient_data).

    if len(rows) < MIN_HISTORY_SAMPLES:
        return _result(
            "insufficient_data",
            f"only {len(rows)} history sample(s) — not enough to assess setpoint conflicts "
            "yet.",
        )

    recent = rows[-CONFLICT_LOOKBACK_SAMPLES:]
    disagreements = []
    for r in recent:
        commanded = r.get("target_level")
        accepted = _row_device_target_level(r)
        if isinstance(commanded, (int, float)) and accepted is not None:
            if abs(commanded - accepted) >= CONFLICT_LEVEL_DELTA:
                disagreements.append(accepted)

    if len(disagreements) >= CONFLICT_MIN_DISAGREEMENTS:
        observed = disagreements[-1]
        return _result(
            "external_setpoint_conflict",
            f"the device's accepted target repeatedly disagreed with our commanded target in "
            f"{len(disagreements)} of the last {len(recent)} samples (observed external "
            f"target ~{observed:.0f}).",
            f"The Eight Sleep app's own schedule (or another controller) is fighting the "
            f"setpoint (holding ~{observed:.0f}). Turn off the schedule/Autopilot in the "
            "Eight Sleep app so this controller has sole control.",
        )

    return _result("ok", "no external-controller conflict detected.")


# --------------------------------------------------------------------------------- #3 frozen
def detect_frozen_telemetry(history: list[dict]) -> dict:
    """Frozen-telemetry guard: ``bed_temp_f`` AND ``device_level`` byte-for-byte unchanged
    across a real span of wall-clock time WHILE the daemon was commanding a non-neutral
    target — i.e. the readings are frozen, not just genuinely steady (which would happen at
    a neutral/hold target and is not a bug).

    Returns ``{"status": "frozen_telemetry"|"ok"|"insufficient_data", "reason", "remedy"}``.
    """
    rows = _sorted_by_time(history or [])
    if len(rows) < MIN_HISTORY_SAMPLES:
        return _result(
            "insufficient_data",
            f"only {len(rows)} history sample(s) — not enough to assess telemetry freshness "
            "yet.",
        )

    window = rows[-MIN_HISTORY_SAMPLES:]
    start_ts = _to_dt(window[0].get("ts"))
    end_ts = _to_dt(window[-1].get("ts"))
    if start_ts is None or end_ts is None:
        return _result("ok", "timestamps unavailable in history — cannot assess.")

    span_s = (end_ts - start_ts).total_seconds()
    if span_s < FROZEN_MIN_SPAN_SECONDS:
        return _result(
            "insufficient_data",
            f"the last {len(window)} samples span only {span_s:.0f}s "
            f"(< {FROZEN_MIN_SPAN_SECONDS:.0f}s) — too short a window to call telemetry "
            "frozen.",
        )

    bed_temps = [t for t in (r.get("bed_temp_f") for r in window) if t is not None]
    levels = [lv for lv in (_row_device_level(r) for r in window) if lv is not None]
    commanded = [c for c in (r.get("target_level") for r in window) if isinstance(c, (int, float))]

    bed_temp_frozen = bool(bed_temps) and len(set(bed_temps)) == 1
    level_frozen = bool(levels) and len(set(levels)) == 1
    active_control = any(abs(c) > NEUTRAL_LEVEL_BAND for c in commanded)

    if bed_temp_frozen and level_frozen and active_control:
        minutes = span_s / 60.0
        return _result(
            "frozen_telemetry",
            f"bed_temp_f ({bed_temps[0]}) and device_level ({levels[0]}) are byte-for-byte "
            f"unchanged across the last {len(window)} samples ({minutes:.0f} min) despite a "
            "non-neutral commanded target.",
            f"Bed temperature/level readings haven't changed in {minutes:.0f} min despite "
            "active control — the daemon may be wedged or telemetry stale. Restart the "
            "daemon (POST /diag/action/restart?target=daemon) and check daemon logs.",
        )

    return _result("ok", "telemetry is updating normally.")
