# Documentation index

`docs/` grew incrementally with no index, so this page is the entry point: every doc, grouped by
purpose, with a one-line description. Anything known to be **STALE** or superseded is marked
inline — read the note before trusting that document on its own.

Start with the top-level **[../README.md](../README.md)** (what the system is, how to run it) and
**[../DESIGN.md](../DESIGN.md)** (the original full architecture/design spec — still broadly
accurate, but DESIGN.md itself says its learning-algorithm section is "the v1 foundation" and
defers to the focused docs below for current detail).

## Control law & in-night algorithms

How the controller decides what temperature to run, reconciles competing goals, and learns.

| Doc | What it covers |
|---|---|
| [CONTROL_LAW.md](CONTROL_LAW.md) | The single source of truth for "is the learning grounded and are the conflicts reconciled": the evidence-based per-mode ideal architecture, how one `ThermalIntent` is chosen per tick, how each learner owns a distinct knob, and the deviation→°F law. Every claim pinned by `tests/test_control_law.py`. |
| [ARCHITECTURE_STEERING.md](ARCHITECTURE_STEERING.md) | Design brainstorm + shipped status for the in-night "keep you in the most favorable sleep state" controller (acquire deeper / defend the state you're in), plus the full ML feature taxonomy across onset/maintenance/wake/architecture. |
| [SELF_LEARNING.md](SELF_LEARNING.md) | The three-phase nightly learning loop (onset, maintenance, wake), the in-night favorable-state controller, the causal n-of-1 deepen/lighten A/B, the wake-causation audit, and the personalized awakening-precursor model. The "what's still open" list here is honest about gaps (CBT-I and nap onset-anchoring both cross-linked). |
| [AWAKENING_PREVENTION.md](AWAKENING_PREVENTION.md) | Design + evidence for predicting and pre-empting awakenings on ~60s cloud data — what's possible, what's fundamentally not, and the thermal direction reconciled for a hot sleeper. |
| [MAINTENANCE_STRATEGY.md](MAINTENANCE_STRATEGY.md) | Evidence rationale for predictive pre-emption + the older forensics/n-of-1 experiment engine (`sleepctl/experiments.py`). Cross-links the newer, narrower THERMAL_DOSE_RESPONSE.md trial. |
| [THERMAL_DOSE_RESPONSE.md](THERMAL_DOSE_RESPONSE.md) | **New.** The n-of-1 randomized trial (`sleepctl/ml/thermal_trial.py`) that tests a comfort-clamped maintenance-offset ladder — including *warming* arms, motivated by Raymann 2008 — to find this user's personal optimum. Disabled by default; block-randomized by night type; refuses to name a winner on thin data. |
| [WAKE_SCIENCE.md](WAKE_SCIENCE.md) | The smart-wake subsystem: why *when* you wake matters more than *how*, the dawn-light dose, vibration rhythm, readiness/caffeine guidance — plus the ultradian-cycle-predictor trajectory fix (predictor is now fed all night, not just inside the wake window). |
| [NAPS_AND_INDUCTION.md](NAPS_AND_INDUCTION.md) | **New.** Nap strategy selection (POWER/CYCLE/TRAP) and the on-demand "help me fall asleep" warm→cool induction cascade. Documents what's shipped today (button-press-anchored nap duration) vs. the **planned** onset-anchored redesign — check the code before assuming the planned section has landed. |
| [SHORT_SLEEP.md](SHORT_SLEEP.md) | Optimizing for the user's dominant regime: chronically short sleep from a fixed early wake. The inverse bedtime calculator, chronic-shortfall tracking, catch-up naps. |
| [SLEEP_DEPRIVATION.md](SLEEP_DEPRIVATION.md) | The cross-shift planner for rotating/on-call schedules: proactive sleep banking, anchor sleep, post-call recovery safety. |
| [CBTI.md](CBTI.md) | **New.** Advisory-only CBT-I sleep-window guidance (`sleepctl/cbti.py`) — sleep restriction / stimulus control, computed and explained, never enforced. Shift-safety guards refuse to recommend restriction before on-call duty or when already sleep-deprived. |

## Sensing & hardware

The physical devices, their data paths, and field-debugging notes.

| Doc | What it covers |
|---|---|
| [VERITY_RESEARCH.md](VERITY_RESEARCH.md) | Polar Verity Sense: accuracy research, the PMD BLE implementation (HR/PPI/ACC, never SDK mode), the Verity-specific data-quality guards (frozen-HR / not-worn), and raw RR-interval + actigraphy persistence (400-day retention) and why it's irreplaceable. |
| [../deploy/VERITY_SENSOR.md](../deploy/VERITY_SENSOR.md) | *(Lives in `deploy/`, not `docs/` — linked here for discoverability.)* Setup/run instructions for the Verity forwarder. **Its quoted stager accuracy numbers (κ≈0.31/0.33) are stale** — superseded by the smoothed-HMM pipeline's numbers in [WHAT_THE_STAGER_IS_FOR.md](WHAT_THE_STAGER_IS_FOR.md) (κ 0.436/0.450). |
| [ALTERNATIVE_SENSORS.md](ALTERNATIVE_SENSORS.md) | **STALE recommendation ranking** (kept for background). Surveys non-Eight-Sleep sensors (Emfit, Withings, wearables) for when the Pod has no Autopilot membership. Its #1/#2 picks (Emfit, Withings) were never built; the project instead went deep on its #3 pick (Polar Verity Sense) — see VERITY_RESEARCH.md for what actually shipped. |
| [PASSIVE_CAPTURE.md](PASSIVE_CAPTURE.md) | The two zero-device-risk paths to faster-than-cloud data: passive network capture (unresolved, likely TLS-pinned) vs. wearable fusion (the path that was built out — see VERITY_RESEARCH.md). |
| [TIER1_RAW_CAPTURE.md](TIER1_RAW_CAPTURE.md) | Verdict: intercepting the Pod's own raw sensor upload is a no-go without rooting the device, and Pod 2 cannot be rooted. Explains why ~60s vitals is the floor everywhere for this hardware. |
| [THERMAL_LATENCY.md](THERMAL_LATENCY.md) | Empirical measurements of how long the bed actually takes to reach a commanded temperature (warming ≫ cooling, state-dependent) — required reading before tuning any lead-time/pre-compensation logic. |
| [THERMAL_WATER_LOOP_DEBUGGING.md](THERMAL_WATER_LOOP_DEBUGGING.md) | Field runbook for "heating/cooling feels weak": telemetry-vs-physical diagnosis tree, the air-bound water loop failure mode, and the "three controllers fighting over the setpoint" symptom. |

## ML, data & the wearable stager

| Doc | What it covers |
|---|---|
| [WHAT_THE_STAGER_IS_FOR.md](WHAT_THE_STAGER_IS_FOR.md) | Why the wearable stager should be optimized for wake-vs-sleep and deep-vs-not-deep, not 4-class kappa (with a concrete case where chasing 4-class kappa picked a worse model). Includes the current shipped model's metrics (4-class κ 0.436, wake κ 0.450, deep-minutes MAE 23.0 min, onset MAE 5.4 min, leave-subjects-out) and the `state_estimator` overlay that lets a stage-less wearable feed drive the controller. |
| [PERSONALIZATION_FINDINGS.md](PERSONALIZATION_FINDINGS.md) | A measured negative result: label-free per-person adaptation of the stager doesn't help (and some mechanisms actively hurt). Explains why raw RR/actigraphy persistence (see VERITY_RESEARCH.md) exists — it's what a real across-nights personalization test needs, and it can't be recovered retroactively. Includes the open-dataset survey (BOAS, DOD-H, BIDSleep, etc.). |

## Operations, deploy & diagnostics

| Doc | What it covers |
|---|---|
| [DASHBOARD.md](DASHBOARD.md) | **Original v1 design doc** — architecture, auth, and data model are still accurate, but the API surface has grown since it was written (see the note at the top of the doc for what's not covered: thermal dose-response, CBT-I, learning phases, shift planning, nap/induce sessions, the `/diag*` surface). Treat `dashboard/api/app/main.py` as the source of truth for the current route list. |
| [DIAGNOSTICS_CLI.md](DIAGNOSTICS_CLI.md) | `sleepctl doctor` — the offline, data-side health check (schema, data volume/completeness, learner maturity, calibration, setpoint/config sanity, outcome trend). Distinct from the live-runtime `/diag*` surface below. |
| [CLAUDE_REMOTE_OPS.md](CLAUDE_REMOTE_OPS.md) | Runbook for a fresh Claude session operating a live deployment remotely over the token-gated `/diag*` HTTP surface: discovery, health, known-issue playbook matching, remote actions (restart/reconnect/backup/self-test), and the self-update-and-redeploy flow with its threat model. |

## Notes on how this index was built

Every doc above was read in full and cross-checked against the current code (not just skimmed) as
part of a July 2026 documentation pass. Sections marked STALE were left in place rather than deleted
— they're still useful historical context — but should not be treated as current behavior without
verifying against the code first. If something in this repo contradicts one of these docs and isn't
already flagged, treat the code as authoritative and consider the doc due for another pass.
