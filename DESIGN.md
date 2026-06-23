# SleepController (`sleepctl`) — Design

A personalized, closed-loop sleep-optimization controller for the **Eight Sleep Pod 2**,
driven by the Pod's own bed sensors plus Google Calendar, and built to **learn** from
nightly outcomes over days and weeks.

> **Not a medical device.** This is a comfort/automation tool. It does not diagnose or
> treat anything, avoids risky interventions, and is conservative by design.

---

## 0. Pod 2 / Pod Pro hardware & sensing (research-grounded)

The target device ("Pod 2") is the **Eight Sleep Pod Pro / "Pod 2 Pro" (Model 10501, 2020)**
— a mattress cover + ~1 L water Hub with **active thermoelectric cooling**, so the
hot-sleeper cooling strategy is supported.

- **Temperature scale (authoritative, from pyEight `constants.py`).** Device range
  **55–110 °F**. The app shows **−10…+10**; the API uses **−100…+100** (= 10× the app). The
  level→°F map is a **non-linear lookup table** (`RAW_TO_FAHRENHEIT_MAP`), *not* a formula.
  Anchor points: **level 0 ≈ 81 °F**, −100 = 55 °F, +92 = 110 °F (e.g. 66 °F → −68, 70 °F →
  −49, 74 °F → −31). We vendor this table in `controller/calibration.py` and verify it matches
  pyEight's `util.temp_to_heating_level` exactly. (A naïve linear calibration is ~10 °F off —
  it would run the bed too *warm* for a hot sleeper.)
- **Sensing = ballistocardiography (BCG).** Two piezo sensors → charge-to-voltage → an
  audio-codec ADC concurrently samples both channels → downsampled → uploaded (Eight Sleep
  patent US12048529; this is the `raw-api-upload.8slp.net` stream used by Tier 1). Validated
  vs gold standard: HR < 1 bpm MAE, HRV r² = 0.91 (Pod 3), RR 98%. **BCG requires stillness**,
  so HR/HRV/RR are unreliable during movement — the controller discounts decision confidence
  when movement is high (`SleepController._biometric_reliability`). HR updates per-minute;
  **HRV/RR update per-session** (slower), so the controller never expects fast HRV response.
- **Composite (effective) temperature control.** The Pod's measured **bed-surface
  temperature** already integrates the water setpoint + the sleeper's body heat + the room,
  and the sleeper's **exposed skin** (head/face) feels the **ambient air**. We therefore
  control a blended *effective* temperature `composite = a·bed + (1−a)·ambient`
  (`Tunables.composite_bed_weight`, default a=0.75). Per-stage targets are *effective comfort*
  temperatures; a proportional loop (`composite_feedback_gain`) nudges the commanded water
  temperature to drive `composite → target`, bounded by the slew/variability limits. This
  **self-calibrates** the Eight Sleep water temp to the user's body heat and room conditions:
  a cold room (cold exposed skin) commands a warmer bed, a hot/heat-retaining body commands a
  cooler one. When no measured bed temp is available the loop falls back to feed-forward blend
  inversion; ambient comes from the Pod's room sensor, with outdoor weather only as a fallback.
- **Validated control strategy (Eight Sleep Autopilot RCT, *SLEEP* 2024, abs. 0462).** Cooler
  offsets promote **deep** sleep; warmer offsets promote **REM**; the offset magnitude is
  escalated when the prior night had **deep < 15%** or **REM < 20%**. Measured effects are
  small (HRV +4.9 ms, deep +4.7 min/night). Our controller mirrors this: `DEEP_BIAS_COOL`,
  a small REM warm offset (`Tunables.rem_warm_offset_f`), the deep/REM-fraction escalation
  triggers (`Benchmarks.deep_pct_floor`/`rem_pct_floor`), and conservative small steps with
  multi-night learning — consistent with effects this small.

---

## 1. Executive summary

The user is a quantitatively-minded anesthesiology trainee (5'9", 190 lb, hot sleeper,
back/side sleeper, needs complete silence, late-night worker with variable early wake
times). Their **primary problem is staying asleep** — fragmentation and awakenings, not
falling asleep. `sleepctl` is a four-phase **Sense → Decide → Act → Learn** controller
that:

- actively helps induce sleep with a gentle wind-down + short cool dip;
- treats **awakenings as the top-priority error signal** and runs a wake-recovery mode;
- is stage-aware (cooler/stable in deep sleep, neutral in REM, warm ramp near wake);
- makes only **small, gradual, explainable** thermal changes (≤2 °F steps, hold timers,
  variability cap);
- switches its objective by schedule (full optimization vs. short-night damage control);
- learns per-user response curves with **robust rolling baselines** and a **tiered
  policy** that resists overreacting to a single bad night;
- logs a rich **3-layer dataset** designed for a future hyper-personalized ML model.

Everything runs offline against a deterministic simulator, so the control logic is fully
testable without hardware. Device access is staged across non-invasive data tiers (see
§2) to honor a hard user constraint: **no chance of bricking the Pod**.

---

## 2. System architecture

```
                 ┌─────────────────────────── SENSE ───────────────────────────┐
   Pod 2 bed     │  PodSensorSource (ABC)                                       │
   sensors  ───▶ │   ├─ EightSleepCloudSource   (Tier 0: cloud intervals)      │
                 │   ├─ RawCaptureSource         (Tier 1: redirected raw stream)│
                 │   └─ LocalFrankSource         (Tier 2: gated, last resort)   │
   Google Cal ─▶ │  CalendarSource → ContextRecord (required wake, short-night) │
                 └───────────────────────────────┬─────────────────────────────┘
                                                 │ SensorFrame + ContextRecord
                                                 ▼
                 ┌────────────────────────── DECIDE ───────────────────────────┐
                 │  SleepController.decide()                                    │
                 │   • stale-data guard (hold if data too old)                  │
                 │   • WakeDetector (multi-signal voting)                       │
                 │   • SleepStateMachine (6 guarded states)                     │
                 │   • routine per state: Induction / Maintenance /             │
                 │       WakeRecovery / SmartWake                               │
                 │   • ThermalController: intent → °F → slew → cap → level      │
                 │   ⇒ Decision (state, intent, target_temp_f, target_level,    │
                 │              action, reason, confidence, log_payload)        │
                 └───────────────────────────────┬─────────────────────────────┘
                                                 │ Decision
                                                 ▼
   ┌──────────── ACT ───────────┐     ┌──────────── LEARN (nightly) ───────────┐
   │ ThermalActuator.set_level  │     │ NightlyUpdater:                         │
   │  (-100..100 device level)  │     │  • BaselineEngine (7/14-day median+MAD) │
   │ Repository logs all 3       │     │  • ResponseEstimator (paired nights)    │
   │  dataset layers + ledgers  │     │  • TieredPolicy (try/hold/escalate/     │
   └────────────────────────────┘     │     revert, min-hold-nights)            │
                                       └─────────────────────────────────────────┘
```

**Adapter-tier strategy.** Every data source implements the same `PodSensorSource`
interface, so data fidelity can improve **without any controller change**:

- **Tier 0 — cloud `intervals`** (`EightSleepCloudSource`, via the `pyEight` OAuth2
  library): minute-level HR/HRV/breath/movement/stage, cloud-delivered with latency.
  Always available, zero device contact, cannot brick anything. Ships today.
- **Tier 1 — non-invasive raw capture** (`RawCaptureSource`): redirect the Pod's own
  upload to `raw-api-upload.8slp.net` to a local capture server. No device modification,
  fully reversible. Go/no-go is TLS cert pinning (see `recon/mitm_probe.md`).
- **Tier 2 — on-device root** (`LocalFrankSource`): Frank local API + STM32 USART raw
  tap. **Last resort, triple-gated** (necessity → proven reversibility → minimality);
  ships as a gated stub. See `recon/pod2_teardown.md`.

The runtime (`loop/runtime.py`) ties it together: `tick()` performs one Sense→Decide→Act
cycle and logs everything; `replay()` drives the loop offline from the simulator. The
nightly cycle (`loop/nightly.py`) performs the Learn phase.

---

## 3. State machine

`sleepctl/controller/state_machine.py` — `SleepStateMachine`. Transitions are guarded by
facts the caller supplies (presence, asleep, wake-detected, required wake time) so the
machine never reaches into other subpackages.

```
        presence=True (got in bed)
  IDLE ───────────────────────────▶ INDUCTION
   ▲                                   │ sleep onset confirmed (asleep ≥2 samples)
   │                                   ▼
   │                               MAINTENANCE ──── wake detected ───▶ WAKE_RECOVERY
   │                                   │  ▲                                 │
   │                                   │  └──── physiology re-stabilized ───┘
   │                                   │        (≥ wake_recovery_minutes + stable)
   │       within wake_window_min of required wake time
   │                                   ▼
   │                               WAKE_WINDOW
   └──────── left bed after wake time ───┘
```

| State          | Meaning                                  | Thermal routine        |
|----------------|------------------------------------------|------------------------|
| `IDLE`         | Not in bed                               | NEUTRAL                |
| `CALIBRATION`  | Reserved for °F↔level calibration runs   | NEUTRAL                |
| `INDUCTION`    | In bed, helping sleep onset              | `InductionRoutine`     |
| `MAINTENANCE`  | Asleep, protecting continuity            | `MaintenanceRoutine`   |
| `WAKE_RECOVERY`| Just after an awakening, stabilizing     | `WakeRecoveryRoutine`  |
| `WAKE_WINDOW`  | Inside the smart-wake window             | `SmartWakeRoutine`     |

Guards are deliberately conservative (e.g. onset requires 2 consecutive asleep samples;
recovery requires both a minimum duration **and** a stable streak) to avoid flapping.

---

## 4. Control rules

Pure thermal math lives in `controller/thermal.py` (`ThermalController`); no device I/O.

**Intent → target °F** (hot-sleeper defaults from `config.Tunables`; `neutral_temp_f`=70,
`deep_bias_temp_f`=66, `wake_ramp_temp_f`=74, `hot_sleeper_cool_bias_f`=−1.5 applied to
neutral/deep):

| ThermalIntent     | When                       | Target (hot sleeper)                |
|-------------------|----------------------------|-------------------------------------|
| `WIND_DOWN`       | awake-in-bed, early induction | neutral−1 (gentle, not aggressive) |
| `INDUCTION_COOL`  | late induction             | neutral−2 (short cool dip for onset)|
| `DEEP_BIAS_COOL`  | deep sleep                 | `deep_bias_temp_f` + cool bias      |
| `REM_NEUTRAL`     | REM                        | neutral (avoid overcooling)         |
| `STABILIZE`       | light/unknown, recovery    | hold last target                    |
| `WAKE_RAMP`       | wake window                | `wake_ramp_temp_f` (no cool bias)   |
| `NEUTRAL`         | idle                       | neutral                             |

On short nights (`NightObjective.DAMAGE_CONTROL`) cool intents are nudged toward neutral
to reduce thermal experimentation.

**Safety limiting (always applied, in order):**
1. **Slew limit** — never move more than `max_step_f` (2 °F) per command, anchored to the
   **last commanded target** so the device never receives a jump larger than one step.
2. **Variability cap** — total swing within a short rolling window is clamped to
   `variability_cap_f` (3 °F) to keep the thermal environment stable (the user's biggest
   lever against fragmentation).
3. **Conversion to device level** — linear calibration (default ~0.2 °F per unit, 70 °F at
   level 0), clamped to [−100, +100]. The `calibrate` CLI refines this per user.

**Conflict resolution** follows `config.CONTROL_PRIORITY`:
`sleep_maintenance > stage_confidence > hrv_hr_trend > sleep_opportunity > deep_sleep >
sleep_efficiency > room_temp > secondary_context`.

**Per-tick output contract** — `decide()` returns a `Decision`:
`state`, `objective`, `thermal_intent`, `target_temp_f`, `target_level`, `action`
(`HOLD`/`WARMER`/`COOLER`/`ESCALATE`/`REVERT`), a human `reason`, a `confidence`, and a
`log_payload` of the signals + what to log this tick.

---

## 5. Wake-maintenance strategy

Because staying asleep is the user's core problem, awakenings are a **first-class control
input**, not just an outcome metric.

- **Multi-signal voting** (`WakeDetector`): counts how many of {movement spike, rising
  HR, stage-confidence drop, return to AWAKE/LIGHT from deeper sleep, increased
  respiratory variability, break in a stable low-motion pattern} fire vs. a rolling
  baseline. A probable awakening requires **≥3 signals**; 1–2 signals → do nothing
  dramatic (hold). This makes the detector robust to single noisy blips.
- **Wake-recovery mode**: on a confirmed awakening the state machine enters
  `WAKE_RECOVERY`, which holds the environment steady (neutral/slightly cool), avoids
  rapid changes, and waits for both a minimum duration and a stable physiology streak
  before resuming optimization.
- **Stability over peak metrics**: in light/unknown stages the maintenance routine chooses
  `STABILIZE`, prioritizing thermal stability over chasing extra deep sleep — consistent
  with maintenance outranking deep sleep in `CONTROL_PRIORITY`.
- **Roadmap — preemptive smoothing**: the dataset records awakening timestamps per night
  (`raw_samples.wake_event`), enabling a future pass that detects recurring same-time
  awakenings and pre-smooths the temperature curve around that window on later nights.

---

## 6. Learning algorithm

Conservative, explainable, and resistant to overfitting (`sleepctl/learning/`).

- **Rolling baselines** (`BaselineEngine`): 7- and 14-day **median + MAD** (not
  mean/stdev) for total sleep, deep, REM, efficiency, wake events, WASO, HRV, HR, onset
  latency. Median+MAD means a single bad night barely moves the baseline. Tolerates short
  history.
- **Nightly deltas**: tonight's metrics vs. the 7-day median.
- **Response curves** (`ResponseEstimator`): paired-night comparison of nights **with vs.
  without** a cooling/stabilizing intervention for effects like cooling↔onset-latency,
  cooling↔deep, cooling↔wake-events, cooling↔HRV. Effects are **shrunk toward zero** below
  a minimum paired-sample count, so small samples never drive big changes.
- **Tiered policy** (`TieredPolicy`): `try → hold → escalate | revert`.
  - Start with a **minimal** adjustment aimed at the top priority (wake events).
  - **Hold** every change for `min_hold_nights` (3) before judging it.
  - Judge on a **robust aggregate** of the held nights (median + majority rule), so **a
    single bad night cannot flip the policy**; a *sustained* majority-worse trend reverts.
  - **Escalate** by one small step only if no improvement after the hold window; never
    exceed `max_step_f`.
  - Re-baseline after locking in a change.

---

### 6a. Learnable setpoint profile (ML-ready)

The composite **setpoint is a first-class, persisted, versioned object** (`SetpointProfile`:
per-stage effective targets + blend weight), not a hardcoded constant — because it is the
quantity a future ML model will tailor per user. Each night:

1. the controller runs on the **active** profile version;
2. the night's `NightSummary` is **stamped with that version** (`setpoint_version`);
3. the tiered policy's recommendation is applied to produce the **next** version
   (`learning/setpoints.apply_recommendation`, bounded + small steps), persisted in the
   `setpoints` table with its `source` (`default`/`policy`/`ml`).

This yields clean training rows — join `nightly_summaries.setpoint_version → setpoints.profile`
against the outcome columns — so the ML can learn the optimal per-stage effective temperatures
and blend weight from real (setpoint, context, outcome) tuples. A trained model simply writes a
new `SetpointProfile` with `source="ml"`; nothing else in the controller changes.

### 5a. Smart wake (heat + gentle vibration) & manual-override learning

- **Optimal-moment wake:** inside the wake window before the required time, `SmartWakeRoutine`
  programs a **vibration + thermal** alarm (`set_alarm_direct`, `smart_light_sleep=True`) so the
  Pod wakes the user during **light sleep**, with a warming ramp. **Audio stays OFF** — "silence"
  means no noise; vibration is tactile. Falls back to the hard deadline if no light window occurs.
  Vibration power is configurable (`Tunables.wake_vibration_power`, gentle default).
- **Manual overrides feed the ML (revealed preference):** every manual temperature change is
  logged as `ActionRecord(source="manual")`. `ml/preference.py` anchors the learned setpoint
  toward the **median manual target** (bounded nudge) so the system stops fighting the user and
  settles on their true optimum; and manual-heavy nights are **flagged as confounded**
  (`confounders.py`) so constant tweaking doesn't corrupt the automated reward attribution.

### 6b. Self-learning ML module (`sleepctl/ml/`)

An interpretable **action-value learner** that maps `context → predicted outcome under each
candidate action → smallest effective action → observed reward → updated profile`. Pure
Python (no hard numpy/pandas dep); evolves toward bandit/RL/causal without controller changes.

- **Dataset** (`ml/dataset.py`): joins the 3 layers + setpoints → one `FeatureRow`/night;
  `export` to CSV/parquet. **Features** (`ml/features.py`) add rolling engineered signals
  (bedtime/wake consistency, sleep-opportunity ratio, prev-night fragmentation, rolling HRV
  deviation + wake trend, behavioral flags).
- **Models** (`ml/model.py` + `ml/linalg.py`): per-outcome **ridge** regression (standardized,
  y-centered, **missing-data imputation**), each with an **uncertainty/confidence** from
  residual spread + data support.
- **Reward** (`ml/reward.py`): multi-objective, **maintenance-dominant** (wake events −3/event),
  + deep/REM/HRV/efficiency/total, − onset deviation, − **churn**, − **temp swings**, +
  subjective check-in. Stored per night as `outcome_score`.
- **Actions** (`ml/actions.py`): a small discrete set (no-change, slight/strong cool, slight
  warm, more-REM-warmth, and **blend-weight** `skin_more/less`) → bounded, versioned setpoint
  deltas. **Selection** (`ml/select.py`) takes the **smallest effective** action whose benefit
  clears an **uncertainty-aware** margin; low confidence → no change.
- **Confounders** (`ml/confounders.py`): illness/travel/alcohol/short-sleep nights excluded
  from training (never block control). **Phenotype** (`ml/phenotype.py`): correlation report.
- **Orchestration** (`loop/nightly.py`): each night scores the reward, then **ML when confident
  + ≥ `ml.min_nights` clean nights, else the rule policy** ("do no harm"); logs the action and
  **attributes rewards by the setpoint version it created** (captures delayed multi-night
  effects). Auto-applies the chosen action (gated; `--dry-run` blocks device writes).
- **Cadence**: `train` (weekly refit + propose/`--apply`), `recalibrate` (monthly re-anchor +
  status), `checkin` (subjective labels). Gates in `config.MLConfig`.

## 7. Data schema

SQLite, three dataset layers + three ledgers (`sleepctl/storage/schema.py`), shaped flat
(one row per sample / night / intervention) for easy ML feature extraction.

| Table                | Layer / role                | Key columns |
|----------------------|-----------------------------|-------------|
| `raw_samples`        | 1 — windowed time-series    | ts, night_date, stage, stage_confidence, heart_rate, hrv, respiratory_rate, movement, presence, bed_temp_f, room_temp_f, commanded_level, controller_state, **wake_event**, data_age_seconds |
| `nightly_summaries`  | 2 — nightly rollup          | date PK, total/deep/rem/light min, wake_events, waso_min, sleep_efficiency, onset latency, avg HR/HRV/RR, temp_profile_summary (JSON), intervention_summary (JSON) |
| `context`            | 3 — daytime/schedule antecedents | date PK, required_wake_time, work_start_time, first_commitment, sleep_opportunity_min, is_short_sleep_day, schedule_variable, steps, workout_*, resting_hr_trend, hr_recovery, strain, caffeine, alcohol, screen_time_min, stress, travel, illness, late_night_work, routine_complete |
| `interventions`      | ledger — what we changed    | ts, night_date, controller_state, action, magnitude_f, reason, **held**, **reverted**, **outcome_delta** |
| `decisions`          | per-tick controller output  | ts, night_date, state, objective, thermal_intent, target_temp_f, target_level, action, reason, confidence, log_payload (JSON) |
| `baselines`          | rolling-stat snapshots      | ts, metrics (JSON) |

The intervention ledger captures exactly what the design calls for: timestamp, magnitude,
reason, whether the change was held or reverted, and whether the night improved after it.

---

## 8. Pseudocode

```
# --- per-tick Decide (SleepController.decide) ---
objective = DAMAGE_CONTROL if context.is_short_sleep_day else OPTIMIZE
if frame.is_stale(stale_data_seconds):
    return Decision(HOLD, intent=STABILIZE, reason="data stale; hold")

wake = WakeDetector.evaluate(frame, recent)        # None unless >=3 signals
state = StateMachine.transition(frame, now, wake_detected=bool(wake), required_wake)

intent = {
    INDUCTION:     Induction.step(frame, objective, minutes_in_bed),
    MAINTENANCE:   Maintenance.step(frame, objective),    # DEEP→cool, REM→neutral, else STABILIZE
    WAKE_RECOVERY: WakeRecovery.step(frame),              # STABILIZE
    WAKE_WINDOW:   SmartWake.step(frame, now, required_wake),  # (WAKE_RAMP, should_wake?)
}[state]

target_f = Thermal.target_for(intent, objective, hot_sleeper)
target_f = Thermal.slew_limit(last_target_f, target_f)       # <= max_step_f
target_f = Thermal.enforce_variability_cap(target_f)         # <= variability_cap_f window
level    = Thermal.to_level(target_f)                        # clamp [-100,100]
action   = HOLD | WARMER | COOLER   (vs measured bed temp)
return Decision(state, objective, intent, target_f, level, action, reason, confidence, log)

# --- per-tick Act (Runtime.tick) ---
if level != last_level: actuator.set_level(level); repo.log_intervention(...)
repo.log_sample(frame, state, wake_event, night_date)
repo.log_decision(decision, night_date)

# --- nightly Learn (NightlyUpdater.run) ---
repo.save_night_summary(night)
baselines = BaselineEngine.update(repo.recent_nights(14)); repo.save_baselines(baselines)
deltas    = BaselineEngine.nightly_delta(night, baselines)
response  = ResponseEstimator.estimate(repo.recent_nights(14), repo.recent_interventions())
policy.register_outcome(night)
return policy.recommend(baselines, deltas, response)   # try | hold | escalate | revert
```

---

## 9. Tuning and validation plan

- **Offline simulator replay** (`python -m sleepctl.cli replay`): drives the full loop
  over `normal`, `short_sleep`, and `clustered_awakenings` synthetic nights and asserts
  state progression, wake-recovery triggering, slew/variability limits, smart-wake firing,
  and that all three dataset layers populate.
- **Unit tests** (`pytest tests/`): storage round-trip, thermal slew/clamp, wake-detection
  voting, controller state progression, end-to-end slew invariant, smart-wake firing,
  and learning robustness (single-bad-night vs. sustained-worsening).
- **Tier 0 live capability probe** (`sleepctl auth` then `calibrate`): confirm which
  `current_*`/`intervals` fields and commands the Pod 2 returns, sample real intervals
  latency, and build the °F↔level calibration. Read-only first.
- **Tier 1 pinning test** (`recon/mitm_probe.md`): determines whether non-invasive raw
  capture is viable; if pinned, fall back to Tier 0.
- **Tier 2** is only considered if Tiers 0+1 are insufficient **and** the reversibility
  gate is satisfied (`recon/pod2_teardown.md`).

---

## 10. Failure modes and safeguards

| Failure mode                     | Safeguard |
|----------------------------------|-----------|
| Stale / delayed cloud data       | `is_stale()` guard → HOLD last command; decisions carry a freshness field |
| Noisy single-signal blips        | Wake detection requires ≥3 voting signals; uncertain → do nothing |
| Wake-detection false positive    | Wake-recovery only *stabilizes* (never aggressive); auto-resumes when physiology settles |
| Overcooling / abrupt change      | Slew ≤2 °F/step, variability cap ≤3 °F window, REM stays neutral, no cool bias near wake |
| Sensor field missing (Pod 2)     | All sensor fields Optional; routines defensive against None; capability probe degrades gracefully |
| Cloud / API outage               | Controller holds; Tier 1/2 are independent local fallbacks behind the same interface |
| Single bad night skewing learning| Median+MAD baselines, min-hold-nights, majority-rule revert, response-curve shrinkage |
| **Bricking the device**          | Default path never touches the device (Tier 0/1); Tier 2 gated on a proven, byte-for-byte reversible SD image |
| Need for silence                 | Vibration disabled by default; smart wake is thermal-only |

---

### Personalization summary

| User trait                         | How the controller reflects it |
|------------------------------------|--------------------------------|
| Hot sleeper                        | `hot_sleeper_cool_bias_f` applied to neutral/deep; warm only near wake |
| Trouble **staying asleep**         | Wake events are the top priority; wake-recovery; stability-first maintenance |
| Needs complete silence             | Vibration off by default; thermal-only smart wake |
| Late-night work, variable wake     | Schedule-driven objective switch; wake window sized from Google Calendar |
| Short-sleep nights                 | DAMAGE_CONTROL: faster induction, less experimentation |
| Data-oriented, wants learning      | 3-layer ML-ready dataset + explainable tiered learning over rolling baselines |
