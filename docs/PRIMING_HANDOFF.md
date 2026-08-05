# Handoff: the perpetual-priming issue

Read this before touching the stuck prime. It exists because the obvious reading of the telemetry
is wrong in a specific, expensive way.

## It is not a sensor

Two separate things get called "the broken sensor" on this project. Keep them apart:

| | What | Status |
|---|---|---|
| **A** | The **Pod's own biometric sensors** (HR / HRV / breathing / sleep stage — *and the sensed bed temperature*) | Effectively unavailable — all of it comes down the trends pipeline, which requires an active Eight Sleep Autopilot membership. This is *why* the project uses a Polar Verity Sense. |
| **B** | The **water loop / prime** — `thermal_capacity: FAIL, stuck_prime` | The live problem. This is an **actuator**, not a sensor. |

The perpetual-priming issue is **B**. An agent sent to debug "a sensor" will read `sleepctl/adapters/`
and find nothing wrong, because nothing there is wrong.

## The signature

From the last published health snapshot:

```
device_water      ok    "reservoir has water"          -> needs_priming == False
device_online     ok    "device reports online"
priming           warn  "the Pod is currently priming" -> device.priming == True
thermal_response  ok    "state=ok (at setpoint)"
thermal_capacity  FAIL  "stuck_prime: priming continuously for 297.4 min (> 6 min)
                         without completing (last_prime='2026-07-18T19:54:06.000Z')"
```

Detector: `sleepctl/diagnostics_thermal.py::analyze_thermal_capacity`, threshold
`STUCK_PRIME_SECONDS = 360`. It fires when `device.priming` has been continuously `True` across
`state_history` for more than six minutes.

## There is no bed thermometer on this box — use the water-side level

The obvious instrument is the wrong one, and reaching for it costs a night.

**`raw_samples.bed_temp_f` is empty here, permanently.** It comes from the trends session
timeseries (`tempBedC`) — the *same membership-gated pipeline* as HR/HRV/stage. No Autopilot
subscription means it is `NULL` on every row, forever, not merely session-gated. Two further
cautions if you ever do see a value:

1. It is also **session-gated** even when the membership is active: `None` for the first ~15–30 min
   of a night and whenever no sleep session is open.
2. Never substitute pyEight's `current_bed_temp` — that is *derived from the commanded level*, so
   feeding it into this reasoning makes the loop read its own command back as "temperature".
3. And cover-side temperature was measured, live, to be an **ambient artifact**: with the cover
   bypassed into a bucket in a hot room it *rose* while the bed was commanded to max cool
   (`sleepctl/controller/thermal_health.py`, top docstring).

**The signal that actually works, with no membership at all, is `currentDeviceLevel`** — exposed as
`{side}HeatingLevel` on the fast device GET, recorded in `thermal_samples.device_level`. It is the
Hub's own water-temp-derived *achieved* level, and it is a different field from the commanded
`{side}TargetHeatingLevel`. When the element works it ramps toward the target (~1.5 levels/min
cooling near neutral); when it does not — low water, cover disengaged, hardware fault — it sits
flat despite the command. That ramp-vs-flat is the whole discriminator:

- **H1 — real hydraulic fault.** Commanded a large cool; `device_level` stays flat.
- **H2 — stuck status flag.** `device_level` tracks the command normally; only `device.priming`
  latched `True` and never cleared.

A note on what `thermal_response: ok "at setpoint"` is worth: it is **not** circular — it compares
achieved against commanded, which is real feedback. But "at setpoint" with `gap ≈ 0` only says the
bed is already where it was asked to be, which is uninformative if nothing large was ever commanded.
Weak evidence, not no evidence. Force the question with an excursion instead of reading a bed at
rest.

The instrument is already written, and prefers whichever trace exists:

```python
from sleepctl.learning.prevention_timing import (
    measure_arrival_min,        # bed_temp_f  — only with an active membership
    measure_level_arrival_min,  # device_level — always available
)
measure_level_arrival_min(rows, start)   # minutes until the level fell 2, or None
```

`None` across several strong commanded cooling moves is real evidence for H1; movement is proof of
H2. `scripts/verify_sensors.py --db <path>` reports the same per channel — note its "Bed
temperature (thermal feedback)" row already reads `thermal_health`, i.e. the level, not a
thermometer.

`prevention_timing.from_repo` distinguishes **"the bed did not move"** from **"we could not see the
bed"** and reports the latter as `no_thermal_data`, explicitly *not* as an accusation against the
water loop. If you make it accuse the loop again, you have reintroduced the bug this section exists
to prevent.

## There is no automatic workaround, and that is deliberate-ish

`sleepctl/repair.py::reenqueue_if_stuck` self-heals two device states:

- `device.needs_priming == True` → enqueue `prime`
- `thermal_health.state == "stalled"` → enqueue `safe_default`

**Neither fires here.** In this signature `needs_priming` is `False` and `thermal_health.state` is
`"ok"`. So nothing retries automatically, by omission rather than by design.

Before adding one: enqueueing another `prime` while the device already claims to be priming is at
best a no-op, and re-priming on a loop is how you burn a pump. Establish H1 vs H2 first.

## What has been tried

- The user topped off the reservoir. **Unverified** — health publishing died 2026-07-19 and the
  snapshot has not moved since, so there is no post-refill telemetry.
- The documented physical remedy (`analyze_thermal_capacity`'s own): distilled water top-off,
  reseat the hub↔cover connectors, then re-prime.
- Deeper field notes, including the air-bound-loop procedure and the "three controllers fight over
  the setpoint" failure mode: `docs/THERMAL_WATER_LOOP_DEBUGGING.md`. Read it.

## Do not do this yet

Leave the **thermal dose-response trial** off (`ThermalTrialConfig.enabled = False`). Running an
n-of-1 temperature experiment on a Pod that may not be moving heat would faithfully measure a
broken actuator and produce a confident, wrong answer about the user's ideal temperature.

## Suggested order

1. Get the box publishing again (`scripts\doctor.ps1`; check `watchdog.heartbeat` age first —
   days old means the box was away, fresh means the git push is failing).
2. Confirm whether the prime is *still* stuck post-refill, or whether the flag cleared.
3. If still stuck: run a strong commanded cooling move (the thermal self-test does exactly this)
   and watch whether `device_level` ramps toward the target or sits flat. That is the H1/H2
   discriminator, and it needs no membership and no sleep session.
4. H1 → physical remedy above. H2 → the flag is cosmetic; consider whether `analyze_thermal_capacity`
   should require corroborating evidence (no `bed_temp_f` movement) before calling `stuck_prime`,
   so a latched flag alone stops producing a FAIL.
