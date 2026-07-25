# What the sleep stager is actually for

A note on optimisation targets, because we spent effort improving the wrong number.

The wearable stager was being tuned on **4-class Cohen's kappa** (wake / light / deep / REM). That is
the conventional sleep-staging benchmark, and it is not what this controller needs. An audit of every
place `frame.stage` is consumed shows the controller makes essentially **two binary decisions**.

## What each distinction actually controls

| distinction | what it drives | value |
|---|---|---|
| **awake vs asleep** | sleep-onset confirmation (INDUCTION -> MAINTENANCE, `sleep_onset.py`), arousal/awakening detection (`arousal.py`), wake-risk and precursor pre-emption, the whole maintenance loop | **CRITICAL** — the user's primary complaint is *staying* asleep |
| **deep vs not-deep** | in-night architecture steering — the "deepen" maneuver compares realized deep minutes to a front-loaded ideal curve (`architecture.py`); and smart wake must never lift the user out of deep sleep (grogginess: Brooks & Lack 2006) | **HIGH** |
| **REM vs light** | one thing: `rem_warm_offset_f` = 1.5 F warm bias during REM (Autopilot RCT) | **LOW** |

REM is grouped **with light** everywhere else that matters:
- `sleep_cycle.py`: `_LIGHT = (LIGHT, AWAKE, REM)` — REM is "liftable" for wake timing, same as light.
- `sleep_onset.py`: REM counts simply as "asleep", identical to light and deep.
- `architecture.py`: the REM-unblock maneuver is `steer_rem_unblock_enabled = False` by default.

So REM discrimination — our **worst-detected class** — buys a single small temperature nudge.

## Why chasing 4-class kappa is actively harmful

It is not merely wasted effort; it can select the wrong model. Concrete case from our own results:
on the apnea cohort, adding HRV features **raised** overall performance while **deep recall fell from
0.244 to 0.143**. Under a 4-class objective that reads as an improvement. Under the objective that
matters — realized deep minutes feeding the steering loop — it is a regression.

Similarly, the HR+motion variant beat HR-only on 4-class kappa (0.455 vs 0.436) and on wake kappa,
yet **worsened deep-minutes error (23.0 -> 26.5 min) and onset error (5.4 -> 6.7 min)**. We kept
motion disabled for exactly this reason (see `config.py`).

## The metrics we should report

**Primary (these decide control actions):**
1. **Wake vs sleep** — accuracy/kappa/F1, plus per-night **wake-minutes error** and
   **number-of-awakenings error**.
2. **Deep vs not-deep** — accuracy/kappa/F1, plus per-night **deep-minutes MAE**, the single number
   the architecture steering consumes.
3. **Sleep-onset time error** (minutes) — drives the state-machine transition.

**Secondary (diagnostic only):** 4-class kappa, per-class recall including REM.

## Open hypothesis

Dedicated **binary** models for (1) and (2) may beat collapsing a 4-class model's output, because they
do not spend capacity separating REM from light. Worth measuring; if true it is a free improvement on
the axes that matter. Untested as of writing.

## The general lesson

Optimise the metric that changes a control decision, not the metric the literature reports. They are
not the same, and here they actively conflict.

## Current shipped model + numbers (verify against `cv_report.json` before quoting elsewhere)

`sleepctl/ml/sleep_staging/` trains on the open PhysioNet **sleep-accel** dataset (Walch et al.:
wrist HR ± motion → PSG stages), pure-stdlib at inference (`infer.py`/`features.py`; `numpy`/`sklearn`
are training-only deps). The default shipped config is **HR-only** (`stager_use_motion = False` in
`sleepctl/config.py`) — see the "harmful metric" case above for why motion is disabled despite
looking like an improvement on 4-class kappa. The pipeline actually deployed is the 4-class head +
binary wake head blended into an emission, then smoothed by an online HMM forward filter — the `"sm"`
variant in `cv_report.json`, evaluated by **grouped leave-subjects-out CV** (5 folds, split by
subject, so no subject's data leaks between train and test):

| metric | value | what it drives |
|---|---|---|
| 4-class κ | **0.436** | secondary/diagnostic (see "why chasing 4-class kappa is harmful" above) |
| wake κ | **0.450** | the primary signal — asleep/awake, feeds onset + arousal detection |
| deep-minutes MAE | **23.0 min** | the number the architecture steerer actually consumes |
| onset-latency MAE | **5.4 min** | drives the state-machine INDUCTION → MAINTENANCE transition timing |

Retrain any time with `python scripts/fetch_sleep_accel.py` then
`python -m sleepctl.ml.sleep_staging.train`.

**Cross-reference note:** `deploy/VERITY_SENSOR.md` (setup/run doc, outside `docs/`, not edited as
part of this pass) currently quotes older figures — "wake/sleep κ≈0.31, 4-class κ≈0.33" — for the
HR-only leave-subjects-out model. Those numbers predate the smoothing/HMM pipeline reflected in
`cv_report.json` above and are **stale**; treat the table in this section as current, and prefer
`sleepctl/ml/sleep_staging/cv_report.json` itself as the ground truth over either document if they
ever diverge again.

## The `state_estimator` overlay: letting a stage-less wearable drive the controller

The Eight Sleep Pod normally supplies `SensorFrame.stage`. When that's unavailable — no active
Autopilot membership, the Pod sensors aren't reporting, or the only physiology source is the Polar
Verity Sense (see `VERITY_RESEARCH.md`) — `stage` arrives as `UNKNOWN`. Onset detection and the state
machine hard-require a real stage, so a stage-less feed would get stuck in `INDUCTION` forever and
none of the maintenance-time steering (arousal / wake-risk / precursor / architecture) would run.

`sleepctl/controller/state_estimator.py` derives a **coarse** stage from HR, HRV, and movement so
that pipeline can engage off the wearable alone, in priority order:

1. **The learned model above** (`sleepctl.ml.sleep_staging.infer.SleepStager`), if its weights are
   available and enough HR history has accumulated.
2. **A heuristic fallback** (interpretable HR/HRV/movement rules), used when the model weights are
   absent or there isn't enough history yet.

Deliberately conservative in both cases: it only ever returns AWAKE/LIGHT/DEEP, never REM (REM isn't
reliably separable from light sleep by cardiorespiratory + actigraphy alone, and claiming it would
mislead the REM-aware steering — see the "REM vs light" row in the table above). Its confidence is
capped well below a real Pod stage (`est_stage_max_conf`), so the onset detector's own multi-signal
persistence gate still has to independently confirm onset — an estimated LIGHT sample never
fabricates sleep on its own. A real Pod stage, if it ever returns mid-night, always wins over the
estimate. `runtime_state.stage_source` (`"sensor"` / `"model"` / `"heuristic"`) reports which one
supplied tonight's stage, so this is observable on the dashboard rather than silent. Tunable via
`use_learned_stager` / `estimate_stage_from_vitals` in `config.py`.
