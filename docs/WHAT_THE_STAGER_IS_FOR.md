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
