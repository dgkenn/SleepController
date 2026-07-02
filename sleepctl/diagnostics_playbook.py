"""Known-issue playbook: symptom -> likely cause -> concrete fix.

This is the third leg of the diagnostics stack (alongside ``sleepctl.diagnostics`` for
data/learning health and ``dashboard/api/app/diagnostics.py`` for live-runtime health): a
small, structured knowledge base of issues this project has actually hit, seeded once and
matched automatically against a live diagnostics run instead of relying on someone
remembering "oh, that means the reservoir is empty".

Deliberately engine-side (no import of ``dashboard``) so it stays usable from the CLI, tests,
and the dashboard API alike -- the dashboard API is the thin caller, not the owner, of this
knowledge.

Each :class:`PlaybookEntry` is intentionally plain data plus one small predicate:

  * ``id`` / ``symptom`` / ``likely_cause`` / ``fix`` -- human-readable playbook fields.
  * ``detect`` -- a predicate over a *signal context* (see :func:`match_playbook`): the
    current diagnostics ``checks`` (keyed by id), recent structured ``events``, whether
    ``.run/watchdog.alert`` exists, and the relevant environment variables. Every predicate is
    called defensively -- one buggy/failing detector can never break the whole battery.
  * ``auto_fixable`` -- reserved for future one-click remediation; every seed entry here is a
    human action (temp/water/creds/network problems aren't safely auto-fixable), so it is
    always ``False`` today.

Entry point: :func:`match_playbook`. Returns only the entries whose ``detect`` currently
matches, each as a plain (JSON-serializable) dict.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class PlaybookEntry:
    id: str
    symptom: str
    detect: Callable[[dict], bool]
    likely_cause: str
    fix: str
    auto_fixable: bool = False

    def as_dict(self) -> dict:
        """JSON-serializable view (drops the ``detect`` callable)."""
        return {
            "id": self.id,
            "symptom": self.symptom,
            "likely_cause": self.likely_cause,
            "fix": self.fix,
            "auto_fixable": self.auto_fixable,
        }


# --------------------------------------------------------------------------------- helpers
def _status(ctx: dict, check_id: str) -> Optional[str]:
    check = (ctx.get("checks") or {}).get(check_id)
    return check.get("status") if isinstance(check, dict) else None


def _blob(ctx: dict) -> str:
    """Lower-cased text blob of everything a keyword search should scan: every check's
    detail/remedy text (which already carries log-line snippets, e.g. ``recent_errors`` and
    ``cloud_errors``), plus recent event messages/codes."""
    cached = ctx.get("_blob")
    if cached is not None:
        return cached
    parts: list[str] = []
    for check in (ctx.get("checks") or {}).values():
        if not isinstance(check, dict):
            continue
        parts.append(str(check.get("detail") or ""))
        parts.append(str(check.get("remedy") or ""))
    for event in ctx.get("events") or []:
        if not isinstance(event, dict):
            continue
        parts.append(str(event.get("message") or ""))
        parts.append(str(event.get("code") or ""))
    blob = " ".join(parts).lower()
    ctx["_blob"] = blob
    return blob


def _keyword_match(ctx: dict, keywords: tuple[str, ...]) -> bool:
    blob = _blob(ctx)
    return any(k.lower() in blob for k in keywords)


def _env_flag(ctx: dict, key: str) -> bool:
    env = ctx.get("env") or {}
    return str(env.get(key, "")).strip().lower() in ("1", "true", "yes", "on")


def _dry_run_left_on(ctx: dict) -> bool:
    if _env_flag(ctx, "SLEEPCTL_LIVE") and _env_flag(ctx, "SLEEPCTL_DRY_RUN"):
        return True
    # Fallback for callers that only pass a diagnostics ``checks`` dict (no env): the
    # existing ``live_mode`` check already renders ``live=<bool> dry_run=<bool>``.
    check = (ctx.get("checks") or {}).get("live_mode")
    if isinstance(check, dict) and check.get("status") == "warn":
        detail = str(check.get("detail") or "").lower()
        return "live=true" in detail and "dry_run=true" in detail
    return False


def _watchdog_restart_storm(ctx: dict) -> bool:
    if ctx.get("watchdog_alert"):
        return True
    return _keyword_match(ctx, ("restart storm",))


# --------------------------------------------------------------------------------- seed data
# Real issues this project has hit, mapped to the checks/signals that already observe them.
PLAYBOOK: list[PlaybookEntry] = [
    PlaybookEntry(
        id="water_reservoir_empty",
        symptom="Bed won't heat or cool / feels completely unresponsive",
        detect=lambda ctx: _status(ctx, "device_water") == "fail",
        likely_cause="The Hub's water reservoir is empty (has_water=false) — the Pod can't "
                     "run its thermal pump without water.",
        fix="Fill the Hub reservoir to the line, then run PRIME (dashboard Controls -> Prime, "
            "or POST /control/prime). Give it a few minutes to finish priming before judging "
            "whether temperature control is working again.",
    ),
    PlaybookEntry(
        id="watchdog_restart_storm",
        symptom="A component (api/daemon/web) keeps crash-looping / restarting repeatedly",
        detect=_watchdog_restart_storm,
        likely_cause="The watchdog observed more than 5 restarts of one component within a "
                     "5-minute window and put it on a restart-storm hold rather than thrash "
                     "it forever.",
        fix="Read .run/watchdog.log for the 'CRITICAL: RESTART STORM' line to see which "
            "component and why; fix the underlying crash (daemon.err/daemon-crash.log "
            "usually has the traceback). The hold clears itself once the component is "
            "observed healthy again; .run/watchdog.alert is removed automatically at that "
            "point.",
    ),
    PlaybookEntry(
        id="daemon_heartbeat_stale",
        symptom="The control loop looks stuck — nothing is changing on the bed",
        detect=lambda ctx: _status(ctx, "daemon_heartbeat") == "fail",
        likely_cause="The control daemon process is dead, hung, or has otherwise stopped "
                     "writing .run/daemon.heartbeat.",
        fix="Check daemon.log/daemon.err/daemon-crash.log for why it stopped. The watchdog "
            "should auto-restart it within ~15s; if it keeps flapping, run scripts/doctor.ps1 "
            "(or GET /diag/bundle) for a full snapshot.",
    ),
    PlaybookEntry(
        id="dry_run_left_on",
        symptom="Live mode is on but the bed never actually moves",
        detect=_dry_run_left_on,
        likely_cause="SLEEPCTL_DRY_RUN=1 is set while SLEEPCTL_LIVE=1 — the daemon is reading "
                     "real device state and computing real decisions but deliberately sending "
                     "NO commands to the bed.",
        fix="Unset SLEEPCTL_DRY_RUN (or set it to 0) in deploy/.env once you trust the "
            "decisions being logged, then restart the daemon.",
    ),
    PlaybookEntry(
        id="pyeight_auth_failure",
        symptom="Eight Sleep cloud calls fail with an authentication error",
        detect=lambda ctx: _keyword_match(
            ctx, ("unauthorized", "401", "authentication", "auth failed", "auth error",
                  "invalid credentials")),
        likely_cause="pyEight's Eight Sleep cloud session failed to authenticate — the stored "
                     "token expired, or the account password/OAuth client secret changed.",
        fix="Verify EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD in deploy/.env are current (confirm "
            "you can still log into the Eight Sleep app with them). If the account requires "
            "an OAuth client id/secret, see deploy/LIVE_POD.md, then restart the daemon.",
    ),
    PlaybookEntry(
        id="no_credentials_configured",
        symptom="Daemon is running in SIMULATOR mode when a real Pod was expected",
        detect=lambda ctx: _status(ctx, "eight_sleep_creds") == "warn",
        likely_cause="EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD are not both set, so the daemon "
                     "falls back to the built-in simulator instead of talking to the real Pod.",
        fix="Set both EIGHTSLEEP_EMAIL and EIGHTSLEEP_PASSWORD in deploy/.env, then restart "
            "the daemon.",
    ),
    PlaybookEntry(
        id="db_locked",
        symptom="Requests fail intermittently / errors mention the database",
        detect=lambda ctx: _keyword_match(
            ctx, ("database is locked", "database locked", "sqlite3.operationalerror")),
        likely_cause="SQLite is locked, almost always caused by two processes (e.g. a stale "
                     "daemon that never exited) writing the same DB file concurrently.",
        fix="Run scripts/doctor.ps1 and check the PROCESSES section for more than one "
            "run_daemon.py; stop the stale one so only a single process holds the DB.",
    ),
    PlaybookEntry(
        id="port_in_use",
        symptom="The API or web server fails to start",
        detect=lambda ctx: _keyword_match(
            ctx, ("address already in use", "eaddrinuse", "port is already in use",
                  "only one usage of each socket")),
        likely_cause="Another process is already bound to the port the API (8000) or web UI "
                     "(3000) needs — usually a stale process left over from a previous run.",
        fix="scripts/doctor.ps1's PORTS/PROCESSES sections show the PID holding the port; "
            "stop it and let the watchdog restart the service on its next pass.",
    ),
    PlaybookEntry(
        id="calendar_ics_unreachable",
        symptom="The work-shift calendar isn't updating",
        detect=lambda ctx: _keyword_match(ctx, ("ics", "calendar"))
        and _keyword_match(ctx, ("unreachable", "timeout", "fetch failed",
                                 "connection error", "fetch error", "404")),
        likely_cause="The configured CALENDAR_ICS_URL couldn't be fetched — a network issue, "
                     "or the calendar provider's secret URL was revoked/rotated.",
        fix="Re-copy the ICS 'secret address' from your calendar provider into deploy/.env "
            "(CALENDAR_ICS_URL) or the dashboard's calendar settings, then POST "
            "/calendar/refresh.",
    ),
    PlaybookEntry(
        id="device_offline",
        symptom="The Pod/Hub shows offline",
        detect=lambda ctx: _status(ctx, "device_online") == "fail",
        likely_cause="The Hub is reporting offline to Eight Sleep's cloud — usually a "
                     "network or power issue at the Hub itself, or a cloud-side outage.",
        fix="Check the Hub's network connection and power; check status.eightsleep.com for a "
            "cloud-side outage; power-cycle the Hub if it stays offline.",
    ),
]


# --------------------------------------------------------------------------------- matching
def match_playbook(result: dict, events: Optional[list[dict]] = None,
                    run_dir: Optional[str] = None, env: Optional[dict] = None) -> list[dict]:
    """Match every :data:`PLAYBOOK` entry's ``detect`` against the current signals.

    ``result`` -- a diagnostics report dict exposing ``checks`` (a list of ``{"id", "status",
    "detail", "remedy", ...}`` dicts, same shape as ``app.diagnostics.run_diagnostics()`` and
    ``sleepctl.diagnostics.data_diagnostics()`` both produce) — or a bare ``{"checks": [...]}``
    dict, which is all the matcher actually needs (this is what the engine-side unit tests
    feed it, keeping this module import-free of ``dashboard``).
    ``events`` -- recent structured events (``repo.recent_events()`` rows), optional.
    ``run_dir`` -- the ``.run`` directory, used only to check whether ``watchdog.alert``
    exists; optional (skipped entirely when not given).
    ``env`` -- environment mapping to check for the dry-run/live flags; defaults to
    ``os.environ``.

    Never raises: a single entry whose ``detect`` throws is skipped, not fatal.
    """
    checks_list = (result or {}).get("checks") or []
    checks_by_id = {c.get("id"): c for c in checks_list if isinstance(c, dict) and c.get("id")}

    watchdog_alert = False
    if run_dir:
        try:
            watchdog_alert = os.path.exists(os.path.join(run_dir, "watchdog.alert"))
        except Exception:
            watchdog_alert = False

    ctx: dict[str, Any] = {
        "result": result or {},
        "checks": checks_by_id,
        "events": events or [],
        "run_dir": run_dir,
        "watchdog_alert": watchdog_alert,
        "env": env if env is not None else os.environ,
    }

    matches: list[dict] = []
    for entry in PLAYBOOK:
        try:
            if entry.detect(ctx):
                matches.append(entry.as_dict())
        except Exception:
            continue  # one bad predicate must never break the rest of the battery
    return matches


def playbook_catalog() -> list[dict]:
    """The full playbook as plain, JSON-serializable dicts (no ``detect`` callables) — for
    surfacing "every known issue we check for", not just the ones currently matching."""
    return [entry.as_dict() for entry in PLAYBOOK]
