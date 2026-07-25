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

## Actions taken / outstanding

- [x] Stay on BLE PMD; never enable SDK mode (it disables the HR/PPI algorithms we depend on).
- [x] Data-quality guards for frozen-HR and not-worn (Polar documents both behaviours).
- [ ] Cross-check the PMD codec against the two community implementations.
- [ ] Tell the user: validate firmware, and be aware placement/skin factors affect resting accuracy.
- [ ] Optional: ANT+ redundant HR channel if BLE dropouts prove to be a real problem in practice.
