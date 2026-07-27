# Polar Verity Sense — dedicated heart-rate / HRV input

This is the physiology path that works **even when the Pod's own sleep tracking is unavailable**
(e.g. no active Eight Sleep membership, or the bed sensors aren't reporting). A **Polar Verity
Sense** armband streams heart rate + beat-to-beat RR intervals over standard Bluetooth LE; a small
forwarder on the always-on PC reads it and POSTs to the dashboard, which computes HRV (RMSSD) and
**fuses it with the iPhone's movement** into one signal the controller consumes.

Zero device risk: the Verity is a *separate* device. Nothing here ever touches or modifies the
Eight Sleep Pod.

## How the two sensors combine

| signal | source | role |
|---|---|---|
| **heart rate + HRV** | Polar Verity Sense → `/hr/ingest` | **authoritative** cardiac channel (optical HR + RR-interval HRV) |
| **movement** | iPhone (Sensor Logger) → `/bcg/ingest`, else the Verity's own accelerometer (PMD ACC) → `/hr/ingest` | sub-second actigraphy / arousal precursor |

`bridge.read_fused_sensor` merges them **per field**, each gated by its own freshness:
- HR/HRV come from the Verity when it's fresh (it overrides the phone's best-effort
  ballistocardiogram HR); if the Verity disconnects, HR/HRV fall back to the phone.
- Movement prefers the phone's 0..1 index — every movement threshold in the controller was
  calibrated against it — and falls back to the **Verity's own accelerometer** when the phone is
  absent or stale, so a Verity-only night keeps its motion channel. That matters because motion
  feeds onset confirmation, arousal scoring, awakening detection and wake risk: without the
  fallback, going phone-less silently disables the machinery that protects sleep *maintenance*.
- A lone phone, a lone Verity, or **both together** all work — whichever is streaming contributes.
- `runtime_state` reports `hr_source` and `movement_source` so you can see which channel supplied
  each field.

**On units.** The phone reports a unitless 0..1 movement index; the Verity's accelerometer reduces
to PIM counts (same definition as the model's training data, kept in native units so live data
stays comparable with it). `bridge.actigraphy_movement_index` maps counts onto the index using the
two anchors the data-quality guards already define — PIM 1.0 "essentially motionless" → 0.06 (under
the 0.15 onset-stillness line) and PIM 5.0 "clearly moving" → 0.30 (exactly the wake-risk line),
saturating at 1.0. The raw counts are still stored separately and are what the stager consumes.

Together this gives the controller clean HR + HRV (for arousal / onset detection, your #1 priority:
staying asleep) plus movement (for actigraphy-style light/deep estimation) — fully independent of
Eight Sleep's cloud.

## How it steers the controller (no Pod staging needed)

The Pod normally supplies the sleep *stage* the controller keys off. With the membership inactive
that stage is missing, so the controller **derives the stage from the Verity's HR** and overlays it
onto the frame at one point — so onset detection, the state machine, arousal detection, wake-risk
pre-emption and in-night steering **all engage off the wearable alone**. This is what lets the
Verity actually *drive* the bed — detect that you've fallen asleep, hold you there, and pre-empt
awakenings — not just display a heart rate. A real Pod stage (if it ever returns) always wins; the
dashboard/`runtime_state` reports `stage_source` (`model` / `heuristic` / `sensor`) so you can see
what's steering.

Two estimators, in priority order:
1. **Learned model** (`sleepctl/ml/sleep_staging/`) — logistic models trained on the open PhysioNet
   **sleep-accel** dataset (Walch et al.: wrist HR → PSG stages). Pure-stdlib at runtime (no numpy).
2. **Heuristic fallback** (`state_estimator.py`) — interpretable HR/HRV/movement rules, used if the
   model weights are absent or there isn't enough HR history yet.

**Honest accuracy (leave-subjects-out CV, HR-only):** wake/sleep κ≈0.31, 4-class κ≈0.33 — deep-sleep
recall ≈0.73 is the strongest, wake/REM are weaker. Staging from a wrist HR feed is genuinely coarse,
so the estimate carries a **capped, sub-Pod confidence** and the controller's own multi-signal
wake/arousal detectors (HR spike + iPhone movement) remain the primary awakening signal — the model
supplies *staging + trajectory*, not the final say on wake. Retrain any time with
`python scripts/fetch_sleep_accel.py` then `python -m sleepctl.ml.sleep_staging.train`.
Tunable via `use_learned_stager` / `estimate_stage_from_vitals` in config.

## One-time setup

1. **Charge & wear.** Put the Verity Sense on your upper forearm/bicep. Single-press the button so
   it enters **heart-rate broadcast mode** (the LED indicates the Bluetooth/HR mode). Battery is
   ~20 h in this mode, so charge it during the day.
2. **Pair to the PC.** On the always-on Windows box, make the sensor available to the OS Bluetooth
   stack (Settings → Bluetooth → Add device), or just let the forwarder auto-discover it.
3. **Install the BLE library** (one-time), into the same venv the daemon uses:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install bleak
   ```
4. **Token.** The forwarder authenticates exactly like the phone: it reads `BCG_INGEST_TOKEN`
   from `deploy\.env`. If neither that nor `BCG_INGEST_OPEN=1` is set, the armband connects and
   streams while every POST is rejected `401` — so `verity-setup.ps1` mints a token when one is
   missing rather than leaving that failure to be discovered at 2 a.m.

The one-command path does all four steps, then **waits for real samples** and tells you whether
the feed actually came up:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verity-setup.ps1
```

## Run the forwarder

By hand (foreground, to confirm it works):
```powershell
.\.venv\Scripts\python.exe scripts\verity_forwarder.py
```
You should see `connected; subscribing to HR notifications; forwarding to http://localhost:8000/hr/ingest?...`.
Pin a specific device if auto-discovery picks the wrong one:
```powershell
.\.venv\Scripts\python.exe scripts\verity_forwarder.py --address AA:BB:CC:DD:EE:FF
```

Run it unattended via a Scheduled Task (survives logout/reboot, auto-reconnects on its own):
```powershell
$py  = "$HOME\SleepController\.venv\Scripts\python.exe"
$arg = "$HOME\SleepController\scripts\verity_forwarder.py"
schtasks /Create /TN "SleepController Verity" /SC ONLOGON /RL HIGHEST /F `
  /TR "`"$py`" `"$arg`""
schtasks /Run /TN "SleepController Verity"
```

## Confirm it's flowing

- Dashboard **Admin / Data health** shows a **Cardiac sensor (Verity)** row that flips to
  *streaming* with a fresh age once samples arrive.
- The health snapshot's `cardiac_sensor` check goes to **ok**.
- `POST /hr/ingest` returns `{"ok": true, "hr": .., "hrv": .., "rr_count": ..}`.

## Notes

- **Verity vs Polar H10.** The forwarder speaks the standard BLE Heart Rate Service (0x180D), so a
  Polar **H10** chest strap works too (`--source h10`). The H10 gives better beat-to-beat HRV and a
  ~year-long coin-cell (no nightly charging); the Verity is the more sleep-tolerable armband. Either
  feeds the same path.
- **Range.** BLE is ~10 m line-of-sight; keep the PC's Bluetooth adapter within range of the bed.
- **RR units.** Polar RR intervals arrive in 1/1024-second units; the forwarder converts to
  milliseconds before POSTing, and the API computes HRV = RMSSD over them.
