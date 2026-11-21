# Apple Watch

Apple has no cloud API for HealthKit, so the Watch's data has to be sent out from the iPhone or the
Watch. There are two paths, and they do different jobs.

## 1. Live heart rate during the night (steers the bed)

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

Health Auto Export (iOS) has a REST API automation that POSTs its JSON export to a URL.

- URL: `https://<funnel host>/api/apple/health` (works away from home), or
  `http://192.168.1.163:3000/api/apple/health` on the home Wi-Fi.
- Format JSON. Metrics: Heart Rate, Heart Rate Variability, Respiratory Rate, Blood Oxygen,
  Sleeping Wrist Temperature, Resting Heart Rate, Sleep Analysis.
- Sleep: either setting works (aggregated nightly totals or per-stage segments); segments are
  what the stager can be scored against, so leave "aggregate sleep" off if offered.
- Interval: hourly is enough. Overlapping windows are de-duplicated.

Stored on the box only (`apple_health`, `apple_sleep` tables). The public health snapshot carries
counts and times (`apple_health` block), never values. `GET /api/apple/health/summary` shows the same.
