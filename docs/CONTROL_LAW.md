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

**Learning the ideal itself (not just how to hit it).** The targets above are the *evidence prior*.
Two learners then personalize what "perfect" means for *you*, both driven by the **morning
subjective survey** (how rested / how groggy / expected daytime energy) — the heavily-weighted
ground truth of recovery:
- `perfect_weights` learns *which metrics matter most* for your felt recovery (revealed preference).
- `ideal_architecture` learns *the target levels themselves* — the deep%/REM% present on the nights
  you rated best becomes your personal ideal, **shrunk to and bounded within ±4 points of the
  evidence prior** (continuity/maintenance never moved). `personalized_targets()` applies both, so
  the index the controller is judged against, and the "what good looks like" shown on the dashboard,
  converge over weeks to *your* optimum without ever drifting from the literature.

**One coherent per-night ideal.** `plan_night()` is the single place tonight's ideal architecture is
assembled from *all* the inputs — wake time / sleep opportunity (which picks the **mode**), sleep
**debt** (which picks the mode and extends the duration target), the **learned** felt-recovery levels,
and **stress** (a stressed night raises the deep floor to *defend* deep when recovery is most at
risk). It folds `personalized_targets()` into the plan, so the dashboard plan, the morning score, the
cross-night policy, and the in-night steerer (§5) all chase the *same* numbers. With thin data it
falls back to the evidence prior.

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

## 5. In-night steering: a single favorable-state controller (acquire · defend · reconcile)

§4 is the **slow loop** (cross-night setpoint learning). On top of it runs a **fast loop** inside
Maintenance (`controller/architecture.py`) that acts *within the night*, within bounds the slow loop
set. It is **one** controller with a single job — **keep you in the most favorable state you can be
in right now** — expressed as two complementary moves and reconciled against the other two in-night
maneuvers by a strict precedence:

- **ACQUIRE a better state** — "I'm lighter than I should be → steer me deeper."
- **DEFEND the good state I'm in** — "I'm in deep / back-half REM → hold cool + stable to keep me
  here and not let me slip out to something worse." (The thermal action matches what Maintenance
  already does per stage; the explicit verdict unifies the reasoning and guards against trading a
  live deep bout away.)

**The precedence (how steering, wake-prevention, and wake-up reconcile — one arbiter, pinned by
tests):**

```
1. wake-PREVENTION  (rising risk / precursor / micro-arousal)  → settle; never deepen into it
2. wake-UP handoff  (within wake_window + ~a cycle of the deadline) → stand down for the ramp
3. favorable-state  → ACQUIRE deeper (if behind, light, early) or DEFEND deep/REM (if in it)
4. else hold
```

So maintenance (#1 priority) always outranks deepening; the steerer hands the bed to the wake-up
trajectory before the deadline so it never deepens you into sleep-inertia; and only with a clear
runway does it acquire/defend. All three share the thermal channel through this one ordering, so they
never fight. The "acquire deeper" details:

- **The ideal curve.** From tonight's personalized targets it builds the ideal *cumulative* deep and
  REM minutes vs time-since-onset: **deep front-loaded** (most SWS in the first cycles — exponent
  `<1`), **REM back-loaded** (grows in the last third — exponent `>1`). The controller accrues your
  *realized* minutes-in-stage each tick, so it always knows the **deficit** ("by now you should have
  ~X min deep; you have Y").
- **The maneuver (asymmetric, evidence-based).** When you are **in light sleep, behind the deep
  curve, in the front ~60 % of the night, and awakening-risk is LOW**, it drives the bed to the deep
  setpoint (`DEEP_BIAS_COOL`; cooler → more deep, Autopilot RCT). Deep is the workhorse and the
  default; the **"nudge lighter"** corollary is only the pre-wake warm ramp (already shipped) plus an
  **off-by-default**, back-third-only REM-unblock that never reduces deep below its floor.
- **Maintenance still wins.** Awakening-risk is the **veto**: the steerer reuses the same pre-empt
  signal (rising wake-risk + leading-edge precursor + micro-arousal), so it never nudges into a
  brewing arousal, and an awakening immediately cancels it. Every move still passes the §2 safety
  chain (slew / variability / clamp), so a deepen nudge can never jolt you.
- **It learns whether it actually works for you.** Each maneuver is edge-logged to the `steer_events`
  ledger and resolved over a 20-min horizon (`deepened?` / `caused_wake?`) — the supervised signal a
  per-person deepening-response model + n-of-1 A/B will use to keep pushing only if it genuinely
  moves *your* architecture (do-no-harm). Pinned by `tests/test_architecture_steering.py`.

## In one sentence

The state machine picks one intent → one evidence-grounded target; biases layer additively and
warming maneuvers bypass the cool bias; each learner owns its own knob; and when architecture
drifts, a single maintenance-first priority maps the deviation to a bounded, held, revert-guarded
±1 °F change — with the rule and ML paths sharing the same maintenance-dominant objective so they
never conflict. Inside the night, one favorable-state controller acquires a deeper state when you're
behind and defends the deep/REM state you're in — reconciled with wake-prevention and the wake-up
ramp by a strict precedence (prevention > wake-up handoff > acquire/defend), so the three never
fight over the bed.
