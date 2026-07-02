"""Data + learning + config health doctor.

Pure, read-only, engine-level diagnostics that answer one question: **is the DATA and ML
side of sleepctl healthy?** This is deliberately the complement of a live-runtime doctor
(device/daemon/telemetry health, built separately against the dashboard API) — this module
never imports from ``dashboard`` and never touches a daemon or a live device. It only reads
the SQLite dataset (via ``Repository``) and the static ``AppConfig``.

Entry point: :func:`data_diagnostics`. It is defensive by construction — every individual
check is wrapped so a missing table, an empty database, or an unexpected schema can never
raise; at worst a check reports ``"info"`` with an explanatory detail. This makes it safe to
run against a brand-new (even empty/``:memory:``) database from the CLI or a test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

# Below this many logged nights, the overall verdict is NEEDS_DATA (unless something has
# actively FAILED, which is a real problem worth DEGRADED regardless of data volume).
_NEEDS_DATA_NIGHTS = 3

# Nights table considered "stale" (no fresh data) once the most recent night is this old.
_STALE_GAP_DAYS = 2

_KEY_TABLES = ("nightly_summaries", "raw_samples", "decisions", "actions")

_COMPLETENESS_FIELDS = ("wake_events", "deep_min", "sleep_efficiency", "avg_hrv")


# --------------------------------------------------------------------------------- helpers


def _check(id_: str, title: str, status: str, detail: str, remedy: str = "") -> dict:
    return {"id": id_, "title": title, "status": status, "detail": detail, "remedy": remedy}


def _safe_call(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _run_check(id_: str, title: str, fn: Callable[[], dict]) -> dict:
    """Run one check function; on ANY internal failure, degrade gracefully to an 'info'
    result rather than letting the whole report crash."""
    try:
        result = fn()
        if not isinstance(result, dict) or "status" not in result:
            return _check(id_, title, "info", "check returned an unexpected shape", "")
        return result
    except Exception as exc:
        return _check(id_, title, "info", f"check could not run: {exc}", "")


# --------------------------------------------------------------------------------- checks


def _check_db(repo) -> dict:
    try:
        existing = {
            r[0] for r in repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except Exception as exc:
        return _check(
            "db", "Database schema", "fail",
            f"could not query the SQLite schema: {exc}",
            "check the --db path points at a valid sleepctl SQLite file",
        )

    missing = [t for t in _KEY_TABLES if t not in existing]
    if missing:
        return _check(
            "db", "Database schema", "fail",
            f"missing key table(s): {', '.join(missing)}",
            "run any sleepctl command once against this DB path — Repository/init_db "
            "auto-creates the schema",
        )

    counts: dict[str, Optional[int]] = {}
    unqueryable = []
    for t in _KEY_TABLES:
        try:
            counts[t] = repo.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = None
            unqueryable.append(t)

    detail = ", ".join(f"{t}={counts[t]}" for t in _KEY_TABLES)
    if unqueryable:
        return _check(
            "db", "Database schema", "fail",
            f"table(s) present but not queryable: {', '.join(unqueryable)} ({detail})",
            "the schema may be corrupt or from an incompatible version — inspect the DB file",
        )
    return _check("db", "Database schema", "ok",
                  f"all key tables present and queryable ({detail})", "")


def _check_data_volume(repo, cfg) -> dict:
    nights = _safe_call(lambda: repo.all_nights(), []) or []
    n = len(nights)
    min_nights = _safe_call(lambda: cfg.ml.min_nights, 14) if cfg is not None else 14

    if n == 0:
        return _check(
            "data_volume", "Data volume", "info",
            "0 nights logged yet — nothing for the learners to learn from.",
            "run `sleepctl replay` (synthetic nights) or let `sleepctl run` log real nights",
        )
    if n < _NEEDS_DATA_NIGHTS:
        return _check(
            "data_volume", "Data volume", "info",
            f"only {n} night(s) logged — too few to assess trends or maturity yet.",
            f"log at least {_NEEDS_DATA_NIGHTS} nights before this report is meaningful",
        )
    if n < min_nights:
        return _check(
            "data_volume", "Data volume", "info",
            f"{n} night(s) logged; the ML gate needs >= {min_nights} clean nights "
            f"before it acts (config ml.min_nights={min_nights}).",
            f"keep logging nights — {min_nights - n} more before ML engages "
            "(rule-based policy runs meanwhile)",
        )
    return _check("data_volume", "Data volume", "ok",
                  f"{n} nights logged (>= ml.min_nights={min_nights}).", "")


def _check_data_completeness(repo) -> dict:
    nights = _safe_call(lambda: repo.recent_nights(14), []) or []
    if not nights:
        return _check("data_completeness", "Data completeness", "info",
                      "no nights logged yet to check completeness on.", "")

    total = len(nights)
    gap_counts = {f: 0 for f in _COMPLETENESS_FIELDS}
    for night in nights:
        for f in _COMPLETENESS_FIELDS:
            if getattr(night, f, None) is None:
                gap_counts[f] += 1
    gappy = {f: c for f, c in gap_counts.items() if c > 0}

    last_date = None
    gap_days = None
    try:
        dates = [datetime.fromisoformat(n.date) for n in nights if n.date]
        if dates:
            last_date = max(dates)
            gap_days = (datetime.now() - last_date).days
    except Exception:
        pass

    status = "ok"
    parts = []
    if gappy:
        status = "warn"
        parts.append(
            "missing fields in recent nights: "
            + ", ".join(f"{f}={c}/{total} nights" for f, c in gappy.items())
        )
    if gap_days is not None and gap_days > _STALE_GAP_DAYS:
        status = "warn"
        parts.append(f"stale — no night logged in {gap_days} days (last: {last_date.date()})")

    if not parts:
        recency = f"{gap_days} day(s) ago" if gap_days is not None else "unknown"
        parts.append(f"key fields populated across the last {total} night(s); "
                     f"most recent night logged {recency}")

    remedy = "" if status == "ok" else (
        "check the sensor feed / adapter — gaps or a stale gap starve the nightly "
        "learners of usable rows"
    )
    return _check("data_completeness", "Data completeness", status, "; ".join(parts), remedy)


def _check_learner_maturity(repo, cfg) -> dict:
    from sleepctl.learning.coordinator import learning_ledger

    entries = _safe_call(lambda: learning_ledger(repo, cfg), []) or []
    if not entries:
        return _check(
            "learner_maturity", "Learner maturity", "info",
            "the learning ledger returned no entries (fresh repo, or the coordinator "
            "module is unavailable).", "",
        )

    by_source: dict[str, list[dict]] = {}
    for e in entries:
        by_source.setdefault(e.get("source", "unknown"), []).append(e)
    n_preset = len(by_source.get("preset", []))
    n_learned = len(by_source.get("learned", []))
    n_measured = len(by_source.get("measured", []))
    total = len(entries)

    starved = [e["name"] for e in by_source.get("preset", []) if (e.get("maturity") or 0) < 5]

    status = "info"
    if total and (n_learned + n_measured) >= total / 2:
        status = "ok"
    elif starved:
        status = "warn" if (n_learned + n_measured) > 0 else "info"

    detail = f"{total} learner(s) tracked: {n_preset} preset, {n_learned} learned, {n_measured} measured."
    if starved:
        shown = ", ".join(starved[:6]) + ("…" if len(starved) > 6 else "")
        detail += f" Data-starved (still preset, low maturity): {shown}."

    remedy = "" if status == "ok" else (
        "most learners need roughly 12-30 nights of history to move off their preset "
        "defaults — keep logging nights"
    )
    return _check("learner_maturity", "Learner maturity", status, detail, remedy)


def _check_calibration(repo) -> dict:
    cal = _safe_call(lambda: repo.get_thermal_calibration())
    comfort = _safe_call(lambda: repo.get_comfort_profile())
    resting = _safe_call(lambda: repo.get_resting_baseline())

    missing = []
    if not cal:
        missing.append("thermal_calibration")
    if not comfort:
        missing.append("comfort_profile")
    if not resting:
        missing.append("resting_baseline")

    if not missing:
        return _check("calibration", "Personal calibration", "ok",
                      "thermal calibration, comfort profile, and resting baseline are all "
                      "present.", "")

    status = "info" if len(missing) == 3 else "warn"
    return _check(
        "calibration", "Personal calibration", status,
        f"missing measured calibration: {', '.join(missing)}",
        "run the on-bed self-test / comfort sweep (see scripts/in_bed_calibration.py) to "
        "measure these directly instead of relying on config defaults",
    )


def _check_setpoints_sane(repo, cfg) -> dict:
    sp = _safe_call(lambda: repo.latest_setpoints())
    if sp is None:
        return _check("setpoints_sane", "Setpoint sanity", "info",
                      "no learned setpoint saved yet — the controller is using config "
                      "defaults.", "")

    try:
        from sleepctl.ml.actions import KNOB_BOUNDS
    except Exception:
        KNOB_BOUNDS = {}

    problems = []
    for knob, bounds in KNOB_BOUNDS.items():
        val = getattr(sp, knob, None)
        if val is None:
            continue
        lo, hi = bounds
        if not (lo <= val <= hi):
            problems.append(f"{knob}={val} outside valid range [{lo}, {hi}]")

    if problems:
        return _check(
            "setpoints_sane", "Setpoint sanity", "fail",
            f"setpoint v{sp.version} (source={sp.source}) has out-of-bounds value(s): "
            + "; ".join(problems),
            "investigate the learner/action that produced this setpoint version; consider "
            "persisting a corrected profile via repo.save_setpoints(...)",
        )
    return _check("setpoints_sane", "Setpoint sanity", "ok",
                  f"setpoint v{sp.version} (source={sp.source}) is within the valid knob "
                  "bounds.", "")


def _check_config_sane(cfg) -> dict:
    if cfg is None:
        return _check("config_sane", "Config sanity", "info", "no config available to check.", "")
    t = cfg.tunables

    problems = []
    if not (0 < t.max_step_f <= 10):
        problems.append(f"max_step_f={t.max_step_f} looks unreasonable (expected 0-10)")
    if not (0 <= t.wake_window_min <= 180):
        problems.append(f"wake_window_min={t.wake_window_min} looks unreasonable (expected 0-180)")
    if not (-100 <= t.level_min < 0 < t.level_max <= 100):
        problems.append(f"level bounds [{t.level_min}, {t.level_max}] outside the device's "
                        "-100..100 range")
    for name in ("neutral_temp_f", "deep_bias_temp_f", "wake_ramp_temp_f"):
        val = getattr(t, name, None)
        if val is not None and not (55.0 <= val <= 110.0):
            problems.append(f"{name}={val} outside the Pod's 55-110F water-temp range")

    if problems:
        return _check("config_sane", "Config sanity", "fail", "; ".join(problems),
                      "review sleepctl/config.py Tunables — one or more values are outside "
                      "the physically sane range")
    return _check(
        "config_sane", "Config sanity", "ok",
        f"max_step_f={t.max_step_f} wake_window_min={t.wake_window_min} "
        f"neutral={t.neutral_temp_f}F deep_bias={t.deep_bias_temp_f}F "
        f"wake_ramp={t.wake_ramp_temp_f}F — all within sane ranges.", "",
    )


def _check_outcome_trend(repo) -> dict:
    nights = _safe_call(lambda: repo.recent_nights(14), []) or []
    scored = [n for n in nights if n.outcome_score is not None]
    if len(scored) < 4:
        return _check(
            "outcome_trend", "Outcome trend", "info",
            f"only {len(scored)} scored night(s) recently — too few to trend.", "",
        )

    half = len(scored) // 2
    first, second = scored[:half], scored[half:]
    avg1 = sum(n.outcome_score for n in first) / len(first)
    avg2 = sum(n.outcome_score for n in second) / len(second)
    delta = avg2 - avg1

    if delta > 0.03:
        trend = "improving"
    elif delta < -0.03:
        trend = "worsening"
    else:
        trend = "flat"

    detail = f"outcome_score trend: {trend} ({avg1:.2f} -> {avg2:.2f} over {len(scored)} nights)"

    we_first = [n.wake_events for n in first if n.wake_events is not None]
    we_second = [n.wake_events for n in second if n.wake_events is not None]
    if we_first and we_second:
        we_delta = (sum(we_second) / len(we_second)) - (sum(we_first) / len(we_first))
        detail += f"; wake_events avg {we_delta:+.2f}"

    return _check("outcome_trend", "Outcome trend", "info", detail, "")


# --------------------------------------------------------------------------------- summary


def _summarize(checks: list[dict], repo) -> tuple[str, str]:
    nights = _safe_call(lambda: len(repo.all_nights()), 0) or 0
    has_fail = any(c.get("status") == "fail" for c in checks)
    has_warn = any(c.get("status") == "warn" for c in checks)

    if has_fail:
        failing = [c["title"] for c in checks if c.get("status") == "fail"]
        return "DEGRADED", (
            "Data/learning health: DEGRADED — failing check(s): " + ", ".join(failing)
        )
    if nights < _NEEDS_DATA_NIGHTS:
        return "NEEDS_DATA", (
            f"Data/learning health: NEEDS_DATA — only {nights} night(s) logged; "
            "log more nights before the learners/ML can meaningfully engage."
        )
    if has_warn:
        warning = [c["title"] for c in checks if c.get("status") == "warn"]
        return "DEGRADED", (
            "Data/learning health: DEGRADED — warning(s): " + ", ".join(warning)
        )
    return "HEALTHY", f"Data/learning health: HEALTHY — {nights} nights logged, all checks passing."


# ----------------------------------------------------------------------------------- entry


def data_diagnostics(repo, cfg=None) -> dict:
    """Run the full data/learning/config health check against ``repo``.

    Pure and read-only: never mutates the database, never retrains anything, and never
    raises — every check is individually sandboxed so a missing table or empty DB degrades
    to an informative ``"info"`` result rather than an exception.

    Returns ``{"verdict": "HEALTHY"|"DEGRADED"|"NEEDS_DATA", "headline": str,
    "checks": [{"id", "title", "status", "detail", "remedy"}, ...]}``.
    """
    if cfg is None:
        try:
            from sleepctl.config import AppConfig
            cfg = AppConfig.default()
        except Exception:
            cfg = None

    checks = [
        _run_check("db", "Database schema", lambda: _check_db(repo)),
        _run_check("data_volume", "Data volume", lambda: _check_data_volume(repo, cfg)),
        _run_check("data_completeness", "Data completeness", lambda: _check_data_completeness(repo)),
        _run_check("learner_maturity", "Learner maturity", lambda: _check_learner_maturity(repo, cfg)),
        _run_check("calibration", "Personal calibration", lambda: _check_calibration(repo)),
        _run_check("setpoints_sane", "Setpoint sanity", lambda: _check_setpoints_sane(repo, cfg)),
        _run_check("config_sane", "Config sanity", lambda: _check_config_sane(cfg)),
        _run_check("outcome_trend", "Outcome trend", lambda: _check_outcome_trend(repo)),
    ]

    verdict, headline = _safe_call(lambda: _summarize(checks, repo),
                                   ("DEGRADED", "Data/learning health: could not summarize checks."))
    return {"verdict": verdict, "headline": headline, "checks": checks}
