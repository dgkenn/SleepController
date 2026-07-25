"""Nap strategy — literature-backed selection of how to run a nap, anchored to MEASURED sleep,
not to wall-clock time since the button press.

Naps live or die by duration because of **sleep inertia**:
  - Brooks & Lack 2006 (Sleep, doi:10.1093/sleep/29.6.831): a ~10-min afternoon nap was the
    most recuperative; a 30-min nap caused inertia (grogginess) before any benefit, because it
    reaches slow-wave sleep and wakes the napper out of it.
  - Patterson et al. 2023 (doi:10.1080/10903127.2023.2227696): 30-min and 2-hr naps both caused
    inertia at wake that dissipated within ~10-30 min; the long nap best preserved performance.

So we pick one of three strategies from the available SLEEP window (not the time-in-bed window):
  - POWER  (<= ~25 min): stay light (avoid SWS), hard-cap the wake -> minimal grogginess.
  - CYCLE  (~60-110 min): allow one full NREM-REM cycle, smart-wake in light sleep near ~90 min.
  - TRAP   (~25-60 min): the danger zone (wakes you out of deep sleep). Always capped SHORT (stay
            light, wake at the power-nap cap) -- see ``replan_on_onset`` and the TRAP-ZONE POLICY
            note below for why capping short is the only policy consistent with a hard deadline.

Naps starting late in the day erode night sleep, so we flag that. After a longer nap we advise a
short inertia buffer before anything safety-critical.

--------------------------------------------------------------------------------------------------
TIME-ANCHORING (the core fix this module exists to make explicit)

A nap must be dosed against SLEEP, not against wall-clock time since the button was pressed --
conflating the two is the single worst thing a nap controller can do: it turns "wake me in
20 minutes" into "you got 10 minutes of sleep" whenever falling asleep took 10 minutes, and it
turns a 90-min cycle nap into a mid-cycle, out-of-deep-sleep wake (the single worst outcome for
grogginess) whenever onset latency wasn't zero. Two request shapes, kept distinct end-to-end:

  - DURATION request ("nap 20 minutes") = 20 minutes of **sleep**. The planned wake is
    ``onset + target_sleep_min`` -- computed once sleep onset is actually confirmed, not at the
    moment the button was pressed. Because onset might never be confirmed (user never falls
    asleep), a duration nap also carries a FALLBACK cap: ``start + target_sleep_min +
    onset_grace_min`` (see ``fallback_deadline``) so the session can never run forever.

  - WAKE_BY request ("wake me at 15:30") = a HARD deadline that is never moved later. Sleep is
    whatever fits between onset and the deadline; see ``replan_on_onset`` for how the strategy is
    re-evaluated once the TRUE available sleep window (``deadline - onset``) is known.

``nap_strategy()`` alone still only knows the press-time ESTIMATE of the window (for a wake-by
request that estimate wrongly includes time spent falling asleep as if it were sleep; for a
duration request the estimate is already correct by construction, since the target is the
requested duration itself). ``replan_on_onset()`` is the second pass, run once onset is confirmed,
that corrects this for a wake-by request and confirms/finalizes it for a duration request.

TRAP-ZONE POLICY on replan (documented, not implicit -- see ``replan_on_onset``): a realized sleep
window landing in the ~25-60 min inertia trap is capped SHORT (wake before slow-wave sleep sets
in) rather than silently letting the nap ride out to the trap-zone deadline. We deliberately do
NOT offer "extend to a full cycle instead" as a live alternative here, and that is itself a
reasoned choice, not an oversight: ``nap_strategy`` guarantees ``target_sleep_min <= window_min``
for every window, and the trap zone is *defined* as ``window_min`` below the cycle threshold
(<< the ~90-min cycle target). So a trap-zone ``available_sleep_min`` (< 60 min, by definition)
can never leave enough room to reach a ~90-min cycle without exceeding the available window --
i.e. without exceeding the HARD wake-by deadline. Extending would only ever be sound for a caller
with a softer/movable deadline than the "never exceeded" wake-by contract this module implements;
capping short is therefore not a preference among two live options, it is the only policy
consistent with the deadline guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class NapStrategy(str, Enum):
    POWER = "power"
    CYCLE = "cycle"
    TRAP = "trap"


class NapRequestKind(str, Enum):
    DURATION = "duration"    # "nap N minutes" -> N minutes of SLEEP
    WAKE_BY = "wake_by"      # "wake me at HH:MM" -> a hard, never-exceeded deadline


@dataclass
class NapPlan:
    strategy: NapStrategy
    window_min: int           # the sleep opportunity THIS plan was computed against
    target_sleep_min: int     # how long we intend to let them sleep
    keep_light: bool          # True -> don't drive deep cooling (avoid SWS / inertia)
    late_day: bool            # starting late enough to risk tonight's sleep
    inertia_buffer_min: int   # advise this buffer before anything critical, post-nap
    headline: str
    advice: str
    # --- time-anchoring bookkeeping (see module docstring) ---
    request_kind: str = NapRequestKind.DURATION.value
    requested_window_min: int = 0        # what the user actually asked for, never overwritten
    realized_sleep_min: Optional[int] = None   # set once onset is known (see replan_on_onset)
    onset_grace_min: int = 0             # fallback grace (duration requests only; else 0)
    trap_resolution: Optional[str] = None      # "capped_short" | None (see module docstring)
    replanned: bool = False              # has this plan been through replan_on_onset?

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "window_min": self.window_min,
            "target_sleep_min": self.target_sleep_min,
            "keep_light": self.keep_light,
            "late_day": self.late_day,
            "inertia_buffer_min": self.inertia_buffer_min,
            "headline": self.headline,
            "advice": self.advice,
            "request_kind": self.request_kind,
            "requested_window_min": self.requested_window_min,
            "realized_sleep_min": self.realized_sleep_min,
            "onset_grace_min": self.onset_grace_min,
            "trap_resolution": self.trap_resolution,
            "replanned": self.replanned,
        }


def nap_strategy(window_min: int, now_hour: Optional[int] = None, cfg=None, *,
                 request_kind: str = NapRequestKind.DURATION.value,
                 requested_window_min: Optional[int] = None) -> NapPlan:
    """Choose the nap strategy for a given SLEEP opportunity ``window_min`` (minutes) and clock
    hour. ``requested_window_min`` preserves what the user actually asked for (defaults to
    ``window_min`` when the caller doesn't distinguish them -- e.g. a plain press-time estimate)."""
    window_min = max(0, int(window_min or 0))   # a non-positive window is no nap opportunity
    requested_window_min = (int(requested_window_min) if requested_window_min is not None
                            else window_min)
    t = getattr(cfg, "tunables", None)
    power_max = getattr(t, "nap_power_max_min", 25)
    cycle_min = getattr(t, "nap_cycle_min_min", 60)
    cycle_target = getattr(t, "nap_cycle_target_min", 90)
    late_hour = getattr(t, "nap_late_hour", 16)
    buffer_min = getattr(t, "nap_inertia_buffer_min", 20)
    # Fallback grace for a DURATION nap whose onset never gets confirmed (~20-25 min tunable) --
    # see ``fallback_deadline``. Not applicable to a wake-by request (its deadline is already hard).
    grace_min = getattr(t, "nap_onset_grace_min", 22)
    onset_grace = grace_min if request_kind == NapRequestKind.DURATION.value else 0

    late_day = now_hour is not None and now_hour >= late_hour
    late_note = (" Heads-up: napping this late can make it harder to fall asleep tonight."
                 if late_day else "")

    if window_min <= power_max:
        return NapPlan(
            strategy=NapStrategy.POWER, window_min=window_min, target_sleep_min=window_min,
            keep_light=True, late_day=late_day, inertia_buffer_min=5,
            headline=f"Power nap (~{window_min} min)",
            advice=("Staying in light sleep and waking you right at the cap — the most "
                    "alertness with the least grogginess." + late_note),
            request_kind=request_kind, requested_window_min=requested_window_min,
            onset_grace_min=onset_grace,
        )
    if window_min >= cycle_min:
        # if they gave more than a cycle, still wake ~one cycle in (more isn't better for a nap)
        target = cycle_target if window_min >= cycle_target else window_min
        return NapPlan(
            strategy=NapStrategy.CYCLE, window_min=window_min, target_sleep_min=target,
            keep_light=False, late_day=late_day, inertia_buffer_min=buffer_min,
            headline=f"Full-cycle nap (~{target} min)",
            advice=(f"Letting a full sleep cycle complete and waking you in light sleep near "
                    f"{target} min. Give yourself ~{buffer_min} min before anything critical."
                    + late_note),
            request_kind=request_kind, requested_window_min=requested_window_min,
            onset_grace_min=onset_grace,
        )
    # 25-60 min: the inertia trap. Policy: always cap SHORT (see module docstring's TRAP-ZONE
    # POLICY note for why this is the only policy consistent with a hard deadline).
    return NapPlan(
        strategy=NapStrategy.TRAP, window_min=window_min, target_sleep_min=power_max,
        keep_light=True, late_day=late_day, inertia_buffer_min=buffer_min,
        headline=f"{window_min} min is the grogginess zone",
        advice=(f"{window_min} min tends to wake you out of deep sleep (worst grogginess). "
                f"Better: ~20 min (quick + sharp) or ~{cycle_target} min (a full cycle). I'll "
                f"keep it light and wake you on the next light-sleep moment, else at the cap."
                + late_note),
        request_kind=request_kind, requested_window_min=requested_window_min,
        onset_grace_min=onset_grace, trap_resolution="capped_short",
    )


def fallback_deadline(start: datetime, plan: NapPlan) -> datetime:
    """The hard cap for a DURATION nap if sleep onset is never confirmed: ``start +
    target_sleep_min + onset_grace_min``. Guards the "user never actually falls asleep" case so a
    nap can never run forever waiting for sleep that isn't coming. For a WAKE_BY request the real
    deadline is already hard and known up front; this helper is meaningful for duration requests
    only (it still returns a value for any plan, but callers should prefer the actual wake-by
    deadline directly for that request kind)."""
    grace = plan.onset_grace_min if plan.request_kind == NapRequestKind.DURATION.value else 0
    return start + timedelta(minutes=plan.target_sleep_min + grace)


def replan_on_onset(plan: NapPlan, onset: datetime, deadline: datetime, cfg=None) -> NapPlan:
    """Re-run the nap strategy now that sleep onset is CONFIRMED, replacing the press-time
    estimate (which, for a wake-by request, conflated getting-to-sleep time with sleep time) with
    the TRUE available sleep window. Pure and deterministic: same inputs -> same plan.

    ``deadline`` is:
      - DURATION request: the outer bound in effect right now (typically the fallback-cap
        deadline from ``fallback_deadline`` -- since onset is now known, this branch just
        confirms the ORIGINALLY chosen ``target_sleep_min`` unchanged (a duration nap is a
        contract on sleep, not on wall-clock window) and drops the now-moot onset grace.
      - WAKE_BY request: the fixed, hard, never-exceeded wake-by time. ``available_sleep_min =
        deadline - onset`` is the true sleep opportunity, and the strategy is re-run against it.
        A realized window landing in the trap zone is capped SHORT — see the module docstring
        for why that is the ONLY policy consistent with a hard, never-exceeded deadline (there is
        never room to extend to a full cycle without violating it).
    """
    if plan.request_kind == NapRequestKind.DURATION.value:
        return replace(plan, realized_sleep_min=plan.target_sleep_min, onset_grace_min=0,
                       replanned=True)

    available_min = max(0, int((deadline - onset).total_seconds() // 60))
    replanned = nap_strategy(
        available_min, now_hour=onset.hour, cfg=cfg,
        request_kind=NapRequestKind.WAKE_BY.value,
        requested_window_min=plan.requested_window_min,
    )
    if replanned.strategy is NapStrategy.TRAP:
        # See the module docstring: for a HARD, never-exceeded wake-by deadline, capping short is
        # the only policy that cannot violate the deadline -- there is no slack to extend into.
        replanned = replace(
            replanned, trap_resolution="capped_short",
            headline=f"Capping short at ~{replanned.target_sleep_min} min",
            advice=(f"~{available_min} min of real sleep would land you in the grogginess trap "
                    f"(out of deep sleep). Keeping the bed light and capping to "
                    f"~{replanned.target_sleep_min} min instead of riding it out to your "
                    "wake-by time."),
        )
    return replace(replanned, realized_sleep_min=replanned.target_sleep_min, replanned=True)
