# Apple Watch

Apple has no cloud API for HealthKit, so the Watch's data has to be sent out from the iPhone or the
Watch. There are two paths, and they do different jobs.

## 1. Live heart rate during the night (steers the bed)

### Preferred: over Wi-Fi with Health Data Server (HDS)

The HDS watch app sends each heart-rate reading over the network as `PUT /` with
`{"data": "heartRate:72"}` (the protocol of Rexios80/hds_desktop). `dashboard/web/middleware.ts`
hands a PUT to the dashboard root to the API's `/hds` route, which ingests it like the band's
heart rate (source `apple_watch`).

- In the HDS watch app's settings: turn **HDS Cloud off**, and set the IP address to
  `192.168.1.163:3000` (the dashboard's address).
- Start it at bedtime. The Watch and the box must be on the same home network.
- No Bluetooth range limit and none of the box's Bluetooth trouble. Battery cost is the same as
  any live-heart-rate app (it runs a workout session).

### Alternative: over Bluetooth

A heart-rate broadcaster app makes the Watch advertise its heart rate as a standard Bluetooth
heart-rate strap (service 0x180D). `scripts/verity_forwarder.py` already reads that service; it
now also looks for broadcaster names (HeartBLE, Echo, "Apple Watch", HRM) after the Polar names,
and falls back to any device advertising 0x180D. A worn Polar band still wins.

- Apps: HeartBLE (on the Watch, watchOS 26+), Echo, or HRM (broadcasts from the iPhone).
- Start the broadcast at bedtime. The Watch must be within Bluetooth range of the box.
- Cost: these run a workout session all night. Developers report 40% (Series 9) to 100%
  (Series 7) battery over 6-7 hours, so charge the Watch before bed.
- Heart rate only: no beat-to-beat intervals unless the app sends them, and no accelerometer.
  The stager runs in HR-only mode, which is the mode its Apple Watch training data supports.

## 2. Full night after waking (Apple's stages, HRV, respiration)

### Free: Apple Shortcuts

`POST /api/apple/shortcut` takes what Shortcuts' "Find Health Samples" can send: JSON fields
`kind` ("sleep" or "heart_rate"), `start`, `end`, `value`, `source`, each a list or a list dropped
into a text field (newline-joined). Dates may be ISO 8601 or the phone's display format
("Nov 21, 2026 at 3:14 AM"); without an offset they are the box's local time. The response says
how many samples were stored and, if none, the first value it could not read.

Shortcut: Find Health Samples (Sleep Analysis, start date in the last 3 days) -> Get Contents of
URL (POST, JSON: kind=sleep, start/end/value/source = the samples' Start Date / End Date / Value /
Source). Repeat with Heart Rate (kind=heart_rate, start + value). The same works for
Heart Rate Variability (kind=hrv), Respiratory Rate (respiratory_rate), Blood Oxygen
(blood_oxygen), Resting Heart Rate (resting_heart_rate) and Sleeping Wrist Temperature
(wrist_temperature). Run it from a daily time-of-day automation; overlapping days are
de-duplicated. These are written by the Watch after the fact, so they feed the morning
outcomes and the recovery score (breathing rate included), not live steering.

### Paid: Health Auto Export

Health Auto Export (iOS) has a REST API automation that POSTs its JSON export to a URL.

- URL: `https://<funnel host>/api/apple/health` (works away from home), or
  `http://192.168.1.163:3000/api/apple/health` on the home Wi-Fi.
- Format JSON. Metrics: Heart Rate, Heart Rate Variability, Respiratory Rate, Blood Oxygen,
  Sleeping Wrist Temperature, Resting Heart Rate, Sleep Analysis.
- Sleep: either setting works (aggregated nightly totals or per-stage segments); segments are
  what the stager can be scored against, so leave "aggregate sleep" off if offered.
- Interval: hourly is enough. Overlapping windows are de-duplicated.

Stored on the box only (`apple_health`, `health_sleep` tables; sleep segments keep the app that
wrote them, so Apple's and Bevel's stay apart). The public health snapshot carries
counts and times (`apple_health` block), never values. `GET /api/apple/health/summary` shows the same.

## 3. Three-way comparison and personal calibration

After each upload that brings new sleep stages, `sleepctl/learning/wearable_consensus.py` runs:

1. Lines up three hypnograms on 30 s epochs: our live stager (`raw_samples.stage`), Apple's
   stages, and Bevel's (the Health samples whose source is Bevel -- only if Bevel writes its
   stages into Apple Health; check Health > Sleep > Data Sources & Access).
2. Settles disagreements with a Dawid-Skene model: each source has a learned confusion matrix
   (how it labels each true stage), fitted with EM across all nights from a "mostly right"
   prior (Apple strongest). The matrices are the fingerprint of each algorithm's habits.
3. Labels each epoch from **Apple + Bevel only** (our own vote never sets the label it is
   corrected against). An epoch with reference posterior below 0.7 abstains.
4. Refits the personal stager calibration (`learning/eeg_calibration.py`) against those labels.
   It switches on only if it beats the unadjusted stager on held-out nights (leave-one-night-out
   plus a bootstrap). EEG-headband nights, if ever imported, are ground truth and win.

`GET /api/staging/consensus` returns the full report (per-night and pooled kappas, confusion
matrices). The health snapshot shows pooled agreement and the calibration state only.

## 4. Watch-only nights, independent grading, and the standing experiment program

- **Watch-only control.** Heart rate from the Watch (`source=apple_watch`) feeds the same fusion,
  stager, bed-entry and arousal paths as the band. The data-quality score no longer docks the
  Watch for HRV and breathing rate it can never send (`data_quality.SOURCE_VITALS`); a sensor
  that can measure them and stops still counts as missing.
- **Independent outcomes** (`learning/recovery.py`). From Apple's hypnogram and Apple Health:
  deep / REM / core minutes, total sleep, efficiency, WASO, awakenings, lowest sustained heart
  rate, mean HRV, resting heart rate, and a **recovery score** (50 = your usual; robust z-scores
  against your previous nights, half physiology, half architecture).
- **Nudges graded by Apple.** Each deepen / REM-warm maneuver is re-graded with whether Apple
  saw the target stage in its response window. The steer learners use that grade when present,
  because the controller's own stager reads a cooler bed's lower heart rate as deeper sleep.
- **Experiments measured by Apple** when Apple covers every night in the comparison.
- **Standing program** (`learning/auto_experiments.py`). Once 7 nights have a recovery score, it
  keeps one "Auto:" crossover running: cooler in deep sleep, cooler baseline, warmer in REM,
  warmer baseline. 3 nights per arm, 1 washout night, judged on the recovery score. A treatment
  whose 95% interval clears zero after 2+ cycles is written into the learned setpoints; no
  difference after 4 cycles keeps the setting. A hand-started experiment always takes priority.
  `SLEEPCTL_AUTO_EXPERIMENTS=0` turns it off.
