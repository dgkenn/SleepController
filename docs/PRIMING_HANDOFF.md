# Handoff: the perpetual-priming issue

Read this before touching the stuck prime. It exists because the obvious reading of the telemetry
is wrong in a specific, expensive way.

## It is not a sensor

Two separate things get called "the broken sensor" on this project. Keep them apart:

| | What | Status |
|---|---|---|
| **A** | The **Pod's own biometric sensors** (HR / HRV / breathing / sleep stage) — **and the sensed cover/room temperatures**, which ride the same trends pipeline | Effectively unavailable — they require an active Eight Sleep Autopilot membership. This is *why* the project uses a Polar Verity Sense. Documented in `docs/THERMAL_LATENCY.md` ("Sensing latency") and `docs/ALTERNATIVE_SENSORS.md`. **Read those before designing any measurement**: the paywall takes `bed_temp_f` with it, which removes the instrument this doc originally prescribed for B (see "The evidence this doc originally prescribed"). |
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

## The evidence this doc originally prescribed — and why it is UNREACHABLE here

> **CORRECTION (2026-08-04).** The plan below cannot be run on *this* account, and this was
> **already documented** — see `docs/THERMAL_LATENCY.md` ("Sensing latency"), which states that
> real sensed **temps** *and* physiology "come from the session-gated trends pipeline ... and are
> only available with an active Autopilot membership", and `docs/ALTERNATIVE_SENSORS.md`, whose
> whole premise is "the Pod 2 Pro has no Autopilot subscription".
>
> So `bed_temp_f` is not merely session-gated, it is **membership-gated** — it never arrives at
> all. Confirmed by measurement: **0 of 16,782 `raw_samples` rows** have ever carried one, and a
> direct 10-day `GET /v1/users/{id}/trends` returns **`days: 0`**.
>
> This ties the two halves of this document together: `tempBedC` lives in the *same* trends
> timeseries as HR/HRV/breathing/stage, so it is gated by the very Autopilot membership that
> makes item **A** unavailable. **A and B are not independent** — this doc's "it is not a sensor"
> framing is right about the *fault* being an actuator, but wrong to imply the membership gate is
> irrelevant to diagnosing it. The gate is precisely what removes the prescribed instrument.
> (`read_frame` gates every sensed field on `phys_fresh`; with an empty trends pipeline that is
> permanently False — correct behaviour, never close the thermal loop on absent data, but it
> leaves `bed_temp_f` permanently `None`.)
>
> Net: **no `bed_temp_f` discriminator exists on this account without a membership.** Use the
> device-level test in the next section instead. `measure_arrival_min` remains correct and
> useful — it simply has no input here.

`raw_samples.bed_temp_f` — the genuinely **sensed** cover temperature (from the trends
`tempBedC` series). Two cautions, both load-bearing *if you ever have the data*:

1. It is **session-gated**: `None` for the first ~15–30 min of a night and whenever no sleep
   session is open. Absence of data is not evidence of a dead loop. (On a membership-less
   account it is absent permanently — see the correction above.)
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

## The discriminator that DOES work here: device-level convergence

`device_level` (`{side}HeatingLevel`) is **not** command-side, contrary to what the trap section
below implies about it. `thermal_health.py` describes it as the hub's own *water-temp-derived*
plate level, and the live data settles the question: if it merely echoed the command it would
equal `device_target_level` immediately, but it demonstrably **lags by up to 36 levels and
converges gradually**. It therefore carries real plate state and can be used as evidence — with
the one caveat that it says nothing about heat reaching *you* through the cover.

Measured 2026-08-04, target held at −63/−68 throughout, bed empty:

```
priming=True  (18:52-19:19)  level: -50 -42 -40 -41 -28 -35 -37 -42   gap -19..-36, chaotic
priming=False (19:22-19:55)  level: -45 -47 -50 -52 -53 -55 -57 -58 -59 -60   gap -18 -> -8, monotonic
```

Continued to setpoint (live poll, 2 min cadence, empty bed):

```
19:58 -60  20:00 -61  20:02 -61  20:04 -61  20:06 -61  20:08 -62      gap -8 -> -6
```

The regime change is exactly coincident with the prime completing (`last_prime` advanced
`19:52:02Z -> 23:18:51Z`, `priming` True -> False). Chaotic while the pump was purging, then a
clean monotonic ramp that closes the gap to **within `thermal_at_target_margin` (8)** — i.e. "at
setpoint" by the controller's own definition.

## VERDICT (2026-08-04)

**H2 is refuted. H1 was correct, and it is now resolved.**

- H2 (cosmetic latched flag, loop fine all along) predicts normal convergence *throughout*,
  including while the flag was stuck. That is not what happened: the plate could not hold a
  level while `priming` was True, and began converging the moment it cleared. The priming
  state was **real**, not cosmetic.
- H1 (a genuine hydraulic process — air being purged) fits the data, and it **completed**: the
  prime finished, `last_prime` advanced for the first time in this episode, and the loop now
  tracks its commanded target. The owner's reservoir top-off is the most likely cause; that had
  been unverifiable before because health publishing was dead, and is now confirmed working.

**Boundary of this claim — do not overstate it.** `device_level` establishes that the *hub* is
reaching its target water/plate level. It does **not** prove heat arrives at the sleeper through
the cover; only `bed_temp_f` could show that, and it is membership-gated (above). Two residual
caveats worth carrying forward:

1. The observed ramp (~0.45 levels/min mid-range, ~0.2 near setpoint) is slower than the
   "~1-5 levels/min" in `THERMAL_WATER_LOOP_DEBUGGING.md`. Converging and within margin, so not
   a fault — but if thermal intents feel weak in practice, suspect *reduced* (not absent)
   capacity and re-run the purge procedure.
2. Final confirmation that the bed actually feels cold is subjective (lie on it) or needs an
   external thermometer. No telemetry on this account can close that last gap.

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

> **All four steps were run on 2026-08-04 and the issue is RESOLVED** — see the VERDICT above.
> Kept here as the procedure to repeat if the prime ever latches again.

1. ~~Get the box publishing again~~ **DONE.** Publishing had been dead since 2026-07-19 because
   the box was away; it resumed once the stack was restarted. (`scripts\doctor.ps1`; check
   `watchdog.heartbeat` age first — days old means the box was away, fresh means the git push
   is failing.)
2. ~~Confirm whether the prime is still stuck post-refill~~ **DONE — it cleared.** `priming`
   False, `last_prime` advanced to `2026-08-04T23:18:51Z`.
3. ~~Measure `bed_temp_f` arrival inside a session~~ **NOT POSSIBLE on this account** — that
   channel is membership-gated, not merely session-gated (see the correction above). The
   device-level convergence test replaced it and was conclusive.
4. **H1 confirmed and resolved.** No code change is needed to `analyze_thermal_capacity`: the
   H2 "cosmetic flag" case it would have guarded against did not occur, so making `stuck_prime`
   require corroborating `bed_temp_f` stillness would (a) be unimplementable here anyway with no
   `bed_temp_f`, and (b) risk suppressing a real fault. Leave the detector strict.

### Two defects found while diagnosing this (both fixed, commit `529fddb`)

- `diagnostics_thermal._to_dt` stamped naive `state_history` timestamps as UTC when they are
  naive **local**, so on a UTC-4 box every row read 240 min old, the 20-min capacity window
  matched **0 of 181 rows**, and `analyze_thermal_capacity` returned `insufficient_data`
  permanently — the air-bound detector was structurally blind in production. This is also why
  the stuck prime was reported as **297 min when the real duration was ~60 min**; any duration
  in an older snapshot is inflated by the machine's UTC offset.
- `verity_forwarder` omitted `SETTING_CHANNELS` from the PMD ACC start, which real Verity Sense
  firmware refuses (`invalid number of channels`, code 11) — the armband's accelerometer never
  streamed from the production path.
