"""Learn the DEEPENING RESPONSE per-person — does "cool-to-deepen" actually move YOUR architecture?

The in-night steerer starts from the evidence (cooler -> more deep; Autopilot RCT). But whether,
and how reliably, a thermal nudge actually tips YOU from light into deep is an n-of-1 question that
only your own data can answer. This learner closes that loop with a clean causal contrast:

  * On most nights the deepen maneuver is ACTUATED (the bed cools when you're light-but-behind the
    deep curve) and logged as an `act` event.
  * On periodic CONTROL nights the steerer makes the SAME judgement but does NOT cool — it logs a
    `shadow` event instead. That measures the natural base rate of slipping into deep on your own.

The causal **lift** is `P(deep | nudged) - P(deep | not nudged)`. If nudging beats the base rate and
doesn't wake you, keep it. If it doesn't help (no lift) or it tends to WAKE you (the one thing the
maneuver must never do), the policy DISABLES actuation — do-no-harm, learned from your own nights,
not assumed. Below the data gate it stays on the evidence-based default (enabled), because the prior
is that cooling helps deep.

Mirrors the other per-phase learners (onset/thermal_wake): pure-python, conservative, bounded,
per-night-MODE when there's enough data, and it ACTIVELY EXPLORES by scheduling the control nights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Data gates: how many of each arm before we'll judge effectiveness / harm (control nights are
# rarer, so its gate is lower). Below these we keep the evidence default (enabled).
_MIN_ACT = 6
_MIN_CTRL = 4
# Do-no-harm thresholds (absolute rate differences).
_WAKE_HARM_LIFT = 0.15      # nudging raises the awakening rate this much over control -> stop
_WAKE_HARM_FLOOR = 0.20     # ...and only if the actuated wake rate is itself non-trivial
_MIN_USEFUL_LIFT = 0.05     # deepen must beat the base rate by at least this, else it's just churn


@dataclass
class DeepeningPolicy:
    enabled: bool                 # actuate the deepen maneuver at all (do-no-harm gate)
    lift: Optional[float]         # P(deep|act) - P(deep|control); None until both arms have data
    wake_lift: Optional[float]    # P(wake|act) - P(wake|control)
    deepen_rate_act: Optional[float]
    deepen_rate_ctrl: Optional[float]
    wake_rate_act: Optional[float]
    n_act: int
    n_ctrl: int
    confidence: float             # 0..1, grows with paired data + effect clarity
    mode: Optional[str]
    is_personalized: bool         # we have enough data to have actually judged it
    rationale: str

    def to_dict(self) -> dict:
        def r(x):
            return round(x, 3) if x is not None else None
        return {"enabled": self.enabled, "lift": r(self.lift), "wake_lift": r(self.wake_lift),
                "deepen_rate_act": r(self.deepen_rate_act),
                "deepen_rate_ctrl": r(self.deepen_rate_ctrl),
                "wake_rate_act": r(self.wake_rate_act), "n_act": self.n_act,
                "n_ctrl": self.n_ctrl, "confidence": round(self.confidence, 2),
                "mode": self.mode, "is_personalized": self.is_personalized,
                "rationale": self.rationale}


def _rate(rows, key) -> Optional[float]:
    return (sum(int(r[key]) for r in rows) / len(rows)) if rows else None


def _filter_mode(records, mode, min_total):
    if mode is None:
        return records
    seg = [r for r in records if (r.get("night_type") or "normal") == mode]
    return seg if len(seg) >= min_total else records


def learn_maneuver_response(records: List[dict], maneuver: str = "deepen",
                            success_key: str = "succeeded",
                            mode: Optional[str] = None) -> DeepeningPolicy:
    """The shared causal core for an in-night thermal maneuver (deepen / lighten).

    records: [{'applied':0/1, <success_key>:0/1, 'caused_wake':0/1, 'night_type':str}, ...].
    Compares the ACTUATED arm to the SHADOW/CONTROL arm to get a confound-free causal estimate of
    (a) the lift toward the target stage and (b) any extra awakenings. ``enabled`` starts True
    (evidence prior) and only flips False once YOUR randomized data shows the maneuver wakes you or
    doesn't beat the natural base rate. Per night-MODE when there's enough data."""
    verb = {"deepen": "deepening", "rem_warm": "lightening (REM)"}.get(maneuver, maneuver)
    target = {"deepen": "deep", "rem_warm": "REM"}.get(maneuver, "the target stage")
    usable = _filter_mode([r for r in records if r.get("applied") in (0, 1)],
                          mode, _MIN_ACT + _MIN_CTRL)
    act = [r for r in usable if r["applied"] == 1]
    ctrl = [r for r in usable if r["applied"] == 0]
    n_act, n_ctrl = len(act), len(ctrl)

    def _srate(rows):
        vals = [int(r.get(success_key, r.get("deepened", 0)) or 0) for r in rows]
        return (sum(vals) / len(vals)) if vals else None
    d_act, d_ctrl = _srate(act), _srate(ctrl)
    w_act, w_ctrl = _rate(act, "caused_wake"), _rate(ctrl, "caused_wake")
    lift = (d_act - d_ctrl) if (d_act is not None and d_ctrl is not None) else None
    wake_lift = (w_act - w_ctrl) if (w_act is not None and w_ctrl is not None) else None

    # Not enough data yet -> keep the evidence-based default ON, don't claim to have judged it.
    if n_act < _MIN_ACT or n_ctrl < _MIN_CTRL:
        return DeepeningPolicy(
            True, lift, wake_lift, d_act, d_ctrl, w_act, n_act, n_ctrl, 0.0, mode, False,
            f"learning — {n_act}/{_MIN_ACT} actuated + {n_ctrl}/{_MIN_CTRL} control nights "
            f"before judging whether {verb} actually works for you")

    confidence = max(0.0, min(1.0, (n_act + n_ctrl - (_MIN_ACT + _MIN_CTRL)) / 16.0))

    # Do-no-harm #1: if actuating raises your awakening rate, STOP — the maneuver must never wake
    # you. The randomized control arm rules out "you'd have woken anyway" — this is the failure
    # mode the user cares about, learned with rigor, not assumed.
    if (wake_lift is not None and wake_lift >= _WAKE_HARM_LIFT
            and (w_act or 0.0) >= _WAKE_HARM_FLOOR):
        return DeepeningPolicy(
            False, lift, wake_lift, d_act, d_ctrl, w_act, n_act, n_ctrl, confidence, mode, True,
            f"DISABLED — {verb} raised your awakening rate "
            f"({w_act:.0%} vs {w_ctrl:.0%} control, +{wake_lift:.0%}); won't do that again")

    # Do-no-harm #2: if it doesn't beat the natural base rate, STOP churning the bed for nothing.
    if lift is not None and lift < _MIN_USEFUL_LIFT:
        return DeepeningPolicy(
            False, lift, wake_lift, d_act, d_ctrl, w_act, n_act, n_ctrl, confidence, mode, True,
            f"DISABLED — {verb} doesn't beat your natural {target} rate "
            f"({d_act:.0%} vs {d_ctrl:.0%} control); holding instead of churning")

    # It works for you: keep actuating.
    return DeepeningPolicy(
        True, lift, wake_lift, d_act, d_ctrl, w_act, n_act, n_ctrl, confidence, mode, True,
        f"{verb} works for you — reaches {target} {d_act:.0%} of the time vs {d_ctrl:.0%} "
        f"without it (+{lift:.0%}), and doesn't wake you")


def learn_deepening(records: List[dict], mode: Optional[str] = None) -> DeepeningPolicy:
    """The deepen ("cool -> more deep") maneuver's per-person causal policy. See
    ``learn_maneuver_response``."""
    return learn_maneuver_response(records, "deepen", "deepened", mode)


def learn_lightening(records: List[dict], mode: Optional[str] = None) -> DeepeningPolicy:
    """The symmetric lighten ("warm -> REM-unblock") maneuver's policy — same causal rigor, target
    stage REM. Only has data when the (off-by-default) REM-unblock is enabled; until then it reports
    the learning state. Mirrors deepening so enabling the maneuver is self-auditing from day one."""
    return learn_maneuver_response(records, "rem_warm", "succeeded", mode)


def next_steer_mode(policy: DeepeningPolicy, night_index: int) -> str:
    """Tonight's steering arm: 'act' (actuate the deepen nudge) or 'observe' (control night — judge
    but don't cool, logging a shadow event). Deterministic by night index (no control-loop
    randomness). When disabled we keep observing to track the natural base rate; otherwise we
    sprinkle in control nights — more often when confidence is low — so the causal lift stays fresh.
    """
    if not policy.enabled:
        return "observe"
    period = 4 if policy.confidence < 0.5 else 8     # ~1-in-4 early, ~1-in-8 once confident
    return "observe" if (night_index % period == 0) else "act"


def deepening_records(repo, nights: int = 60) -> List[dict]:
    """Resolved deepen steer events as learner rows (delegates to the repository reader)."""
    try:
        return repo.maneuver_records("deepen", nights)
    except Exception:
        return []


def lightening_records(repo, nights: int = 60) -> List[dict]:
    """Resolved rem_warm (lighten) steer events as learner rows."""
    try:
        return repo.maneuver_records("rem_warm", nights)
    except Exception:
        return []
