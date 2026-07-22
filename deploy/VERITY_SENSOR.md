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
| **movement** | iPhone (Sensor Logger) → `/bcg/ingest` | sub-second actigraphy / arousal precursor |

`bridge.read_fused_sensor` merges them **per field**, each gated by its own freshness:
- HR/HRV come from the Verity when it's fresh (it overrides the phone's best-effort
  ballistocardiogram HR); if the Verity disconnects, HR/HRV fall back to the phone.
- Movement always comes from the phone.
- A lone phone, a lone Verity, or **both together** all work — whichever is streaming contributes.

Together this gives the controller clean HR + HRV (for arousal / onset detection, your #1 priority:
staying asleep) plus movement (for actigraphy-style light/deep estimation) — fully independent of
Eight Sleep's cloud.

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
   from `deploy\.env` (already set for the iPhone). Nothing else to configure.

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
