# The Control Law: ideal architecture, conflict reconciliation, and deviation → temperature

This is the precise answer to "are we sure the learning works — are the temperature goals
evidence-grounded, and are all the conflicts (what the temp goal should be, and what action to
take) reconciled?" Every claim here is pinned by `tests/test_control_law.py`.

## 1. The ideal sleep architecture, per situation (evidence-grounded)

"Ideal" is mode-specific. `benchmarks.targets_for()` returns literature-anchored targets (Ohayon
2004 architecture; Ohayon 2017 / NSF continuity), and the per-mode **scoring weights** encode what
matters most in that situation:

| Situation | Deep | REM | Efficiency | What's weighted most |
|---|---|---|---|---|
| **Normal** | 16–20 % | 20–25 % | ≥ 90 % | balanced (duration / continuity / deep / REM) |
| **Constrained** (short, early wake) | **18–22 %** (protect the homeostatically-defended, front-loaded deep) | 16–20 % (de-emphasized) | ≥ 92 % | **continuity** (WASO + awakenings) + efficiency; duration de-weighted |
| **Recovery** (off day / debt) | 18–23 % | **22–26 %** (REM rebound) | ≥ 86 % | **total sleep** + REM + autonomic (HRV) recovery |

The starting **temperature priors** are on the device's 55–110 °F water scale and follow the Eight
Sleep Autopilot-RCT direction (cooler → deep, warmer → REM):

`deep 66 °F  <  neutral 70 °F  <  wake-ramp 74 °F` · REM `+1.5 °F` warm · hot-sleeper `−1.5 °F` ·
settle `−1.0 °F` (cool default) · onset `+1.0 °F` warm (Raymann/Van Someren cutaneous warming).

## 2. Reconciling "what should the temperature be" → one coherent target

There is never a tug-of-war between "deep wants cool" and "REM wants warm" at the same instant,
because the **state machine selects exactly one `ThermalIntent` per tick** (Induction → Maintenance
↔ Wake-recovery → Wake-window). That one intent maps to one target in `thermal.target_for()`:

```
DEEP_BIAS_COOL → deep_bias_f            (cool)     REM_NEUTRAL → neutral + rem_warm_offset (warm)
INDUCTION_COOL → neutral − 2            (cool)     ONSET_WARM  → neutral + onset_warm      (warm)
SETTLE_COOL    → neutral + settle_nudge (signed)   WAKE_RAMP   → wake_ramp_f               (warm)
NEUTRAL        → neutral                            STABILIZE   → hold last
```

Biases then layer **additively and deterministically**, not competitively:
- **hot-sleeper cool bias** + **ambient (weather) pre-compensation** shift the cool/neutral targets
  down — but the deliberately-warm intents (ONSET_WARM, WAKE_RAMP) **bypass the cool bias** so a
  warming maneuver is never cancelled by it.
- **short-night (DAMAGE_CONTROL)** blends cooling intents halfway to neutral (calmer, less
  experimentation when every minute counts).

Then the safety chain reconciles everything into a single safe command:
`target_for → composite closed-loop (body + ambient) → slew ≤ max_step_f → variability cap →
clamp 55–110 °F → device level`. The result is always one number, and it can never jump.

## 3. Reconciling "which learner sets the knob" → no stomping

Each learner **owns a distinct knob**, so they cannot fight over the same value:

| Knob | Owner | Driven by |
|---|---|---|
| `neutral_f`, `deep_bias_f`, `rem_warm_offset_f`, `composite_bed_weight` | **ML action-value learner / tiered policy** | maintenance + architecture outcomes |
| `wake_ramp_f` | **`thermal_wake` learner** | morning grogginess |
| `onset_warm_f` | **`onset` learner** | measured sleep-onset latency |
| `settle_nudge_f` | **`settle` learner** | pre-cool prevention rate |

The ML action set never contains a `wake_ramp_f` delta (pinned by test), so the nightly profile
update and the per-phase learners are orthogonal. Apply order is fixed each night: ML/policy choose
the profile → revealed-preference anchors it toward repeated manual choices → `thermal_wake` merges
its `wake_ramp_f` → the n-of-1 experiment arm (if any) is the top layer for the night → onset/settle
are set as their own thermal fields. Deterministic precedence, not a race.

## 4. Deviation from ideal → exactly what temperature change

When last night's architecture is off-ideal, the nightly learner decides the action by a strict
**priority order that matches the user's #1 goal (stay asleep)** — this is how conflicting
deviation signals are reconciled:

```
1. wake events ↑ vs baseline     → thermal_stability   (hold steady / reduce swings; in-loop,
                                                         via the variability cap + wake-recovery)
2. else deep %  < floor          → deep_bias_cooling    → deep_bias_f −= ~1 °F   (bed runs COOLER in deep)
3. else REM %   < floor          → rem_warming          → rem_warm_offset_f += ~1 °F (WARMER in REM)
4. else                           → small deep-bias nudge (do-no-harm default)
```

So if **both** deep and REM are low *and* wake events are up, **maintenance wins** (pinned by
`test_maintenance_takes_priority_over_architecture`). Every change is:

- **bounded** to one `max_step_f` (~1 °F) per night and clamped to each knob's safe range;
- **held ≥ `min_hold_nights` (3)** before it's judged — never flipped on a single night;
- **reverted only on a robust majority-worse** trail (median + how many post-nights regressed), so
  one bad/confounded night can't undo it.

When the ML path is confident (enough clean nights), it does the same thing through the
action-value learner (`slight_cool / strong_cool / rem_warm_more / skin-blend`), scored by a reward
whose weights encode the *same* priority — **wake events −3.0 (dominant)**, deep +0.30, REM +0.10,
efficiency +0.10, HRV +0.05 — so the ML and rule paths agree on what "better" means and can't pull
in opposite directions. Below the confidence/data gate it falls back to the conservative rule
policy (do-no-harm).

## In one sentence

The state machine picks one intent → one evidence-grounded target; biases layer additively and
warming maneuvers bypass the cool bias; each learner owns its own knob; and when architecture
drifts, a single maintenance-first priority maps the deviation to a bounded, held, revert-guarded
±1 °F change — with the rule and ML paths sharing the same maintenance-dominant objective so they
never conflict.
