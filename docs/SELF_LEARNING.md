# Closing the Self-Learning Loop on All Three Phases — Design

**Goal:** over months of data, converge on *your* optimal sleep architecture in **all three
phases** — going to sleep (onset), staying asleep (maintenance), and waking up — and do it
**constraint-aware**, so a short early-wake night and a full recovery night each get their own
learned optimum rather than being averaged into one compromise.

## Where the loop already closed (before this pass)

- **Maintenance**: rolling baselines + tiered policy + the ML action-value recommender choose the
  next setpoint nightly; pre-cool **lead-times** are learned from prevention outcomes; the reward
  is **mode-aware** (different weights for short vs recovery vs normal nights).
- **Wake**: the smart-alarm **window + lift bar** (`wake_tuning`) and the **thermal wake ramp**
  (`thermal_wake`) are learned from your morning grogginess check-ins, with active exploration.
- **Subjective feedback**: morning check-ins re-score the night and feed the reward.

## The gaps this pass closed

### 1. Onset was tracked but never learned — now it is

"Going to sleep" was the one fully open-loop phase: onset latency was logged, but the induction
warm-nudge was a static config value. New `learning/onset_tuning.py` learns the warm-nudge
magnitude that gets **you** to sleep fastest:

- Each night the daemon applies the learned best **plus a rotating exploration jitter**
  (`next_onset_warm_f`) and records the nudge used in the per-night `wake_log`.
- `learn_onset()` bins the recorded nudges by the onset latency they produced and moves toward the
  fastest (shrunk by sample size, clamped to the comfort cap).
- Applied via `controller.set_onset_warm()` → the thermal controller's `ONSET_WARM` target.

### 2. The maintenance settle-nudge was learned but dormant in one daemon

`learn_settle_nudge` (flip the settle direction if pre-cooling isn't preventing awakenings) was
wired in the live daemon but **not** in the simulator daemon the user tests with. Now both apply
it nightly via `controller.set_settle_nudge()`.

### 3. The thermal learners are now constraint-aware (per night-mode)

`learn_onset`, `learn_wake_tuning`, and `learn_thermal_wake` now accept a `mode`
(`normal | constrained | recovery`) and **segment** the data, falling back to the pooled estimate
when a mode lacks enough nights. The daemons pass tonight's mode, so a short night converges on its
*own* fast-onset / narrow-window optimum, separate from full nights. This required carrying the
night-mode into the per-night `wake_log` (a new `night_type` column, plus `onset_warm_f`).

### 4. A unified, visible "learning across all phases" surface

`/learning/phases` (service `learning_phases`) reports, for **onset / maintenance / wake /
architecture**, the learned value, whether it's personalized yet, the nights of data, and a
plain-language rationale — broken out **per mode** for the thermal learners. The Learning page's
**LearningPhasesCard** makes the convergence watchable: green = personalized to you, with
short/recovery nights learning separately.

## The in-night layer: one favorable-state controller, learned causally

On top of the nightly (slow) loop, a **fast loop inside Maintenance** keeps you in the most
favorable state available — it **acquires** a deeper state when you're behind tonight's personalized
ideal curve and **defends** the deep / back-half-REM state you're already in. It is reconciled with
the other two in-night maneuvers by a strict precedence: **wake-prevention** (a brewing arousal →
settle, never deepen into it) **> wake-up handoff** (stand down near the deadline so the smart-wake
ramp lifts you from light sleep — no deepening into inertia) **> acquire/defend > hold**. See
[ARCHITECTURE_STEERING.md](ARCHITECTURE_STEERING.md) and [CONTROL_LAW.md](CONTROL_LAW.md) §5.

### 5. Tonight's ideal is personalized + stress/debt-aware

`plan_night` is the single place tonight's target architecture is designed, folding
`personalized_targets` (learned felt-recovery deep/REM levels + a stress bump that *defends* deep)
onto the mode's debt-extended evidence prior. The dashboard plan, the morning score, the cross-night
policy, and the in-night steerer now chase the *same* numbers.

### 6. Does the deepen nudge actually work for you? — a causal n-of-1 A/B

`learning/deepening.py` answers it rigorously. Most nights ACTUATE the deepen nudge; periodic
**control** nights judge the same situation but don't cool, logging a **shadow** event
(`steer_events.applied=0`). The confound-free **lift** = `P(deep|nudged) − P(deep|not nudged)` and
the awakening rate drive a **do-no-harm gate**: if cooling doesn't beat your natural base rate, or it
raises your awakening rate, the maneuver **disables itself**. The daemons gate tonight's actuation on
the policy and schedule the control nights (more when confidence is low). The symmetric **lightening**
maneuver shares the identical causal core, ready when the off-by-default REM-unblock is enabled.

### 7. Failure-mode audit + personalized awakening prediction

- **Wake-causation audit** (`learning/wake_causation.py`): every mid-sleep adjustment is checked for
  an awakening within a horizon, **controlled for the night's base wake rate** so "you'd have woken
  anyway" is netted out. Reactive maneuvers (a settle fires *because* a wake is brewing) are labelled
  **confounded and never auto-blamed**; only proactive maneuvers with a clear excess are flagged.
- **Personalized awakening-precursor** (`awakening_precursor_profile`): learns the sensor trajectory
  in the minutes *before* your awakenings vs matched control windows, across a comprehensive feature
  set — HR creep + level, HRV decay + level, RR rise + irregularity, a rich movement block
  (restlessness mean, rising trend, peak, **tossing/turning burst count**), and bed warming + level.
  The separating signals tune the precursor detector's HR/HRV/restlessness triggers
  (`PrecursorDetector.personalize`) so pre-emption fires on *your* drift, earlier and more accurately.

## Data plumbing

The per-night `wake_log` row (already one-per-night, written at close-out with the correct date) is
the natural ledger and now also carries `onset_warm_f` and `night_type`. An idempotent
`_apply_migrations()` in `db.py` adds the columns to existing databases. `onset_records()` joins
the logged nudge with the night summary's measured `sleep_onset_latency_min`.

## What is intentionally still open (honest list)

- **New comfort features are not yet learned from outcomes**: the post-wake light dose, the Hue
  therapy lamp, the vibration-pulse rhythm, and the (inert) cold-snap are applied but their effect
  on grogginess isn't yet attributed back. Adding a one-tap "how was the wake?" light/cold rating
  to the check-in is the natural next step.
- **Baselines and the ML setpoint model are still pooled** (mode enters via the reward, not via
  separate per-mode models). Per-mode baselines are a sensible future step; today the per-mode
  segmentation lives in the three thermal phase-learners where it matters most.
- **Gym / shift / bedtime guidance remain advisory** — their decisions aren't yet scored against
  realized outcomes.

## Files

- Per-phase learners: `sleepctl/learning/onset_tuning.py` · `wake_tuning.py` · `thermal_wake.py`
  (per-mode) · `settle.py` · `lead_time.py`.
- Per-night ideal: `learning/perfect_weights.py` + `ideal_architecture.py` ·
  `controller/sleep_plan.py` (`plan_night(repo=…)`).
- In-night steering: `controller/architecture.py` (favorable-state controller) ·
  `controller/maintenance.py`, `controller/controller.py` (steer wiring + shadow logging) ·
  `learning/deepening.py` (causal A/B for deepen + lighten).
- Failure-mode + prediction: `learning/wake_causation.py` (audit + `awakening_precursor_profile`) ·
  `controller/precursor.py` (`personalize`).
- Plumbing: `storage/schema.py` + `repository.py` (`steer_events` ledger + `_apply_migrations`) ·
  `dashboard/daemon/{run,live}_daemon.py` (apply all learners nightly) · `dashboard/api/app/db.py`
  (`wake_log` columns + migration) · `services.py` (`learning_phases`) · `main.py` (`/learning/phases`).
- `dashboard/web/components/LearningPhasesCard.tsx` + `lib/api.ts` + `app/learning/page.tsx`.
