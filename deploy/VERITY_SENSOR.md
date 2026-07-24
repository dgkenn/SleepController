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

## Easiest: one command

1. **Charge & wear.** Put the Verity Sense on your upper forearm/bicep, single-press the button so
   it enters **heart-rate broadcast mode** (blue LED). Battery is ~20 h in this mode.
2. **Run the setup script** from the SleepController folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\verity-setup.ps1
   ```
   It installs the BLE library, scans to confirm your sensor is visible, sets `SLEEPCTL_VERITY=1`,
   and restarts the watchdog so the forwarder launches. That's it — within a minute or two the
   dashboard's **Cardiac Sensor (Verity)** card shows a live HR, and HR/HRV start steering the
   controller (fused with the phone's movement).

Everything below is the manual equivalent / reference if you'd rather do it by hand.

## Manual setup

1. **Pair to the PC.** Make the sensor available to the OS Bluetooth stack (Settings → Bluetooth →
   Add device), or just let the forwarder auto-discover it.
2. **Install the BLE library** into the same venv the daemon uses:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install bleak
   ```
   (The watchdog also auto-installs this the first time `SLEEPCTL_VERITY=1` is set, so you can skip
   it.)
3. **Token.** The forwarder authenticates exactly like the phone: it reads `BCG_INGEST_TOKEN`
   from `deploy\.env` (already set for the iPhone). Nothing else to configure.

## Run the forwarder

**Preferred — let the watchdog run it.** Set `SLEEPCTL_VERITY=1` in `deploy\.env` and restart the
watchdog (or wait for its next auto-update). The watchdog then launches `verity_forwarder.py`,
keeps it alive (relaunches if it dies), and — deliberately — treats it as *non-critical*, so a
missing sensor or Bluetooth hiccup can never make the box report unhealthy. Its log is
`.run\verity.log`.

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
