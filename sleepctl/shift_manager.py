"""Shift-aware sleep-debt & circadian manager — for a resident with a variable call schedule.

The rest of the system optimizes *one* night. This plans *across* an erratic shift sequence:
track cumulative sleep debt, and reason forward about the next shift to recommend strategic,
not just in-night, action — bank sleep with a prophylactic nap before a night shift, prioritize
recovery (and flag drowsy-driving safety) after a night/call, hold an anchor-sleep core to keep
the circadian clock from scattering across rotations, and pay debt down on the off nights.

Evidence (PubMed): sleep debt is real and cumulative, and recovery takes multiple nights — chronic
restriction produces dose-dependent neurobehavioral deficits that one long sleep doesn't fully
repay (Van Dongen et al. 2003, doi:10.1093/sleep/26.2.117). Nap duration follows the same
inverted-U as the nap engine: ~10–20 min (power) or ~90 min (full cycle), avoiding the ~30–60 min
SWS-inertia trap (Brooks & Lack 2006, doi:10.1093/sleep/29.6.831). Anchor sleep — a fixed core
sleep period held constant across shifting schedules — stabilizes circadian phase for shift workers.

Pure functions over a schedule + recent nights; no device or I/O. Reuses ``benchmarks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.benchmarks import SLEEP_NEED_MIN, chronic_shortfall, sleep_debt_min


@dataclass
class Shift:
    """One scheduled block. ``kind``: 'day' | 'night' | 'call' | 'off'."""
    start: datetime
    end: datetime
    kind: str = "day"

    @property
    def is_night(self) -> bool:
        return self.kind in ("night", "call")


@dataclass
class NapRec:
    type: str            # 'prophylactic' | 'recovery' | 'anchor'
    when: str            # human window, e.g. "early afternoon"
    duration_min: int
    reason: str

    def to_dict(self) -> dict:
        return {"type": self.type, "when": self.when,
                "duration_min": self.duration_min, "reason": self.reason}


@dataclass
class ShiftPlan:
    debt_min: float
    debt_band: str                       # none | mild | moderate | severe
    tonight_target_min: int
    naps: List[NapRec] = field(default_factory=list)
    anchor_window: Optional[str] = None  # recommended fixed core-sleep window
    warnings: List[str] = field(default_factory=list)
    strategy: str = ""
    rationale: str = ""
    banking: Optional[str] = None        # proactive "extend sleep now" prescription before a block

    def to_dict(self) -> dict:
        return {"debt_min": round(self.debt_min), "debt_h": round(self.debt_min / 60, 1),
                "debt_band": self.debt_band, "tonight_target_min": self.tonight_target_min,
                "tonight_target_h": round(self.tonight_target_min / 60, 1),
                "naps": [n.to_dict() for n in self.naps], "anchor_window": self.anchor_window,
                "warnings": self.warnings, "strategy": self.strategy, "rationale": self.rationale,
                "banking": self.banking}


def _debt_band(debt: float) -> str:
    if debt >= 360:
        return "severe"
    if debt >= 240:
        return "moderate"
    if debt >= 120:
        return "mild"
    return "none"


def _next_shift(shifts: List[Shift], now: datetime) -> Optional[Shift]:
    upcoming = [s for s in shifts if s.start > now and s.kind != "off"]
    return min(upcoming, key=lambda s: s.start) if upcoming else None


def _recent_shift_just_ended(shifts: List[Shift], now: datetime, within_h: float = 10.0):
    """A night/call shift that ended in the last ``within_h`` hours -> post-call recovery mode."""
    for s in shifts:
        if s.is_night and 0 <= (now - s.end).total_seconds() / 3600.0 <= within_h:
            return s
    return None


def _schedule_is_variable(shifts: List[Shift]) -> bool:
    kinds = {s.kind for s in shifts if s.kind != "off"}
    return len(kinds) >= 2  # mixes day & night/call -> rotating -> anchor sleep helps


def plan_shift_sleep(recent_nights, upcoming_shifts: List[Shift], now: datetime,
                     need_min: int = SLEEP_NEED_MIN) -> ShiftPlan:
    """Strategic cross-shift sleep plan: debt, tonight's target, naps, anchor, safety warnings."""
    debt = sleep_debt_min(recent_nights)
    band = _debt_band(debt)
    naps: List[NapRec] = []
    warnings: List[str] = []

    nxt = _next_shift(upcoming_shifts, now)
    post = _recent_shift_just_ended(upcoming_shifts, now)
    hrs_to_next = (nxt.start - now).total_seconds() / 3600.0 if nxt else None

    # tonight's target: baseline need + a bounded slice of debt to repay (recovery takes nights,
    # so repay gradually, not all at once), capped by the opportunity until the next shift.
    target = need_min + min(debt, 120.0)
    if nxt is not None and not nxt.is_night and hrs_to_next is not None and hrs_to_next < 16:
        # day shift tomorrow: opportunity is bounded by when you must be up
        opportunity = max(0.0, hrs_to_next * 60.0 - 30.0)  # leave 30 min to get ready
        target = min(target, opportunity) if opportunity > 0 else target
    tonight_target = int(round(target))

    # --- post-call recovery (highest priority safety state) ---
    if post is not None:
        warnings.append("Post-call: drowsy-driving risk is real — do not drive home impaired; "
                        "nap or get a ride before driving.")
        naps.append(NapRec("recovery", "as soon as you're home", 90,
                           "One full cycle to start repaying the night; extend into longer sleep "
                           "if you can (recovery needs multiple nights, not one)."))
        strategy = "Recovery: pay down the on-shift loss, protect against post-call impairment."

    # --- prophylactic nap before an upcoming night shift ---
    elif nxt is not None and nxt.is_night and hrs_to_next is not None and 2 <= hrs_to_next <= 16:
        dur = 90 if hrs_to_next >= 3.5 else 20
        when = "early/mid afternoon, well before the shift" if dur == 90 else "right before leaving"
        naps.append(NapRec("prophylactic", when, dur,
                           "Bank sleep ahead of the night shift to cut on-shift sleepiness "
                           "(a full cycle if there's time, else a 20-min power nap)."))
        strategy = "Pre-load: nap before the night shift so you start it rested."

    # --- normal / off night: pay down debt ---
    else:
        if band in ("moderate", "severe"):
            strategy = "Repay: a protected long night to draw the debt down."
        else:
            strategy = "Maintain: hold a consistent, full night."

    # --- proactive sleep banking before a known night block (Rupp 2009) ---
    # The day-of prophylactic nap (above) is the last-mile; banking is the days-ahead play.
    # Extending nightly time-in-bed to ~9–10 h in the days before sleep restriction cut on-shift
    # PVT lapses AND made post-restriction recovery faster (Rupp/Wesensten/Balkin 2009,
    # doi:10.1093/sleep/32.3.311). Fires when a night shift is on the horizon but past the
    # immediate prophylactic-nap window (i.e. you have whole nights to bank first).
    BANK_TIB_MIN = 570   # ~9.5 h, midpoint of the 10 h banking arm
    banking = None
    if nxt is not None and nxt.is_night and hrs_to_next is not None and 16 < hrs_to_next <= 72:
        nights_to_bank = max(1, int(round(hrs_to_next / 24.0)))
        banking = (f"Bank sleep now: extend to ~9–10 h in bed for the next "
                   f"{nights_to_bank} night{'s' if nights_to_bank > 1 else ''} before your night "
                   "block. Banked sleep cuts on-shift lapses and speeds your recovery afterward "
                   "(Rupp 2009).")
        # Raise tonight's target toward the banking goal (a whole night away, so no same-day cap).
        tonight_target = max(tonight_target, BANK_TIB_MIN)
        strategy = "Bank: extend sleep ahead of the night block so you start it rested."

    # --- anchor sleep for rotating schedules (circadian stability) ---
    anchor = None
    if _schedule_is_variable(upcoming_shifts):
        anchor = "a fixed ~4 h core (e.g. 03:00–07:00) kept constant across rotations"
        naps.append(NapRec("anchor", "same clock-time every day", 240,
                           "Hold an anchor-sleep core at the same hours across day/night rotations "
                           "to keep your circadian clock from scattering."))

    # --- debt warnings ---
    if band == "severe":
        warnings.append(f"~{round(debt/60,1)} h cumulative sleep debt — protect recovery sleep and "
                        "watch for impaired vigilance on shift.")
    elif band == "moderate":
        warnings.append(f"~{round(debt/60,1)} h sleep debt is building — prioritize a long night soon.")

    if nxt is not None and nxt.is_night and band in ("moderate", "severe") and not post:
        warnings.append("High debt going into a night shift — the prophylactic nap is important, "
                        "not optional.")

    # --- catch-up nap for CHRONIC short sleep (the everyday early-wake regime, no shift needed) ---
    # If you're structurally short night after night (can't wake later, can't always move bedtime),
    # a short daytime nap recovers alertness without the SWS-inertia trap. Only add it when nothing
    # else already prescribed a nap for today, so the card isn't noisy.
    chronic = chronic_shortfall(recent_nights)
    if chronic["is_chronic"] and not post and not any(
            n.type in ("prophylactic", "recovery") for n in naps):
        naps.append(NapRec("catch_up", "early-mid afternoon (not after ~16:00)", 20,
                           f"You're averaging ~{round((chronic['avg_tst_min'] or 0)/60,1)} h — "
                           "chronically short. A 20-min power nap recovers alertness without "
                           "grogginess; keep it before late afternoon so it doesn't erode tonight."))

    rationale = (f"Debt {round(debt/60,1)} h ({band}); "
                 + (f"next shift {nxt.kind} in {round(hrs_to_next,1)} h; " if nxt else "no upcoming shift; ")
                 + (f"post-call recovery. " if post else "")
                 + f"target tonight ≈ {round(tonight_target/60,1)} h.")
    return ShiftPlan(debt_min=debt, debt_band=band, tonight_target_min=tonight_target,
                     naps=naps, anchor_window=anchor, warnings=warnings,
                     strategy=strategy, rationale=rationale, banking=banking)
