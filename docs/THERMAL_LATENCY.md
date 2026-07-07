# Thermal actuation latency — empirical measurements (Pod 2.1, live)

The bed does **not** reach a commanded temperature instantly — there is a multi-minute lag
between a `set_temp` command and the water/surface physically arriving. This latency is
**large, asymmetric, and state-dependent**, and every time-sensitive maneuver (onset cascade,
smart-wake ramp, pre-emptive settle) must *lead* its command to account for it. It also matters
for **interpreting sleep data**: a physiological response (stage change, HR/HRV shift) to a
thermal command lags the *command* by this actuation latency **plus** the sensing lag — so when
the deepening-response learner / architecture steerer correlate "we cooled → did it deepen?",
they must offset by this latency, not compare at the same timestamp.

## Measured rates (2026-07-03, live, dgkenn Pod 2.1, "left" side — historical; default is now right)

Units: device *level* (−100..+100) and approximate °F (the level↔°F map is non-linear, denser
near neutral 0 ≈ 81 °F).

| Maneuver | From → To | Time | °F/min | levels/min | Source |
|---|---|---|---|---|---|
| Warm, near neutral | 79.5 → 87.4 °F (−6 → +23) | 7.2 min | ~1.1 | ~4.0 | 88 °F run 6:18–6:26 ET |
| Warm, near neutral | 74 → 79 °F (−31 → −8) | 6.7 min | ~0.75 | ~3.4 | 80 °F run 6:10–6:17 ET |
| Warm, big gap from cold | 68 → 74 °F (−54 → −31) | ~3 min | ~2.0 | ~7.7 | 80 °F run 6:07–6:10 ET |
| Warm **from very cold** | 59 → 66 °F (−85 → −68) | ~13 min | ~0.5 | ~1.3 | cascade warm-pulse 5:29–5:41 |
| **Cool, near neutral** | 83.8 → 78.1 °F (+10 → −12) | 14.3 min | ~0.40 | ~1.5 | max-cool run 9:11–9:26 ET (roughly linear) |
| **Cool, already cold** | 63 → 60 °F (−75 → −84) | ~11 min | ~0.3 | ~0.8 | cold-settle @ cmd −93, 5:16–5:27 |

> **Now auto-captured.** As of the `thermal_samples` table (daemon writes a row every control
> tick the bed is actively heating/cooling — ts, device_level, target_level, signed delta,
> direction, room_temp_f, state, session_mode), these curves accumulate continuously and can be
> pulled via `GET /diag/thermal-samples`. The measurements below were the manual bootstrap; the
> live dataset supersedes and extends them (and will gain `room_temp_f` once tracking is active).

## What the numbers say

1. **Warming ≫ cooling.** Warming near neutral is ~4 levels/min (~1.1 °F/min); cooling near
   neutral is ~1.5 levels/min (~0.4 °F/min), decaying to ~0.8 levels/min once the bed is
   already cold — so **warming is ~2.7× faster near neutral and up to ~5× faster vs cold-end
   cooling**. Any lead-time / pre-compensation model must be **asymmetric** (allow much more
   runway to cool than to warm) AND scale the cool runway with how cold the bed already is.
2. **Cooling is hardware-capped, not command-capped.** Commanding a very aggressive −93 still
   only moved the actual level ~0.8 levels/min. Over-driving the cool command does **not** speed
   it up (ambient heat-rejection ceiling). (This is why the "overdrive-then-cut" idea only has
   potential upside on the *warm* direction — see the pending fast-approach measurement.)
3. **State-dependent warming.** Warming is fastest with a large gap near neutral (~7.7 levels/min
   at 68→74 °F) and **slowest from very cold** (~1.3 levels/min at 59→66 °F — cold-water thermal
   inertia), and it **slows as it approaches the setpoint** (proportional-style tail).
4. **Rule-of-thumb lead times** for a step change (until within ~1 °F of target):
   - Warm ~10 °F near neutral: **~7–10 min**
   - Warm from a deep-cold floor: **longer** (~1 °F every ~2 min)
   - Cool ~5 °F: **~15+ min** and capped — plan cooling *well* ahead.

## Implications to calibrate once sleep data flows

- **Onset cascade timing.** The warm-pulse phase (10 min) barely moved the bed off the cold
  floor (60 → 62 °F) *because* warming-from-cold is only ~0.5 °F/min. To make the pulse
  actually felt: lengthen the pulse. (Note: the cascade is now warm→cool, with no cold-settle
  opener; the bed starts from wherever it was when the user gets in bed.) Tune against measured
  onset latency.
- **Smart-wake ramp.** Start the pre-wake warming earlier (≈ desired-Δ°F ÷ 1 °F/min, more from
  a cold start).
- **Pre-emptive settle / steering.** A cooling nudge takes minutes to land — the pre-emption
  must fire on the *leading edge* of rising wake-risk, not at the awakening.
- **Response attribution.** When correlating a thermal action to a sleep-stage/HR response,
  offset the outcome window by ~(actuation latency + sensing lag). Do **not** score at the
  command timestamp — the effect hasn't arrived yet.

## Sensing latency (separate, TBD when data flows)
Cover-side `bed_temp` tracks ambient (not a true feedback signal). Real sensed temps + physiology
(HR/HRV/RR/stage) come from the session-gated trends pipeline with **minutes** of additional lag,
and are only available with an active Autopilot membership (see the no-data diagnosis). Measure
end-to-end sensing lag against an external clock once sessions record, and add it to the
actuation latency above for the full command→observed-response delay.

_Next step (pending, user-approved): the "fast-approach" overdrive measurement on an empty bed —
does commanding max-warm then cutting early beat the direct command on the warm direction?_
