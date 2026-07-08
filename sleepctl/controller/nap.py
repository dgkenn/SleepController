"""Nap strategy — literature-backed selection of how to run a nap.

Naps live or die by duration because of **sleep inertia**:
  - Brooks & Lack 2006 (Sleep, doi:10.1093/sleep/29.6.831): a ~10-min afternoon nap was the
    most recuperative; a 30-min nap caused inertia (grogginess) before any benefit, because it
    reaches slow-wave sleep and wakes the napper out of it.
  - Patterson et al. 2023 (doi:10.1080/10903127.2023.2227696): 30-min and 2-hr naps both caused
    inertia at wake that dissipated within ~10-30 min; the long nap best preserved performance.

So we pick one of three strategies from the available window:
  - POWER  (<= ~25 min): stay light (avoid SWS), hard-cap the wake -> minimal grogginess.
  - CYCLE  (~60-110 min): allow one full NREM-REM cycle, smart-wake in light sleep near ~90 min.
  - TRAP   (~25-60 min): the danger zone (wakes you out of deep sleep). Recommend shortening to
            ~20 or extending to ~90; if forced, wake on the next light-sleep moment, else cap.

Naps starting late in the day erode night sleep, so we flag that. After a longer nap we advise a
short inertia buffer before anything safety-critical.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class NapStrategy(str, Enum):
    POWER = "power"
    CYCLE = "cycle"
    TRAP = "trap"


@dataclass
class NapPlan:
    strategy: NapStrategy
    window_min: int           # the nap opportunity the user asked for
    target_sleep_min: int     # how long we intend to let them sleep
    keep_light: bool          # True -> don't drive deep cooling (avoid SWS / inertia)
    late_day: bool            # starting late enough to risk tonight's sleep
    inertia_buffer_min: int   # advise this buffer before anything critical, post-nap
    headline: str
    advice: str

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
        }


def nap_strategy(window_min: int, now_hour: Optional[int] = None, cfg=None) -> NapPlan:
    """Choose the nap strategy for a given opportunity ``window_min`` (minutes) and clock hour."""
    window_min = max(0, int(window_min or 0))   # a non-positive window is no nap opportunity
    t = getattr(cfg, "tunables", None)
    power_max = getattr(t, "nap_power_max_min", 25)
    cycle_min = getattr(t, "nap_cycle_min_min", 60)
    cycle_target = getattr(t, "nap_cycle_target_min", 90)
    late_hour = getattr(t, "nap_late_hour", 16)
    buffer_min = getattr(t, "nap_inertia_buffer_min", 20)

    late_day = now_hour is not None and now_hour >= late_hour
    late_note = (" Heads-up: napping this late can make it harder to fall asleep tonight."
                 if late_day else "")

    if window_min <= power_max:
        return NapPlan(
            strategy=NapStrategy.POWER, window_min=window_min,
            target_sleep_min=window_min, keep_light=True, late_day=late_day,
            inertia_buffer_min=5,
            headline=f"Power nap (~{window_min} min)",
            advice=("Staying in light sleep and waking you right at the cap — the most "
                    "alertness with the least grogginess." + late_note),
        )
    if window_min >= cycle_min:
        # if they gave more than a cycle, still wake ~one cycle in (more isn't better for a nap)
        target = cycle_target if window_min >= cycle_target else window_min
        return NapPlan(
            strategy=NapStrategy.CYCLE, window_min=window_min,
            target_sleep_min=target, keep_light=False, late_day=late_day,
            inertia_buffer_min=buffer_min,
            headline=f"Full-cycle nap (~{target} min)",
            advice=(f"Letting a full sleep cycle complete and waking you in light sleep near "
                    f"{target} min. Give yourself ~{buffer_min} min before anything critical."
                    + late_note),
        )
    # 25-60 min: the inertia trap
    return NapPlan(
        strategy=NapStrategy.TRAP, window_min=window_min,
        target_sleep_min=power_max, keep_light=True, late_day=late_day,
        inertia_buffer_min=buffer_min,
        headline=f"{window_min} min is the grogginess zone",
        advice=(f"{window_min} min tends to wake you out of deep sleep (worst grogginess). "
                f"Better: ~20 min (quick + sharp) or ~{cycle_target} min (a full cycle). I'll "
                f"keep it light and wake you on the next light-sleep moment, else at the cap."
                + late_note),
    )
