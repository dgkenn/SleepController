"""GO / NO-GO for tonight — one answer to "is this thing actually going to work?"

There are already two diagnostic batteries, and between them they report ~30 checks:

  * ``dashboard/api/app/diagnostics.py`` — RUNTIME (processes, device, thermal loop, sensors)
  * ``sleepctl/diagnostics.py``          — DATA/LEARNING (history depth, learner maturity, config)

Both are the right tools for "what is wrong". Neither answers the question you actually have at
11pm: *can I go to sleep and expect the controller to do its job tonight?* Thirty checks with three
severities is not an answer — and the severities aren't tuned for that question anyway, because
several things that are merely "info" for general health are absolutely disqualifying for a live
controlled night (dry-run mode is the clearest: perfectly healthy, and it means the bed gets no
commands at all).

So this module re-reads the SAME checks through one lens — tonight — and sorts them into:

  BLOCKING  the controller cannot do its job; fix before relying on it
  DEGRADED  it will run, but with a limb tied behind its back
  READY     good to go

It deliberately owns no checks of its own. Everything here is a policy statement about which
existing check matters for which purpose, so there is exactly one place to change that opinion,
and no third battery to keep in sync.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------------------------
# Which runtime checks disqualify a live controlled night, and why. A check id appears here ONLY
# if a failing/warning state means the controller genuinely cannot do its job -- otherwise it
# lands in DEGRADED and the night still goes ahead.
# ---------------------------------------------------------------------------------------------

# fail => BLOCKING. The controller is not running, or the bed physically cannot be steered.
_BLOCK_ON_FAIL = {
    "daemon_heartbeat": "the control daemon isn't running — nothing will steer the bed",
    "api": "the API is down — no commands, no dashboard, no ingest",
    "device_online": "the Pod is unreachable",
    "device_water": "the reservoir is dry — the bed cannot move heat",
    "thermal_capacity": "the water loop can't deliver temperature changes",
    "prevention_timing": "cooling is commanded but the bed never responds",
}

# warn => DEGRADED (never blocking): real, worth knowing, doesn't stop the night.
_DEGRADE_ON_WARN = {
    "watchdog_heartbeat": "nothing is supervising the processes — a crash won't be restarted",
    "runtime_state_fresh": "the daemon's snapshot is stale",
    "priming": "the Pod is priming — normal control resumes when it finishes",
    "thermal_response": "the bed isn't tracking its setpoint",
    "frozen_telemetry": "sensor values have stopped changing",
    "external_conflict": "something else is also commanding the Pod",
    "prevention_timing": "awakening pre-emption is arriving too late to work",
    "degraded": "subsystems are failing silently and being skipped",
    "wake_alarm": "the Pod refuses the alarm write — no vibration; light + warmth only",
    "recent_errors": "the daemon logged errors recently",
    "cloud_errors": "Eight Sleep cloud calls are failing",
}


@dataclass
class PreflightItem:
    id: str
    title: str
    severity: str          # blocking | degraded | note
    detail: str
    remedy: Optional[str] = None


@dataclass
class PreflightReport:
    verdict: str = "GO"                     # GO | GO_DEGRADED | NO_GO
    purpose: str = "controlled night"
    blocking: List[PreflightItem] = field(default_factory=list)
    degraded: List[PreflightItem] = field(default_factory=list)
    notes: List[PreflightItem] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)   # which batteries actually ran

    def to_dict(self) -> dict:
        def _items(xs):
            return [{"id": i.id, "title": i.title, "severity": i.severity,
                     "detail": i.detail, "remedy": i.remedy} for i in xs]

        return {"verdict": self.verdict, "purpose": self.purpose,
                "blocking": _items(self.blocking), "degraded": _items(self.degraded),
                "notes": _items(self.notes), "sources": self.sources}


#: Where the API listens. The Windows watchdog starts uvicorn on 8000 (scripts/windows-watchdog.ps1
#: Start-Api), which is the deployment this runs on — but a hardcoded port would turn any other
#: layout into a permanent, unexplained NO_GO, which is worse than not checking at all. Overridable.
DEFAULT_API_PORT = int(os.environ.get("SLEEPCTL_API_PORT") or 8000)


def api_port_open(host: str = "127.0.0.1", port: Optional[int] = None,
                  timeout: float = 0.5) -> bool:
    """Is something listening on the API port?

    The runtime battery's own ``api`` check is a TAUTOLOGY — it reasons "this function is running,
    therefore a request reached the API process, therefore the API is up". That is sound inside
    ``/diag`` and meaningless here, where we import the battery and call it from a CLI process that
    has nothing to do with the API. Taken at face value it reports a permanently green API on a box
    where the API is dead, which is exactly the false negative a preflight must not have. So probe
    the socket instead of trusting the check."""
    import socket

    try:
        with socket.create_connection((host, int(port or DEFAULT_API_PORT)), timeout=timeout):
            return True
    except Exception:
        return False


def _runtime_checks(repo) -> Optional[List[dict]]:
    """The runtime battery, if the dashboard package is importable from here. Returns None when
    it isn't (a bare engine checkout), so the caller can say so instead of silently passing."""
    try:
        from app.diagnostics import run_diagnostics  # type: ignore
    except Exception:
        try:
            import os
            import sys

            root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            api_dir = os.path.join(root, "dashboard", "api")
            if api_dir not in sys.path:
                sys.path.insert(0, api_dir)
            from app.diagnostics import run_diagnostics  # type: ignore
        except Exception:
            return None
    try:
        return run_diagnostics(repo).get("checks") or []
    except Exception:
        return None


def evaluate(repo, want_sensor: bool = True, cfg=None, checks=None) -> PreflightReport:
    """Build the go/no-go. ``want_sensor`` treats a silent Verity as blocking — the right default
    when the whole point of tonight is a wearable-driven night; pass False for a Pod-only run.

    ``checks`` accepts an already-computed runtime battery. Callers that just ran diagnostics (the
    ``/diag`` endpoint, the health-snapshot publisher) pass theirs rather than paying for a second
    full battery — and, more importantly, get a verdict derived from the SAME observations they are
    reporting, instead of a second sample taken moments later that can disagree with it."""
    rep = PreflightReport()
    if checks is None:
        checks = _runtime_checks(repo)

    if checks is None:
        rep.notes.append(PreflightItem(
            "runtime", "Runtime battery", "note",
            "runtime diagnostics unavailable from this process (dashboard package not importable)",
            "run the preflight on the controller box, or hit GET /diag"))
    else:
        rep.sources.append("runtime")
        by_id = {c.get("id"): c for c in checks}

        # Replace the in-process-only `api` check with a real socket probe before reading the
        # blocking set, so one entry in one place governs it (see ``api_port_open``).
        if not api_port_open():
            by_id["api"] = {"id": "api", "title": "API process", "status": "fail",
                            "detail": f"nothing is listening on 127.0.0.1:{DEFAULT_API_PORT}",
                            "remedy": "the API isn't up — the watchdog should start it; "
                                      "check .run/api.log and .run/api.err"}

        for cid, why in _BLOCK_ON_FAIL.items():
            c = by_id.get(cid)
            if c and c.get("status") == "fail":
                rep.blocking.append(PreflightItem(
                    cid, c.get("title") or cid, "blocking",
                    f"{why} — {c.get('detail')}", c.get("remedy")))

        for cid, why in _DEGRADE_ON_WARN.items():
            c = by_id.get(cid)
            if not c or c.get("status") != "warn":
                continue
            # Never list the same check twice: a fail already recorded as blocking wins.
            if any(b.id == cid for b in rep.blocking):
                continue
            rep.degraded.append(PreflightItem(
                cid, c.get("title") or cid, "degraded",
                f"{why} — {c.get('detail')}", c.get("remedy")))

        # Dry run is the trap this whole module exists for: every check green, and the bed is
        # never actually commanded. Healthy by any general measure, useless for tonight.
        live = by_id.get("live_mode")
        if live and "dry_run=True" in (live.get("detail") or ""):
            rep.blocking.append(PreflightItem(
                "live_mode", "Live / dry-run mode", "blocking",
                "SLEEPCTL_DRY_RUN=1 — the controller will decide but send NO commands to the bed",
                "set SLEEPCTL_DRY_RUN=0 in deploy/.env and restart the daemon"))

        # The wearable is the physiology path. If tonight is a sensor night and it is silent,
        # the controller falls back to whatever the Pod reports — which may be nothing at all.
        card = by_id.get("cardiac_sensor")
        if want_sensor and card and card.get("status") != "ok":
            rep.blocking.append(PreflightItem(
                "cardiac_sensor", "Cardiac sensor (Verity)", "blocking",
                f"no wearable physiology — {card.get('detail')}",
                card.get("remedy") or "run scripts/verity-setup.ps1"))

        cal = by_id.get("calibration")
        if cal and cal.get("status") != "ok":
            rep.notes.append(PreflightItem(
                "calibration", "Personal calibration", "note",
                cal.get("detail") or "", cal.get("remedy")))

    # Data/learning battery: never blocking — a thin history means the controller runs on priors,
    # which is the expected state early on, not a fault.
    try:
        from sleepctl.diagnostics import data_diagnostics

        data = data_diagnostics(repo, cfg)
        rep.sources.append("data")
        for c in data.get("checks", []):
            if c.get("status") in ("warn", "fail"):
                rep.notes.append(PreflightItem(
                    c.get("id", "?"), c.get("title", "?"), "note",
                    c.get("detail", ""), c.get("remedy") or None))
    except Exception:
        pass

    if rep.blocking:
        rep.verdict = "NO_GO"
    elif rep.degraded:
        rep.verdict = "GO_DEGRADED"
    else:
        rep.verdict = "GO"
    return rep


def format_report(rep: PreflightReport) -> str:
    """Human-readable, ordered by what to fix first."""
    banner = {"GO": "GO — the controller can do its job tonight",
              "GO_DEGRADED": "GO (degraded) — it will run, with limitations",
              "NO_GO": "NO-GO — fix the blocking items below first"}[rep.verdict]
    out = ["=" * 74, banner, "=" * 74]

    if rep.blocking:
        out.append("")
        out.append(f"BLOCKING ({len(rep.blocking)}) — the controller cannot do its job:")
        for i, it in enumerate(rep.blocking, 1):
            out.append(f"  {i}. {it.title}: {it.detail}")
            if it.remedy:
                out.append(f"     fix: {it.remedy}")
    if rep.degraded:
        out.append("")
        out.append(f"DEGRADED ({len(rep.degraded)}) — it will run, with a limb tied behind its back:")
        for i, it in enumerate(rep.degraded, 1):
            out.append(f"  {i}. {it.title}: {it.detail}")
            if it.remedy:
                out.append(f"     fix: {it.remedy}")
    if rep.notes:
        out.append("")
        out.append(f"NOTES ({len(rep.notes)}) — not blocking tonight:")
        for it in rep.notes:
            out.append(f"  - {it.title}: {it.detail}")
    if not (rep.blocking or rep.degraded or rep.notes):
        out.append("")
        out.append("  nothing to report — every check is green.")

    if not rep.sources:
        out.append("")
        out.append("  WARNING: no diagnostic battery could be run, so this verdict means little.")
    out.append("")
    return "\n".join(out)
