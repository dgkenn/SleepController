# Handoff: the perpetual-priming issue

Read this before touching the stuck prime. It exists because the obvious reading of the telemetry
is wrong in a specific, expensive way.

## It is not a sensor

Two separate things get called "the broken sensor" on this project. Keep them apart:

| | What | Status |
|---|---|---|
| **A** | The **Pod's own biometric sensors** (HR / HRV / breathing / sleep stage) | Effectively unavailable — they require an active Eight Sleep Autopilot membership. This is *why* the project uses a Polar Verity Sense. |
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

## The trap — read this twice

**`thermal_response: ok "at setpoint"` does NOT mean the water loop is working.**

`ThermalResponseMonitor` (`sleepctl/controller/thermal_health.py`) compares `target_level` against
`device_level` — *two command-side numbers*. "At setpoint" means only that the Pod acknowledged the
level we asked for. A completely air-bound loop moving no heat at all would still report exactly
this. It is not evidence of heat delivery and must not be used to conclude the loop is fine.

The corollary matters too: you **cannot** currently distinguish these two hypotheses from the
published telemetry alone —

- **H1 — real hydraulic fault.** The loop is air-bound; the prime genuinely never completes.
- **H2 — stuck status flag.** The loop is fine; `device.priming` latched `True` and never cleared.

Anyone who tells you which one it is without the measurement below is guessing.

## The only real evidence

`raw_samples.bed_temp_f` — the genuinely **sensed** cover temperature (from the trends
`tempBedC` series). Two cautions, both load-bearing:

1. It is **session-gated**: `None` for the first ~15–30 min of a night and whenever no sleep
   session is open. Absence of data is not evidence of a dead loop.
2. Never substitute pyEight's `current_bed_temp` — that is *derived from the commanded level*, so
   feeding it into this reasoning makes the loop read its own command back as "temperature".

The instrument is already written:

```python
from sleepctl.learning.prevention_timing import measure_arrival_min
# rows: [{"ts": ..., "bed_temp_f": ...}, ...] spanning a commanded cooling move
measure_arrival_min(rows, start)   # minutes until the bed actually fell 0.5F, or None
```

`None` across several strong commanded moves, *inside a session*, is real evidence for H1.
Movement is proof of H2. `scripts/verify_sensors.py --db <path>` reports the same thing per channel.

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
3. If still stuck: run a strong commanded cooling move **inside a sleep session** and measure
   `bed_temp_f` arrival. That is the H1/H2 discriminator.
4. H1 → physical remedy above. H2 → the flag is cosmetic; consider whether `analyze_thermal_capacity`
   should require corroborating evidence (no `bed_temp_f` movement) before calling `stuck_prime`,
   so a latched flag alone stops producing a FAIL.
