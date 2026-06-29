# Awakening Detection & Prevention on Slow (Cloud) Data — Design & Evidence

**Goal:** prevent nocturnal awakenings (the user's #1 problem) using only the data available
**without rooting the device**, and be honest about what that data can and cannot do.

> **Now personalized.** The fixed precursor thresholds described here are tuned per-person by a
> learner that mines the sensor trajectory before *your* awakenings (HR/HRV drift, breathing
> irregularity, **tossing/turning bursts**, bed warming) vs matched control windows — see
> `awakening_precursor_profile` + `PrecursorDetector.personalize` in
> [SELF_LEARNING.md](SELF_LEARNING.md) §7 and [CONTROL_LAW.md](CONTROL_LAW.md) §5.

## The hard constraint: ~60 s is the floor everywhere

Investigation of the Eight Sleep API surface and the community local-API projects established:

- The cloud's finest vitals are the **~1-minute-binned `trends` timeseries**; `current_heart_rate`
  is literally the last point of that series. There is **no websocket/SSE/MQTT** the library is
  missing — `/users/{id}/trends` is the floor.
- **Rooting would not help the vitals.** The Pod *firmware* derives HR/HRV/RR per minute (the
  local jailbroken API inserts vitals **once every 60 s**). Rooting/raw-capture only exposes the
  raw piezo **waveform**, which you would have to process yourself.

⇒ Prediction on ~60 s data is not a compromise; it is the **only** architecture that exists.
The one thing raw capture would add is faster **movement** detection — a future enhancement, not
required for prevention to work.

## What the evidence says (PubMed)

- Autonomic precursors (HR rise, HRV sympathovagal shift) **precede** cortical arousal, but at
  **beat-to-beat resolution and a seconds-scale lead** (Togo 2006, doi:10.1016/j.clinph.2006.07.314;
  Calandra-Buonaura 2012, doi:10.1016/j.sleep.2011.11.007; Kato 2001,
  doi:10.1177/00220345010800101501). **At 1 sample/min this lead is aliased away.** So our risk
  score is an **arousability-STATE estimator, not a seconds-ahead event predictor.**
- A large fraction of arousals have **no stereotyped slow precursor** at all (BuSha 2001,
  doi:10.1093/sleep/24.5.499); exogenous awakenings (noise, bladder, dreams) carry no minute-scale
  lead. **A meaningful fraction of awakenings is intrinsically unpreventable** from this data.
- Stage/cardiorespiratory **state** *is* recoverable at minute resolution, and improves with
  **rolling multi-minute windows / trend stacking** (Fonseca 2016, doi:10.1109/JBHI.2016.2550104;
  Kwon 2021, doi:10.1109/JBHI.2021.3072644; Weber 2021, doi:10.1109/EMBC46164.2021.9630743) —
  which is exactly how the detectors work (slopes over a window, not single-minute snapshots).
- **Thermal prevention:** a small **+0.4 °C skin warming with core held flat** suppressed
  wakefulness and cut early-morning awakening probability **0.58 → 0.04** (Raymann/Van Someren
  2008, doi:10.1093/brain/awm315). Warming that **raises core** temperature does nothing (Liao
  2013, doi:10.1016/j.ijnurstu.2013.04.006). Cooling/instability **promotes** arousal (Mahapatra
  2005, doi:10.1016/j.physbeh.2004.12.003). Biggest yield is the **final cycles / early morning**.

## Strategy: predictive + prophylactic, not reactive

Because the seconds-scale precursor is invisible at 1/min, prevention is driven by **slow priors**
modulated by **trend triggers**, fused into a graded arousability-risk score:

- **Priors (state, minutes–hours):** light-NREM stage, ~90-min NREM-REM **cycle boundaries**, the
  lighter **back half**, the **circadian core-temp nadir (~03:30–05:30)**, learned **recurring
  awakening clocks** (online n-of-1; not hard-coded — recurrence is an unproven literature claim).
- **Triggers (the within-minute escalation):** HR slope up, HRV decay, bed warming. **Movement
  confirms, it does not lead.**
- **Anticipatory:** start protecting a learned lead-time *before* a predicted window arrives.

These already exist in `controller/wake_risk.py` (`WakeProfile` + `WakeRiskAssessor`) and
`controller/precursor.py` (`PrecursorDetector`), fused in `SleepController`.

## The thermal direction — reconciled for a HOT sleeper

Raymann's lever is small skin **warming** (→ vasodilation → core heat dumped → sleepiness). But
this user is a **hot sleeper whose awakenings are heat-driven** — he already has a heat-*dumping*
problem, so adding skin warmth would likely backfire (and Liao warns against core-loading). So:

- **Active heat trigger** (bed running warm vs target / personal warm threshold) → **cool toward
  neutral.** Direct route to the same end Raymann's warming achieves (lower core heat load).
- **Prophylactic window, no heat trigger** → **prioritize STABILITY** (no abrupt cooling swings —
  instability itself promotes arousal); any warm nudge is kept **small, bounded, and learner-gated**
  (default ≈ neutral), enabled only if *his* data shows warming reduces *his* awakenings
  (`learning/settle.py` sign-learning).

Net: stability-first, small bounded nudges, cooling only on a real heat trigger — evidence-consistent
and "do no harm" for this phenotype.

## Honest limits (do not over-claim)

- It prevents the **predictable, recurring, thermally-mediated** subset — not every awakening.
- It is a **risk/state estimator**, not a seconds-ahead predictor; minute data cannot do the latter.
- Abrupt/exogenous awakenings (noise, bladder, dreams) are **not** predicted early, by design.
- Night-to-night timing recurrence is an **online-learned** prior, not a literature constant.

## Proof on 1-minute data

`tests/test_prevention_backtest.py` validates, at 1-frame-per-minute: a slow pre-arousal drift is
pre-empted **with lead time before the awakening**; stable sleep does **not** false-alarm (no
needless swings); an **abrupt** awakening is honestly **not** predicted early; and the early-morning
structural window raises prophylactic risk with no instantaneous trigger.
