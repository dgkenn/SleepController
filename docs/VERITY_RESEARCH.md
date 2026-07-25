# Polar Verity Sense — design-relevant research findings

Sourced research used to settle open design questions before committing to the streaming
architecture. Findings are marked VERIFIED / UNVERIFIED, and anything that applies to the **H10
(chest ECG)** or **OH1** rather than the **Verity Sense (arm PPG)** is flagged — conflating them
would mislead, since the sensing modality differs.

## The headline: the sensor is capable of far better than we currently achieve

**Topalidis et al. 2023, _Sensors_ 23(22):9077 ([10.3390/s23229077](https://doi.org/10.3390/s23229077))**
— the one directly on-point study. 4-class sleep staging **from HRV/inter-beat intervals**, 136 poor
sleepers, comparing PSG/ECG against the Polar H10 and the **Verity Sense**:

| source | accuracy | kappa |
|---|---|---|
| ECG (PSG criterion) | 86.3% | 0.79 |
| Polar H10 (chest ECG) | 84.4% | 0.76 |
| **Polar Verity Sense (arm PPG)** | **84.2%** | **0.75** |

Our shipped HR-only stager reaches 4-class kappa **0.436**. So **the ceiling for this exact device is
not ~0.44 — published work reaches 0.75 with it.** The gap is not the hardware.

Two things that study did which we do not:
1. It used genuine **inter-beat intervals**, not just heart rate.
2. It applied **random-forest-based artifact cleaning to the IBI stream** and a tuned loss function.
   Near-ECG accuracy was reached only *after* that quality-control pipeline, not out of the box.

We now capture raw RR/PPI (see `rr_intervals`), so ingredient 1 is in hand. What blocks us is
training data: no reachable open corpus pairs beat-to-beat intervals with expert stage labels in a
healthy population (see `PERSONALIZATION_FINDINGS.md`). That, not the sensor, is the binding
constraint.

## Accuracy and its failure modes

- **VERIFIED — resting HR accuracy is excellent.** Schweizer & Gilgen-Ammann 2025, _JMIR Cardio_
  9:e67110 ([10.2196/67110](https://doi.org/10.2196/67110)): Verity Sense on the upper arm vs H10 ECG
  criterion, sedentary/lying: bias **-0.05 bpm**, MAE 1.43 bpm, MAPE 1.35%.
- **VERIFIED — tattoos and skin tone degrade accuracy specifically AT REST.** Navalta et al. 2025,
  _Sensors_ 25(22):6896 ([10.3390/s25226896](https://doi.org/10.3390/s25226896)): on tattooed skin at
  rest, MAPE 22.9% and CCC 0.25, versus MAPE <5% / CCC >0.90 on clean skin. Overnight *is* the rest
  condition, so this is a live risk factor, and it argues for surfacing a data-quality/confidence
  signal rather than trusting raw PPI blindly.
- **Flagged, NOT Verity Sense**: Nuuttila et al. 2022 (Polar Vantage V2 **wrist** watch — different
  optics) showed nocturnal PPG RR bias 0.17 ms, r=0.993. Same brand, different hardware; use only as
  a weak analog. Wulterkens 2021 / van Gilst 2020 (Philips **wrist** PPG) are useful mainly for one
  transferable lesson: **ECG-trained algorithms lose accuracy when applied to PPG** (kappa 0.60 →
  0.56) — relevant because our only IBI-bearing training source (`slpdb`) is ECG.

## Transport: BLE vs ANT+

- **VERIFIED**: the Verity broadcasts HR over **BLE and ANT+ simultaneously**, ANT+ on by default;
  up to **2 concurrent BLE connections** plus **unlimited ANT+ receivers** (broadcast, no pairing, no
  connection slot consumed).
- **VERIFIED**: the **accelerometer is not available over ANT+** — ACC/PPG/PPI are exposed only via
  Polar's proprietary PMD service over BLE.
- **UNVERIFIED**: whether the Verity populates the ANT+ HRM profile's beat-event fields with genuine
  PPG-derived beat timing (i.e. usable RR). Suggestive counter-signal: Kubios HRV supports the Verity
  Sense but **does not support ANT+ sensors at all**.

**Decision: BLE/PMD remains the primary transport — no change.** ANT+ cannot carry accelerometer or
confirmed-quality PPI. It remains a plausible *redundant raw-HR fallback* (openant + Garmin ANTUSB
stick) to survive multi-hour BLE dropouts, since it neither pairs nor competes for the BLE slot.
Low priority; not required.

## Overnight reliability

- **VERIFIED (user report)**: ~**40% battery per ~8 h night** streaming HR+ACC — i.e. roughly two
  nights per charge. Comfortably within budget for a single night, but plan a charging cadence.
  No source quantifies battery with **ACC + PPI concurrently**.
- **VERIFIED — firmware updates are a real risk.** Polar's own **v3.0.15 (Dec 2025)** broke a
  third-party BLE app connection for at least one user, disabled USB FlowSync, and **was subsequently
  withdrawn by Polar**. The same update added a warning when dual-BLE + ANT+ + "HR visible to all"
  are enabled together. **Recommendation: validate a known-good firmware and avoid uncontrolled
  updates.** (Note the tension: SDK mode needs ≥1.1.5 and offline recording ≥2.1.0, so some minimum
  is required — but newest is not automatically safest.)
- **Flagged, H10-specific**: an H10 user reports ~2 h of BLE disconnection nightly
  ([polar-ble-sdk#595](https://github.com/polarofficial/polar-ble-sdk/issues/595)), unresolved. Not
  demonstrated for the Verity Sense, but multi-hour body-worn 2.4 GHz attenuation is a transport-layer
  phenomenon common to any body-worn BLE sensor — so treat silent multi-hour stalls as an expected
  failure mode to detect and recover from. (Our forwarder already auto-reconnects.)
- **UNVERIFIED**: whether the device idle-stops when motionless for long periods; and no
  Verity-specific `bleak`-on-Windows defect was found.

## Python/Windows toolkits

There is **no official Python SDK** (Polar's is Kotlin/Swift, Android/iOS only). Two actively
maintained `bleak`-based community libraries claim Verity Sense support:

- **[zHElEARN/polar-python](https://github.com/zHElEARN/polar-python)** — MIT; explicitly documents
  Verity Sense PPG (55 Hz, 22-bit, 4ch), PPI, ACC (52 Hz/8 G), gyro, magnetometer.
- **[fsmeraldi/bleakheart](https://github.com/fsmeraldi/bleakheart)** — MPL-2.0; author states it was
  **tested on a Verity sensor under Windows** — directly relevant to our platform. ACC support is
  documented for H10; PPG for Verity, so coverage of the full ACC+PPI combination needs checking.

**Decision: keep our own codec, but cross-check frame parsing against both.** Neither is mature
enough to obviously beat a working implementation, but both were built against real hardware — which
ours has never touched — so they are cheap validation for exactly the parts we had to infer
(delta-frame bit-packing, PPI flags, the PPI-mode 5 s HR cadence).

## Implementation: what actually ships (PMD service, never SDK mode)

The findings above shaped an implementation, not just a decision. `scripts/polar_pmd.py` is a pure,
I/O-free codec (no BLE dependency, unit-testable without hardware — see `tests/test_polar_pmd.py`)
for Polar's vendor **PMD** (Polar Measurement Data) GATT service, which sits alongside the generic
0x180D Heart Rate Service and carries the streams the generic service hides:

- **ACC** — the armband's *own* triaxial accelerometer. This gives actigraphy without the user's
  iPhone, and in the *same* modality/units (triaxial accel → magnitude in g, reduced to
  PIM/ZCM/MAD/std/peak) as the PhysioNet `sleep-accel` training data — removing the unit-scale
  mismatch that otherwise blocks a clean HR+motion model (see `PERSONALIZATION_FINDINGS.md`).
- **PPI** — pulse-to-pulse intervals with a per-interval error estimate and a "blocker" bit, a
  cleaner HRV source than the generic HR service's RR field.

`scripts/verity_forwarder.py` owns the BLE plumbing and supports three transports (`--mode`):
`hr` (generic 0x180D — the long-standing baseline path), `pmd` (ACC + PPI via PMD), and `auto`
(default — tries PMD, degrading stream-by-stream: ACC refused → PPI-only; PPI refused → ACC + the
generic HR service; both refused or no PMD service at all → generic HR for that session). The log
always states which streams actually started.

**We never enable SDK mode, and `polar_pmd.py` intentionally defines no opcode for it.** Polar's own
documentation is explicit that SDK mode disables every on-device algorithm — "any computed data such
as heart rate, PP intervals, RR intervals, etc. is not available anymore" — in exchange for raw PPG
and higher ACC rates this system does not need. The entire cardiac path here is the
*device-computed* PPI/HR, so SDK mode would silently destroy exactly the signal we came for. The one
real hazard is a device left in SDK mode by a *different* app (e.g. an interrupted third-party
session): PPI then "starts" successfully and simply never delivers a sample. `warmup_state()` detects
that symptom (no PPI well past the documented ~25 s warm-up) so the forwarder can print a specific
hint — power-cycle the armband — instead of surfacing as a generic connection fault.

Two Verity quirks the forwarder's logging is written around: with PPI enabled, HR only updates every
~5 s and the *first* PPI batch takes ~25 s to arrive (normal silence, not a failure); and BLE
body-worn attenuation can cause silent multi-hour stalls (see "Overnight reliability" above), which
the forwarder's auto-reconnect loop is built to recover from.

See `deploy/VERITY_SENSOR.md` for the setup/run instructions (pairing, running the forwarder as a
scheduled task, confirming data is flowing) — this section covers only the *why* of the protocol
choices; that doc covers the *how* of running it and is not duplicated here.

## Data-quality guards: why a frozen HR is dangerous, not just wrong

Polar documents two behaviours of its on-device algorithms that matter for a controller that trusts
this data to steer a bed all night: the HR algorithm can **freeze** (hold the last value) during
motion artifact rather than reporting nothing, and the device's own skin-contact/"not worn" detection
is **unreliable**. Naively trusting either would be actively harmful here, not merely noisy:

- **A frozen HR reads as near-zero variability, which is itself a strong SLEEP signal.** If the
  device freezes HR while the arm is actually moving (e.g. the wearer rolled over or got up), a naive
  feed would show flat HR *during* movement — and flat HR is exactly the kind of low-variability
  signal the sleep stager and onset/wake detectors treat as evidence of stillness/deep sleep. Left
  unguarded, this lets a movement-and-arousal episode masquerade as deep sleep — the opposite of what
  the data actually shows, and a plausible way to *miss* a real awakening.
- **Skin-contact detection is unreliable**, so "the device says it's worn" can't be trusted alone
  either; a genuinely off-body reading needs to be inferred from the data itself.

`assess_cardiac_quality(hr, rr, acc, history, now)` (`dashboard/api/app/services.py`) is a pure,
deterministic guard (no hidden state — same inputs always produce the same output) that flags two
conditions per ingested batch:

- **`hr_frozen`** — the current HR value has repeated for at least `HR_FROZEN_MIN_DURATION_S`
  **while the wearable's own actigraphy shows movement** (`pim >= MOVEMENT_PIM_THRESHOLD`). Both
  conditions are required deliberately: a repeating HR alone is normal resting/deep-sleep physiology
  (or a normal PPI update cadence), and movement alone with a *fresh* HR is normal wakeful activity.
  Only the conjunction — stuck reading *plus* real movement — is the Polar-documented freeze failure
  mode.
- **`not_worn`** — dead-flat actigraphy for a sustained period **and** corroborating absent or
  implausibly-low RR variability (RMSSD below a floor, or no RR data at all). Deliberately
  conservative: actigraphy stillness *alone* describes ordinary deep sleep, so requiring the RR
  corroboration too means the guard would rather miss a genuinely off-body reading (false negative)
  than wrongly discard real, motionless deep-sleep training data (false positive).

Flagged HR samples are **excluded from the stager's input**: `bridge.sensor_history_series` (used to
build the learned stager's dense HR window — see `WHAT_THE_STAGER_IS_FOR.md`) drops any sample where
`hr_frozen` or `not_worn` is set, and reports the excluded count so the guard is observable rather
than silent. Actigraphy is left untouched even when the paired HR is flagged — movement stays valid
evidence while the device is worn/moving even if that tick's HR reading is bad. The flags are also
persisted per-sample (`sensor_samples.hr_frozen` / `.not_worn` / `.quality_reason`), so the excluded-
sample audit trail survives a restart and is queryable later, not just applied in the moment.

## Raw data persistence: why RR intervals are irreplaceable

Every ingested cardiac batch computes and stores a derived HRV scalar (RMSSD) into `sensor_samples`,
but that alone would foreclose future work: **every other HRV metric — SDNN, pNN50, Poincaré SD1/SD2,
LF/HF — is computable only from the raw beat-to-beat intervals, and none of them can be reconstructed
after the fact** once only the RMSSD summary is kept. Since the entire point of this sensing path is
a model personalized to *this* user, each night's raw series is irreplaceable training data — there
is no way to backfill it later if it turns out a different HRV feature would have mattered.

So every `/hr/ingest` batch also persists to two append-only tables (`dashboard/api/app/bridge.py`,
schema in `db.py`):

- **`rr_intervals`** — the raw beat-to-beat RR series, milliseconds, one row per ingest batch (a
  Verity streams roughly one HR sample every 2 s). Retained **400 days** (`_RR_RETENTION_DAYS`) —
  far longer than the `sensor_samples` rolling window — because this is exactly the "cannot be
  recovered later" data described above. The underlying SQLite file is already covered by the
  off-box encrypted backup, so this is durably preserved regardless of local retention; the retention
  constant just bounds the live table's size.
- **`actigraphy`** — PIM/ZCM/MAD/std/peak counts from the wearable's *own* accelerometer (Polar PMD
  ACC), computed with the same reduction as `scripts/reduce_motion_activity.py` uses to build the
  training set, so live counts are unit-comparable with training data — unlike the iPhone's unitless
  0–1 movement index, which stays in a separate column (`sensor_samples.movement`) rather than being
  conflated with it. Same 400-day retention, same rationale.

Both tables exist specifically because `PERSONALIZATION_FINDINGS.md`'s conclusion was that
*across-nights* personalization (using many of this user's own nights) is the one adaptation
mechanism that hasn't been ruled out — and that requires exactly this raw, un-collapsed history to
ever be tested, which is why it is being captured now, before there's a concrete plan to consume it.

## Actions taken / outstanding

- [x] Stay on BLE PMD; never enable SDK mode (it disables the HR/PPI algorithms we depend on).
  See "Implementation" above for the shipped codec/forwarder.
- [x] Data-quality guards for frozen-HR and not-worn (Polar documents both behaviours). See
  "Data-quality guards" above.
- [x] Persist raw RR intervals + wearable actigraphy for future across-nights personalization
  (400-day retention). See "Raw data persistence" above.
- [ ] Cross-check the PMD codec against the two community implementations.
- [ ] Tell the user: validate firmware, and be aware placement/skin factors affect resting accuracy.
- [ ] Optional: ANT+ redundant HR channel if BLE dropouts prove to be a real problem in practice.
