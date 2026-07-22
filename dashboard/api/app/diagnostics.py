"""Self-diagnosis battery for the sleepctl dashboard.

Goal: when anything breaks, ONE query (``run_diagnostics()``) gives a precise, structured
"what's wrong + the fix" — instead of a human having to read raw log tails through a lossy
summarizer. This module runs a battery of small, independent CHECKS against:

  * the daemon's ``runtime_state`` snapshot (via ``bridge.read_runtime_state`` — the SAME
    dict ``health_monitor.evaluate_health`` consumes, so this reuses that pure evaluator as
    one input rather than re-deriving the same logic twice),
  * the ``.run`` heartbeat/log files the watchdog + daemon write,
  * local process/port state (is something listening on 3000?),
  * deploy config presence (Eight Sleep creds, calendar/shift),
  * the deployed git commit + web-build freshness.

Every check is defensive: a missing file, a malformed runtime_state, an import that fails —
none of it should ever raise out of ``run_diagnostics``. Each check degrades to "info" with
an explanatory detail instead. This is what makes it safe to wire into a hot, publicly
reachable endpoint (``/diag``).

Kept easy to unit-test: pass a real ``repo`` (any object exposing ``.conn``, most likely a
``sleepctl.storage.repository.Repository`` over a temp DB) whose ``runtime_state`` you've
seeded with ``bridge.write_runtime_state``, plus a ``run_dir`` pointing at a temp directory
with fake heartbeat/log files.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timedelta, timezone

# ------------------------------------------------------------------ thresholds / constants
DAEMON_HEARTBEAT_STALE_S = 90     # daemon writes .run/daemon.heartbeat roughly every ~2s
WATCHDOG_HEARTBEAT_STALE_S = 60   # watchdog writes .run/watchdog.heartbeat roughly every ~15s
LOG_SIZE_WARN_BYTES = 50 * 1024 * 1024  # 50MB — a runaway/looping log
CLOUD_ERROR_TAIL_LINES = 500
CLOUD_ERROR_WARN_COUNT = 10   # >= this many hits in the tail -> treat as a real outage (fail)
# daemon-crash.log is append-only/historical, so its LAST line can be a crash from hours ago
# that was already recovered from. Only treat a crash as a live FAIL when the crash log was
# modified within this window (or the daemon heartbeat is currently stale) -- otherwise a long-
# fixed crash would pin the whole diagnosis to DEGRADED forever.
RECENT_CRASH_WINDOW_S = 15 * 60   # 15 min
CLOUD_ERROR_PATTERNS = (
    "RequestError", " 504", "Timeout", "timeout", "ConnectionError", "ClientError",
)

# Checks whose FAILURE means the system is effectively DOWN (not merely degraded). Kept
# narrow on purpose — everything else (no water, thermal stalled, missing creds, ...) is a
# real problem worth flagging but the daemon+API are still up and reachable, so DEGRADED.
DOWN_TRIGGER_IDS = {"daemon_heartbeat", "api"}

# Rendering/aggregation order (stable, readable; doesn't affect verdict logic).
_CHECK_ORDER = [
    "daemon_heartbeat", "watchdog_heartbeat", "api", "web", "runtime_state_fresh",
    "device_water", "device_online", "priming", "thermal_response",
    "thermal_capacity", "external_conflict", "frozen_telemetry", "recent_errors",
    "cloud_errors", "live_mode", "phone_sensor", "cardiac_sensor", "eight_sleep_creds",
    "version", "log_sizes", "calendar", "shift",
]

# History window handed to the thermal-capacity/conflict/frozen-telemetry detectors — plenty
# to confirm a stuck prime (>6 min) or a frozen window (>5 min) without pulling the whole 7-day
# state_history table on every /diag hit.
_THERMAL_HISTORY_HOURS = 1
_THERMAL_HISTORY_LIMIT = 200


def _check(id: str, title: str, status: str, detail: str, remedy: str | None = None) -> dict:
    return {"id": id, "title": title, "status": status, "detail": detail, "remedy": remedy}


# ------------------------------------------------------------------ small file/io helpers
def _default_run_dir() -> str:
    """Same resolution ``app.main._run_dir``/``app.bridge.run_dir`` use — duplicated (not
    imported) so this module has no import-time dependency on the rest of the app and degrades
    gracefully if that import ever fails."""
    try:
        from app.bridge import run_dir as _bridge_run_dir
        return _bridge_run_dir()
    except Exception:
        db = os.environ.get("SLEEPCTL_DB", "")
        root = os.path.dirname(db) if db else os.getcwd()
        return os.path.join(root, ".run")


def _repo_root() -> str:
    """The checkout root (parent of ``dashboard/``), derived from this file's own location so
    it works regardless of cwd or how the API was launched (uvicorn --app-dir, docker, etc)."""
    here = os.path.abspath(os.path.dirname(__file__))  # .../dashboard/api/app
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _file_age_s(path: str, now: float) -> float | None:
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def _tail_lines(path: str, n: int) -> list[str] | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-n:]
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}TB"


def _age_seconds_iso(updated: str | None) -> float | None:
    if not updated:
        return None
    try:
        ts = datetime.fromisoformat(updated)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


# ------------------------------------------------------------------ git / version
def _read_packed_ref(git_dir: str, ref: str) -> str | None:
    try:
        with open(os.path.join(git_dir, "packed-refs"), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line[0] in "#^":
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except Exception:
        pass
    return None


def _git_head_info(repo_root: str) -> dict:
    """Deployed commit SHA + branch + (best-effort) commit time. Reads ``.git/HEAD`` + refs
    directly first (works even where a ``git`` binary isn't installed, e.g. some minimal
    container images); falls back to shelling out to ``git`` only if that fails."""
    info: dict = {"sha": None, "branch": None, "full_sha": None, "commit_time": None}
    git_dir = os.path.join(repo_root, ".git")
    try:
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8") as fh:
            content = fh.read().strip()
        if content.startswith("ref:"):
            ref = content.split(" ", 1)[1].strip()
            info["branch"] = ref.rsplit("/", 1)[-1]
            ref_path = os.path.join(git_dir, ref)
            if os.path.exists(ref_path):
                with open(ref_path, "r", encoding="utf-8") as fh:
                    info["full_sha"] = fh.read().strip()
                info["commit_time"] = os.path.getmtime(ref_path)
            else:
                info["full_sha"] = _read_packed_ref(git_dir, ref)
        else:
            # detached HEAD: the file content IS the sha
            info["full_sha"] = content
            info["branch"] = "(detached)"
            info["commit_time"] = os.path.getmtime(os.path.join(git_dir, "HEAD"))
    except Exception:
        pass
    if info["full_sha"]:
        info["sha"] = info["full_sha"][:7]
    if not info["sha"] or info["commit_time"] is None:
        # best-effort git-binary fallback (short timeout — never let this hang the request)
        try:
            import subprocess
            if not info["sha"]:
                out = subprocess.run(["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True, timeout=3)
                if out.returncode == 0 and out.stdout.strip():
                    info["sha"] = out.stdout.strip()
            if not info["branch"]:
                br = subprocess.run(["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True, timeout=3)
                if br.returncode == 0 and br.stdout.strip():
                    info["branch"] = br.stdout.strip()
            if info["commit_time"] is None:
                ct = subprocess.run(["git", "-C", repo_root, "log", "-1", "--format=%ct"],
                                    capture_output=True, text=True, timeout=3)
                if ct.returncode == 0 and ct.stdout.strip():
                    info["commit_time"] = float(ct.stdout.strip())
        except Exception:
            pass
    return info


def _last_web_commit_time(repo_root: str) -> float | None:
    """Unix time of the most recent commit that touched ``dashboard/web`` — so the version check
    can tell whether the ``.next`` build is actually behind the UI SOURCE, not merely behind some
    unrelated backend/infra commit. Best-effort via the git binary; returns None if git isn't
    available or the call fails (caller then falls back to the HEAD commit time)."""
    try:
        import subprocess

        out = subprocess.run(
            ["git", "-C", repo_root, "log", "-1", "--format=%ct", "--", "dashboard/web"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except Exception:
        pass
    return None


def _check_version(repo_root: str) -> dict:
    info = _git_head_info(repo_root)
    sha = info.get("sha") or "unknown"
    branch = info.get("branch") or "unknown"
    detail = f"commit {sha} on {branch}"
    status, remedy = "info", None

    web_next = os.path.join(repo_root, "dashboard", "web", ".next")
    build_id = os.path.join(web_next, "BUILD_ID")
    build_mtime = None
    if os.path.exists(build_id):
        build_mtime = os.path.getmtime(build_id)
    elif os.path.isdir(web_next):
        build_mtime = os.path.getmtime(web_next)

    # Compare the .next build against the last commit that actually TOUCHED the web UI source
    # (dashboard/web), not merely the latest HEAD commit -- otherwise an unrelated backend/infra
    # deploy falsely flags the UI as stale. Falls back to the HEAD commit time if the git binary
    # isn't available.
    web_commit_time = _last_web_commit_time(repo_root)
    ref_time = web_commit_time if web_commit_time is not None else info.get("commit_time")
    if build_mtime is None:
        detail += "; no production web build found (.next missing)"
        status = "warn"
        remedy = "run `npm run build` in dashboard/web — the UI has never been built for production"
    elif ref_time is not None and build_mtime < ref_time:
        age_h = (ref_time - build_mtime) / 3600.0
        detail += f"; web build is {age_h:.1f}h older than the web UI source"
        status = "warn"
        remedy = ("web build is older than the web UI source — rebuild the UI "
                  "(npm run build in dashboard/web; the watchdog only builds it if .next is "
                  "entirely missing)")
    else:
        detail += "; web build is up to date"

    return _check("version", "Deployed version", status, detail, remedy)


# ------------------------------------------------------------------ process / port liveness
def _check_daemon_heartbeat(run_dir: str, now: float) -> dict:
    age = _file_age_s(os.path.join(run_dir, "daemon.heartbeat"), now)
    remedy = "daemon down — watchdog should restart it; if it keeps flapping, run doctor.ps1"
    if age is None:
        return _check("daemon_heartbeat", "Control daemon heartbeat", "fail",
                      "daemon.heartbeat not found — the daemon has never checked in", remedy)
    if age > DAEMON_HEARTBEAT_STALE_S:
        return _check("daemon_heartbeat", "Control daemon heartbeat", "fail",
                      f"last heartbeat {age:.0f}s ago (> {DAEMON_HEARTBEAT_STALE_S}s)", remedy)
    return _check("daemon_heartbeat", "Control daemon heartbeat", "ok",
                  f"last heartbeat {age:.0f}s ago", None)


def _check_watchdog_heartbeat(run_dir: str, now: float) -> dict:
    age = _file_age_s(os.path.join(run_dir, "watchdog.heartbeat"), now)
    remedy = "watchdog not looping — check watchdog.log; the Scheduled Task may need a restart"
    if age is None:
        return _check("watchdog_heartbeat", "Watchdog heartbeat", "fail",
                      "watchdog.heartbeat not found — the watchdog may not be running", remedy)
    if age > WATCHDOG_HEARTBEAT_STALE_S:
        return _check("watchdog_heartbeat", "Watchdog heartbeat", "fail",
                      f"last heartbeat {age:.0f}s ago (> {WATCHDOG_HEARTBEAT_STALE_S}s)", remedy)
    return _check("watchdog_heartbeat", "Watchdog heartbeat", "ok",
                  f"last heartbeat {age:.0f}s ago", None)


def _check_api() -> dict:
    # If this function is running at all, a request made it through the API process — so this
    # is definitionally "ok". It exists as an explicit check for symmetry/readability and so the
    # verdict aggregation has a named, always-present anchor for "the API itself is up".
    return _check("api", "API process", "ok", "this request was served, so the API is up", None)


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_web() -> dict:
    if _port_open("127.0.0.1", 3000):
        return _check("web", "Web UI (port 3000)", "ok",
                      "something is listening on 127.0.0.1:3000", None)
    return _check("web", "Web UI (port 3000)", "warn",
                  "nothing is listening on 127.0.0.1:3000",
                  "the Next.js server isn't up — the watchdog should start it; "
                  "check web.log/web.err/web-build.log")


def _check_runtime_state_fresh(rt: dict, stale_seconds: int) -> dict:
    updated = rt.get("updated")
    remedy = "daemon not publishing state — it may be stuck or dead; check daemon.log/daemon.err"
    if updated is None:
        return _check("runtime_state_fresh", "Runtime state freshness", "fail",
                      "no runtime_state has ever been published", remedy)
    age = _age_seconds_iso(updated)
    age_txt = f"{age:.0f}s ago" if age is not None else "unknown age"
    if bool(rt.get("stale")):
        return _check("runtime_state_fresh", "Runtime state freshness", "fail",
                      f"last update {age_txt} (stale threshold {stale_seconds}s)", remedy)
    return _check("runtime_state_fresh", "Runtime state freshness", "ok",
                  f"last update {age_txt}", None)


# ------------------------------------------------------------------ device / control state
def _check_device_water(extra: dict) -> dict:
    device = extra.get("device") or {}
    has_water = device.get("has_water") if isinstance(device, dict) else None
    if has_water is False:
        return _check("device_water", "Water reservoir", "fail",
                      "has_water=false — the bed can't heat or cool",
                      "fill the Hub reservoir + PRIME")
    if has_water is None:
        return _check("device_water", "Water reservoir", "info",
                      "unknown (no device telemetry yet)", None)
    return _check("device_water", "Water reservoir", "ok", "reservoir has water", None)


def _check_device_online(extra: dict) -> dict:
    device = extra.get("device") or {}
    online = device.get("online") if isinstance(device, dict) else None
    if online is False:
        return _check("device_online", "Device online", "fail",
                      "the bed/hub is reporting offline",
                      "check the Hub's network connection/power; verify Eight Sleep cloud status")
    if online is None:
        return _check("device_online", "Device online", "info",
                      "unknown (no device telemetry yet)", None)
    return _check("device_online", "Device online", "ok", "device reports online", None)


def _check_priming(extra: dict) -> dict:
    device = extra.get("device") or {}
    if not isinstance(device, dict):
        device = {}
    if device.get("priming"):
        return _check("priming", "Priming state", "warn", "the Pod is currently priming",
                     "wait for priming to finish; normal control resumes automatically")
    if device.get("needs_priming"):
        return _check("priming", "Priming state", "warn", "the Pod reports it needs priming",
                     "run PRIME from the dashboard controls (or POST /control/prime)")
    return _check("priming", "Priming state", "ok", "not priming / doesn't need priming", None)


def _check_thermal_response(extra: dict) -> dict:
    thermal = extra.get("thermal_health") or {}
    if not isinstance(thermal, dict):
        thermal = {}
    state = thermal.get("state")
    reason = thermal.get("reason")
    if state == "stalled":
        why = reason or "bed temperature is not responding to commands"
        return _check("thermal_response", "Thermal response", "fail",
                      f"thermal control appears stalled: {why}",
                      "power-cycle the Hub, check the hose for kinks, or run the on-bed self-test")
    if state in ("ok", "ramping"):
        detail = f"state={state}" + (f" ({reason})" if reason else "")
        return _check("thermal_response", "Thermal response", "ok", detail, None)
    return _check("thermal_response", "Thermal response", "info",
                  f"state={state or 'unknown'}", None)


# ------------------------------------------------------------------ water-loop / capacity / conflict / frozen
# Three checks built on ``sleepctl.diagnostics_thermal`` (pure detection engine) fed by the
# ``state_history`` table (see ``Repository.record_state_snapshot``/``state_history``) — this
# is the trend data the daemon already records every ~60s, so no new sampling is needed. These
# close the loop on failure modes that were previously only found by manually reading logs: an
# air-bound water loop, a prime that starts but never finishes, a low reservoir, the Eight
# Sleep app's own schedule fighting this controller, and telemetry frozen by a crash-looping
# daemon.
def _check_thermal_capacity(repo, extra: dict, history: list | None = None) -> dict:
    device = extra.get("device") or {}
    if not isinstance(device, dict):
        device = {}
    try:
        from sleepctl.diagnostics_thermal import analyze_thermal_capacity
        if history is None:
            history = repo.state_history(hours=_THERMAL_HISTORY_HOURS, limit=_THERMAL_HISTORY_LIMIT)
        now_iso = datetime.now(timezone.utc).isoformat()
        result = analyze_thermal_capacity(device, history, now_iso)
    except Exception as exc:
        return _check("thermal_capacity", "Water-loop / thermal capacity", "info",
                     f"check could not run: {exc!r}", None)

    status = result.get("status")
    reason = result.get("reason") or "no water-loop/thermal-capacity issue detected."
    remedy = result.get("remedy") or None
    detail = f"{status}: {reason}"
    if status in ("stuck_prime", "reduced_capacity"):
        return _check("thermal_capacity", "Water-loop / thermal capacity", "fail", detail, remedy)
    if status == "low_water":
        return _check("thermal_capacity", "Water-loop / thermal capacity", "warn", detail, remedy)
    if status == "insufficient_data":
        return _check("thermal_capacity", "Water-loop / thermal capacity", "info", reason, None)
    return _check("thermal_capacity", "Water-loop / thermal capacity", "ok", reason, None)


def _check_external_conflict(repo, extra: dict, history: list | None = None) -> dict:
    device = extra.get("device") or {}
    if not isinstance(device, dict):
        device = {}
    try:
        from sleepctl.diagnostics_thermal import detect_external_conflict
        if history is None:
            history = repo.state_history(hours=_THERMAL_HISTORY_HOURS, limit=_THERMAL_HISTORY_LIMIT)
        result = detect_external_conflict(device, history)
    except Exception as exc:
        return _check("external_conflict", "External controller conflict", "info",
                     f"check could not run: {exc!r}", None)

    status = result.get("status")
    reason = result.get("reason") or "no external-controller conflict detected."
    remedy = result.get("remedy") or None
    detail = f"{status}: {reason}"
    if status == "external_setpoint_conflict":
        return _check("external_conflict", "External controller conflict", "warn", detail, remedy)
    if status == "insufficient_data":
        return _check("external_conflict", "External controller conflict", "info", reason, None)
    return _check("external_conflict", "External controller conflict", "ok", reason, None)


def _check_frozen_telemetry(repo, history: list | None = None) -> dict:
    try:
        from sleepctl.diagnostics_thermal import detect_frozen_telemetry
        if history is None:
            history = repo.state_history(hours=_THERMAL_HISTORY_HOURS, limit=_THERMAL_HISTORY_LIMIT)
        result = detect_frozen_telemetry(history)
    except Exception as exc:
        return _check("frozen_telemetry", "Frozen telemetry", "info",
                     f"check could not run: {exc!r}", None)

    status = result.get("status")
    reason = result.get("reason") or "telemetry is updating normally."
    remedy = result.get("remedy") or None
    detail = f"{status}: {reason}"
    if status == "frozen_telemetry":
        return _check("frozen_telemetry", "Frozen telemetry", "fail", detail, remedy)
    if status == "insufficient_data":
        return _check("frozen_telemetry", "Frozen telemetry", "info", reason, None)
    return _check("frozen_telemetry", "Frozen telemetry", "ok", reason, None)


def _check_phone_sensor(repo, extra: dict) -> dict:
    """Phone (Sensor Logger) ingest liveness -- METADATA ONLY: streaming yes/no, seconds since the
    last sample, count in the last hour, in-bed, fusing. NEVER the physiology VALUES (HR/HRV/
    movement), so it's safe in the scrubbed public health snapshot. Lets an off-box operator
    confirm the iPhone stream is actually reaching the server without exposing any biometrics."""
    from app.bridge import read_sensor_sample
    sample = read_sensor_sample(repo.conn)
    cnt = None
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        row = repo.conn.execute(
            "SELECT COUNT(*) c FROM sensor_samples WHERE ts >= ?", (since,)).fetchone()
        cnt = row["c"] if row is not None else None
    except Exception:
        cnt = None
    if sample is None and not cnt:
        return _check("phone_sensor", "Phone sensor (iPhone)", "info",
                      "no phone sensor data received yet (Sensor Logger not streaming)",
                      "point Sensor Logger at /bcg/ingest — see deploy/IPHONE_SENSOR.md")
    age = sample.get("age_seconds") if sample else None
    in_bed = (extra.get("bed_presence") is True)
    streaming = bool(age is not None and age < 120)
    fusing = bool(age is not None and age < 90 and in_bed)
    age_txt = f"{age:.0f}s ago" if age is not None else "unknown"
    cnt_txt = f"{cnt} samples in last hr" if cnt is not None else "count unavailable"
    if streaming:
        detail = (f"phone STREAMING (last sample {age_txt}; {cnt_txt}); "
                  f"in_bed={in_bed}, fusing={fusing}")
        status = "ok"
    else:
        detail = f"phone not currently streaming (last sample {age_txt}; {cnt_txt})"
        status = "info"
    return _check("phone_sensor", "Phone sensor (iPhone)", status, detail, None)


def _check_live_mode(extra: dict) -> dict:
    live = extra.get("live")
    dry_run = extra.get("dry_run")
    if dry_run:
        return _check("live_mode", "Live / dry-run mode", "warn",
                      f"live={live} dry_run={dry_run}",
                      "read-only: SLEEPCTL_DRY_RUN=1, not actuating the bed — "
                      "unset it in deploy/.env once you trust the decisions")
    return _check("live_mode", "Live / dry-run mode", "info",
                  f"live={live} dry_run={dry_run}", None)


# ------------------------------------------------------------------ logs
def _check_cloud_errors(run_dir: str) -> dict:
    lines = _tail_lines(os.path.join(run_dir, "daemon.log"), CLOUD_ERROR_TAIL_LINES)
    if lines is None:
        return _check("cloud_errors", "Eight Sleep cloud errors", "info",
                      "daemon.log not found", None)
    hits = [ln for ln in lines if any(p in ln for p in CLOUD_ERROR_PATTERNS)]
    if not hits:
        return _check("cloud_errors", "Eight Sleep cloud errors", "ok",
                      f"no cloud/timeout errors in the last {len(lines)} log lines", None)
    latest = hits[-1].strip()[:300]
    status = "fail" if len(hits) >= CLOUD_ERROR_WARN_COUNT else "warn"
    return _check("cloud_errors", "Eight Sleep cloud errors", status,
                 f"{len(hits)} cloud/timeout error line(s) in the last {len(lines)} log lines; "
                 f"latest: {latest}",
                 "Eight Sleep's cloud API looks flaky/down — the daemon retries automatically; "
                 "if this persists, check status.eightsleep.com")


def _check_recent_errors(run_dir: str, now: float, daemon_heartbeat_age: float | None) -> dict:
    err_lines = [l for l in (_tail_lines(os.path.join(run_dir, "daemon.err"), 200) or [])
                 if l.strip()]
    crash_path = os.path.join(run_dir, "daemon-crash.log")
    crash_lines = [l for l in (_tail_lines(crash_path, 200) or []) if l.strip()]
    if not err_lines and not crash_lines:
        return _check("recent_errors", "Recent daemon errors", "ok",
                      "daemon.err and daemon-crash.log are empty", None)

    # A crash is only a live problem if it is RECENT (crash log touched within the window) or
    # the daemon is currently unhealthy (heartbeat stale/missing). daemon-crash.log is append-
    # only history, so an old-but-recovered crash must not FAIL a daemon that's healthy now.
    daemon_healthy = (daemon_heartbeat_age is not None
                      and daemon_heartbeat_age <= DAEMON_HEARTBEAT_STALE_S)
    parts = []
    status = "warn" if err_lines else "ok"
    remedy = None
    if crash_lines:
        last = crash_lines[-1].strip()[:300]
        crash_age = _file_age_s(crash_path, now)  # None if unreadable -> treat as recent
        crash_recent = crash_age is None or crash_age <= RECENT_CRASH_WINDOW_S
        if crash_recent or not daemon_healthy:
            parts.append(f"daemon-crash.log last: {last}")
            status = "fail"
            remedy = "read the daemon.err/daemon-crash.log tails below for the full traceback"
        else:
            parts.append(f"last crash {crash_age / 60:.0f}m ago "
                         f"(stale; daemon healthy since): {last}")
    if err_lines:
        parts.append(f"daemon.err last: {err_lines[-1].strip()[:300]}")
        if remedy is None:
            remedy = "read the daemon.err/daemon-crash.log tails below for the full traceback"
    return _check("recent_errors", "Recent daemon errors", status, " | ".join(parts), remedy)


def _check_log_sizes(run_dir: str) -> dict:
    names = ["daemon.log", "daemon.err", "daemon-crash.log", "watchdog.log", "api.log",
             "web-build.log"]
    sizes: dict[str, int] = {}
    over = []
    for n in names:
        try:
            sz = os.path.getsize(os.path.join(run_dir, n))
        except OSError:
            continue
        sizes[n] = sz
        if sz > LOG_SIZE_WARN_BYTES:
            over.append(n)
    if not sizes:
        return _check("log_sizes", "Log file sizes", "info", "no log files found in run dir", None)
    detail = ", ".join(f"{n}={_human_bytes(sz)}" for n, sz in sizes.items())
    if over:
        return _check("log_sizes", "Log file sizes", "warn", detail,
                     f"{', '.join(over)} > 50MB — rotate/truncate to avoid disk pressure")
    return _check("log_sizes", "Log file sizes", "ok", detail, None)


# ------------------------------------------------------------------ deploy config presence
def _check_eight_sleep_creds() -> dict:
    if not os.environ.get("EIGHTSLEEP_EMAIL") or not os.environ.get("EIGHTSLEEP_PASSWORD"):
        return _check("eight_sleep_creds", "Eight Sleep credentials", "warn",
                      "EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD not both set",
                      "daemon will fall back to SIMULATOR — set both in deploy/.env for live control")
    return _check("eight_sleep_creds", "Eight Sleep credentials", "ok",
                  "EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD are set", None)


def _check_calendar(repo) -> dict:
    from app import services
    cfg = services._get_calendar_config(repo)
    if cfg.get("enabled") and cfg.get("ics_url"):
        return _check("calendar", "Work calendar (ICS)", "info",
                      "calendar feed is configured and enabled", None)
    if cfg.get("ics_url"):
        return _check("calendar", "Work calendar (ICS)", "info",
                      "calendar URL is set but disabled", None)
    return _check("calendar", "Work calendar (ICS)", "info", "no calendar configured", None)


def _check_shift(repo) -> dict:
    from app import services
    cfg = services._get_shift_config(repo)
    if cfg.get("enabled") and cfg.get("next_shift"):
        return _check("shift", "Shift plan", "info",
                      f"enabled, next_shift={cfg.get('next_shift')} kind={cfg.get('kind')}", None)
    return _check("shift", "Shift plan", "info", "no upcoming shift configured", None)


# ------------------------------------------------------------------ aggregation
def _aggregate(checks: list[dict]) -> tuple[str, str, str | None]:
    """verdict, headline, primary_remedy from the check battery.

    DOWN if a ``DOWN_TRIGGER_IDS`` check failed (daemon dead / api down — the system can't be
    doing anything useful). DEGRADED if anything else failed or warned. Otherwise HEALTHY.
    "Most important problem" = the first FAIL among the down-triggers, else the first FAIL,
    else the first WARN, in ``_CHECK_ORDER``.
    """
    by_id = {c["id"]: c for c in checks}
    ordered = [by_id[i] for i in _CHECK_ORDER if i in by_id]
    ordered += [c for c in checks if c["id"] not in _CHECK_ORDER]  # any unknown extras, still shown

    down_fails = [c for c in ordered if c["id"] in DOWN_TRIGGER_IDS and c["status"] == "fail"]
    other_fails = [c for c in ordered if c["id"] not in DOWN_TRIGGER_IDS and c["status"] == "fail"]
    warns = [c for c in ordered if c["status"] == "warn"]

    if down_fails:
        top = down_fails[0]
        return "DOWN", f"{top['title']}: {top['detail']}", top.get("remedy") or "see checks below"
    if other_fails or warns:
        top = (other_fails or warns)[0]
        verdict = "DEGRADED"
        return verdict, f"{top['title']}: {top['detail']}", top.get("remedy") or "see checks below"
    return "HEALTHY", "all systems nominal", None


def _check_cardiac_sensor(repo) -> dict:
    """Dedicated BLE cardiac sensor (Polar Verity Sense -> /hr/ingest) freshness. Metadata only —
    reports streaming state, not raw HR/HRV."""
    from app import bridge
    s = bridge.read_cardiac_sample(repo.conn)
    if not s:
        return _check("cardiac_sensor", "Cardiac sensor (Verity)", "info",
                      "no cardiac-sensor data yet (Polar Verity Sense not streaming)",
                      "run scripts/verity_forwarder.py -- see deploy/VERITY_SENSOR.md")
    age = s.get("age_seconds")
    if age is not None and age < 120:
        return _check("cardiac_sensor", "Cardiac sensor (Verity)", "ok",
                      f"streaming (last HR sample {int(age)}s ago)", None)
    ago = f"{int(age)}s ago" if age is not None else "at an unknown time"
    return _check("cardiac_sensor", "Cardiac sensor (Verity)", "info",
                  f"not currently streaming (last sample {ago})", None)


# ------------------------------------------------------------------ entry point
def run_diagnostics(repo, run_dir: str | None = None) -> dict:
    """Run the full diagnostic battery. Never raises.

    ``repo`` — anything exposing ``.conn`` (a ``sleepctl.storage.repository.Repository``) so
    the daemon's live ``runtime_state`` + calendar/shift config can be read.
    ``run_dir`` — override the ``.run`` directory (defaults to alongside the SQLite DB, same
    resolution as ``app.main._run_dir``); tests pass a temp dir with fake heartbeat/log files.
    """
    now = time.time()
    run_dir = run_dir or _default_run_dir()
    repo_root = _repo_root()

    try:
        from app.config import settings
        stale_seconds = settings.runtime_stale_seconds
    except Exception:
        stale_seconds = 180

    rt: dict = {"updated": None, "stale": True}
    extra: dict = {}
    try:
        from app import bridge
        rt = bridge.read_runtime_state(repo.conn, stale_seconds)
        extra = rt.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
    except Exception:
        pass  # degrade to the "never reported" defaults above

    checks: list[dict] = []

    def add(id_: str, title: str, fn) -> None:
        try:
            checks.append(fn())
        except Exception as exc:  # a single buggy/failing check must never break the battery
            checks.append(_check(id_, title, "info", f"check crashed: {exc!r}", None))

    add("version", "Deployed version", lambda: _check_version(repo_root))
    add("daemon_heartbeat", "Control daemon heartbeat", lambda: _check_daemon_heartbeat(run_dir, now))
    add("watchdog_heartbeat", "Watchdog heartbeat", lambda: _check_watchdog_heartbeat(run_dir, now))
    add("api", "API process", _check_api)
    add("web", "Web UI (port 3000)", _check_web)
    add("runtime_state_fresh", "Runtime state freshness",
        lambda: _check_runtime_state_fresh(rt, stale_seconds))
    add("device_water", "Water reservoir", lambda: _check_device_water(extra))
    add("device_online", "Device online", lambda: _check_device_online(extra))
    add("priming", "Priming state", lambda: _check_priming(extra))
    add("thermal_response", "Thermal response", lambda: _check_thermal_response(extra))
    # thermal_capacity/external_conflict/frozen_telemetry all read the same state_history window;
    # fetch it once here instead of each check re-querying it (3x the same SELECT per /diag call).
    try:
        _thermal_history = repo.state_history(hours=_THERMAL_HISTORY_HOURS,
                                              limit=_THERMAL_HISTORY_LIMIT)
    except Exception:
        _thermal_history = []
    add("thermal_capacity", "Water-loop / thermal capacity",
        lambda: _check_thermal_capacity(repo, extra, history=_thermal_history))
    add("external_conflict", "External controller conflict",
        lambda: _check_external_conflict(repo, extra, history=_thermal_history))
    add("frozen_telemetry", "Frozen telemetry",
        lambda: _check_frozen_telemetry(repo, history=_thermal_history))
    add("live_mode", "Live / dry-run mode", lambda: _check_live_mode(extra))
    add("phone_sensor", "Phone sensor (iPhone)", lambda: _check_phone_sensor(repo, extra))
    add("cloud_errors", "Eight Sleep cloud errors", lambda: _check_cloud_errors(run_dir))
    daemon_hb_age = _file_age_s(os.path.join(run_dir, "daemon.heartbeat"), now)
    add("recent_errors", "Recent daemon errors",
        lambda: _check_recent_errors(run_dir, now, daemon_hb_age))
    add("eight_sleep_creds", "Eight Sleep credentials", _check_eight_sleep_creds)
    add("cardiac_sensor", "Cardiac sensor (Verity)", lambda: _check_cardiac_sensor(repo))
    add("calendar", "Work calendar (ICS)", lambda: _check_calendar(repo))
    add("shift", "Shift plan", lambda: _check_shift(repo))
    add("log_sizes", "Log file sizes", lambda: _check_log_sizes(run_dir))

    verdict, headline, primary_remedy = _aggregate(checks)
    git_info = _git_head_info(repo_root)
    playbook_matches = _match_known_issues(repo, checks, run_dir)

    return {
        "verdict": verdict,
        "headline": headline,
        "primary_remedy": primary_remedy,
        "checks": checks,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": {"sha": git_info.get("sha"), "branch": git_info.get("branch")},
        "playbook_matches": playbook_matches,
    }


# ------------------------------------------------------------------ known-issue playbook (#9)
def _match_known_issues(repo, checks: list[dict], run_dir: str) -> list[dict]:
    """Run the engine-side known-issue playbook (``sleepctl.diagnostics_playbook``) against
    this battery's checks + recent structured events. Defensive: never raises, degrades to no
    matches rather than breaking ``run_diagnostics``."""
    try:
        from sleepctl.diagnostics_playbook import match_playbook
        events: list[dict] = []
        try:
            events = repo.recent_events(limit=100)
        except Exception:
            events = []
        return match_playbook({"checks": checks}, events=events, run_dir=run_dir)
    except Exception:
        return []


# ------------------------------------------------------------------ plaintext rendering
_STATUS_ORDER = {"fail": 0, "warn": 1, "ok": 2, "info": 3}
_STATUS_LABEL = {"fail": "FAIL", "warn": "WARN", "ok": "OK", "info": "INFO"}


def render_diagnosis_text(report: dict) -> str:
    """Render a ``run_diagnostics()`` dict as the plaintext block ``/diag`` prepends: fails
    first, then warns, then ok/info — each with its fix inline so nothing needs a second
    lookup."""
    lines = [f"=== DIAGNOSIS: {report.get('verdict', 'UNKNOWN')} ==="]
    headline = report.get("headline") or "unknown"
    lines.append(f"! {headline}")
    remedy = report.get("primary_remedy")
    if remedy:
        lines.append(f"-> {remedy}")
    checks = sorted(report.get("checks") or [],
                    key=lambda c: _STATUS_ORDER.get(c.get("status"), 9))
    for c in checks:
        label = _STATUS_LABEL.get(c.get("status"), str(c.get("status")).upper())
        line = f"[{label:<4}] {c.get('id')}: {c.get('detail')}"
        if c.get("remedy"):
            line += f"  (fix: {c['remedy']})"
        lines.append(line)

    matches = report.get("playbook_matches") or []
    if matches:
        lines.append("")
        lines.append("=== LIKELY CAUSES & FIXES ===")
        for m in matches:
            lines.append(f"- {m.get('symptom')}")
            lines.append(f"    cause: {m.get('likely_cause')}")
            lines.append(f"    fix:   {m.get('fix')}")
    return "\n".join(lines)
