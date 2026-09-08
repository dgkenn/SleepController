# Verity BLE bridge on a Raspberry Pi

**Why.** Every wearable outage this month was the Windows Bluetooth stack, not the band:

| date | what the log showed | root cause |
|---|---|---|
| 08-06 | "PMD service not present" minutes after streaming ACC + PPI | WinRT cached the band's GATT services from a previous mode |
| 08-26 | 25 samples at 19:00, nothing for ten hours, link still "connected" | a BLE link that stays open after notifications stop |
| 09-01 | 837 consecutive `BleakDeviceNotFoundError` | band held by another central; the Windows side could not clear the bond |
| 09-05 | 171 consecutive connect `TimeoutError`, band found on every scan | same -- phone bond; `bthserv` restarts (the last rung of the recovery ladder) did nothing |
| 09-07 | 16 h connected on the generic HR service, HR = 0, every batch rejected | fallback path taken because PMD was refused for the reason above |

A Linux BlueZ stack does not have the first problem at all (no per-device service cache), and
gives us the one tool that fixes the third and fourth: `bluetoothctl remove <addr>` genuinely
drops a stale bond. Running the bridge on its own small box also puts the radio **next to the
bed** instead of wherever the PC is, and takes the BLE stack out of the same process tree as
the controller, API, web build and self-updater.

**What moves, what stays.** Only the forwarder moves. It is the same `scripts/verity_forwarder.py`
-- unchanged -- POSTing to the same `/hr/ingest` on the Windows box over the LAN. The controller,
API, dashboard, publishers and watchdog stay where they are.

## Parts

* Raspberry Pi Zero 2 W (~$15) or any Pi 3/4/5 -- all have built-in BLE. A Zero 2 W is plenty.
* micro-SD card, power supply. No display needed.

## Install (10 minutes)

1. Flash Raspberry Pi OS Lite (64-bit) with the Imager; enable SSH and Wi-Fi in its settings.
2. On the Pi:
   ```bash
   git clone https://github.com/dgkenn/SleepController.git
   cd SleepController
   sudo INGEST_URL='http://<windows-box-ip>:8000/hr/ingest?token=<BCG_INGEST_TOKEN>' \
        VERITY_ADDRESS='24:AC:AC:16:96:1D' \
        bash deploy/pi-bridge/install.sh
   ```
   `BCG_INGEST_TOKEN` is the value in `deploy\.env` on the Windows box. The band's address is
   in `.run\verity.address` there, or run `python scripts/verity_forwarder.py --scan` on the Pi.
3. On the Windows box, set `SLEEPCTL_VERITY=0` in `deploy\.env` and restart the watchdog, so two
   forwarders do not fight over the band.
4. Watch it come up:
   ```bash
   journalctl -u verity-bridge -f
   ```
   You want `connected`, then `PMD: start PPI ok` and `PMD: start ACC @52Hz`. The dashboard's
   armband card and the "Armband connected" push confirm the same thing from the other end.

## What the installer sets up

| unit | job |
|---|---|
| `verity-forwarder.service` | the forwarder, `Restart=always`, logs to journald |
| `bt-reset.path` + `bt-reset.service` | when the forwarder drops `.run/bt-reset.request` (its last recovery rung), remove the band's bond, power-cycle the adapter, restart the forwarder -- rate-limited to once per 10 min |

## If the band still refuses to connect

The bond on the *band's* side is the usual reason. Order matters:

1. On the phone: Settings → Bluetooth → the Polar Sense → **Forget This Device**. Quit Polar Flow.
2. On the Pi: `bluetoothctl remove 24:AC:AC:16:96:1D`
3. Then hold the band's button until it re-advertises.

Unpair first, then power-cycle -- a band power-cycled before the phone forgets it re-bonds to the
phone the moment it wakes.

## Keeping the band ours

Do not re-pair the band to the phone. The Polar app is the one thing that has cost more nights
of data than any bug in this repository; on this system the band should have exactly one master,
and that is the bridge.
