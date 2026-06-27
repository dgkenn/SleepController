"""Morning readiness / clinical-safety score.

For an anesthesiology trainee, "how degraded am I today?" has real-world safety weight, not
just wellness. This composes the already-built benchmarks into one actionable morning number:

  readiness = sleep quality (perfect_sleep_index) + autonomic recovery (HRV vs baseline)
              + sleep continuity (maintenance — the user's #1 problem) - cumulative sleep debt

and surfaces explicit clinical-safety flags (severe short sleep, high debt, depressed HRV,
fragmented night) with advisory guidance. Advisory only — not a medical device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sleepctl.benchmarks import NightMode, perfect_sleep_index, sleep_debt_min


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _band(score: float) -> str:
    if score < 40:
        return "impaired"
    if score < 60:
        return "compromised"
    if score < 80:
        return "adequate"
    return "prime"


@dataclass
class Readiness:
    score: int                       # 0-100
    band: str                        # impaired | compromised | adequate | prime
    components: dict                 # {sleep_quality, recovery, continuity} each 0-100
    debt_min: float
    flags: List[dict] = field(default_factory=list)  # [{flag, severity, message}]
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "score": self.score, "band": self.band,
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "debt_min": round(self.debt_min),
            "flags": self.flags, "recommendation": self.recommendation,
        }


def morning_readiness(last_night, recent_nights, mode: NightMode = NightMode.NORMAL,
                      baseline_hrv: Optional[float] = None) -> Readiness:
    debt = sleep_debt_min(recent_nights)
    # Score the night against DEBT-AWARE benchmarks: when in debt a good recovery night is
    # deep-heavy + long, so deep/total are rewarded more and onset/efficiency judged tighter.
    psi = perfect_sleep_index(last_night, mode, debt_min=debt)
    quality = float(psi["score"])

    tst = float(getattr(last_night, "total_sleep_min", 0) or 0)
    wake = float(getattr(last_night, "wake_events", 0) or 0)
    waso = getattr(last_night, "waso_min", None)
    eff = getattr(last_night, "sleep_efficiency", None)
    hrv = getattr(last_night, "avg_hrv", None)

    # Autonomic recovery: HRV relative to the user's own baseline (else a 70 ms anchor).
    if hrv is not None and baseline_hrv:
        recovery = _clamp(50.0 + (hrv / baseline_hrv - 1.0) * 200.0)
    elif hrv is not None:
        recovery = _clamp(hrv / 70.0 * 100.0)
    else:
        recovery = quality  # no HRV -> lean on quality

    # Continuity (sleep maintenance is the top priority): awakenings + WASO dominate.
    continuity = 100.0 - min(60.0, wake * 20.0)
    if waso is not None:
        continuity -= min(30.0, float(waso))
    continuity = _clamp(continuity)

    debt_penalty = min(35.0, debt / 600.0 * 35.0)

    base = 0.42 * quality + 0.28 * recovery + 0.30 * continuity
    score = int(round(_clamp(base - debt_penalty)))

    flags: List[dict] = []
    if tst and tst < 360:
        flags.append({"flag": "severe_short_sleep", "severity": "high",
                      "message": f"Only {round(tst/60,1)} h of sleep — expect reduced vigilance."})
    if debt >= 240:
        sev = "high" if debt >= 360 else "medium"
        flags.append({"flag": "sleep_debt", "severity": sev,
                      "message": f"~{round(debt/60,1)} h cumulative sleep debt is building."})
    if hrv is not None and baseline_hrv and hrv < 0.85 * baseline_hrv:
        flags.append({"flag": "low_hrv", "severity": "medium",
                      "message": "HRV is well below your baseline — under-recovered autonomically."})
    if wake >= 3 or (eff is not None and eff < 0.80):
        flags.append({"flag": "fragmented", "severity": "medium",
                      "message": "Fragmented night (awakenings/low efficiency) — sleep maintenance suffered."})
    if tst and tst < 360 and recovery < 50:
        flags.append({"flag": "impairment_risk", "severity": "high",
                      "message": "Short sleep + low recovery: alertness likely impaired. Be cautious "
                                 "with high-stakes clinical tasks; consider a strategic 10-20 min nap "
                                 "and time caffeine to the start of demanding blocks."})

    band = _band(score)
    if any(f["severity"] == "high" for f in flags):
        rec = ("Treat today as a degraded day: protect against errors, lean on checklists, "
               "take a strategic nap if you can, and prioritize an early, consistent bedtime tonight.")
    elif band in ("adequate", "prime"):
        rec = "You're well-recovered — a good day to take on demanding cognitive load."
    else:
        rec = ("Moderate readiness: pace high-stakes work, keep caffeine timed to your hardest "
               "blocks, and aim to pay down debt tonight.")

    return Readiness(score=score, band=band,
                     components={"sleep_quality": quality, "recovery": recovery,
                                 "continuity": continuity},
                     debt_min=debt, flags=flags, recommendation=rec)
