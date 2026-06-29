# Verifying the bed actually does what the buttons say (round-trip, live)

When the Pod is plugged in, this confirms that clicking a control on the dashboard produces the
**real action on the bed** — not just a command in the queue. It does that by reading the **Pod's
own reported state** back from the Eight Sleep cloud and comparing it to what was commanded.

## Why a round-trip is the only honest check

The architecture is decoupled on purpose: the UI enqueues a command → the daemon applies it to the
Pod via the Eight Sleep cloud. We already prove (in CI) that *the UI hits the right endpoint* and
*the daemon maps each command to the right device call*. The one thing only a plugged-in Pod can
confirm is that **the bed itself accepted and acted on it**. So the verifier compares:

The verifier exercises **every device-affecting control** and checks each against the Pod's own
readback where the cloud exposes one:

| Control (`--checks` name) | Device-reported signal it checks |
|---|---|
| **Set temperature** (`temp`) | `device_target_level` — the level the **Pod accepted** (vs the level the commanded °F maps to) + `bed_temp_f` moving. Tests 66 / 72 / 69 °F. |
| **Nudge temperature** (`nudge`) | `device_target_level` changes up and down |
| **Mode** auto/manual/view (`mode`) | `/status` mode + that **manual actually holds** the commanded level on the Pod |
| **Emergency Stop / power** (`power`) | side goes `OFF` (`state=OFF` / `power_on=False`, set only after a successful `turn_off_side`) and the Pod's level returns to 0; then power back on |
| **Away on/off** (`away`) | commanded (the cloud's away read-back is limited → reported COMMANDED, not DEVICE-CONFIRMED) |
| **Prime** (`prime`) | the Pod's `priming` flag flips on |
| **Smart wake** set/clear (`wake`) | the Pod's **own alarm slot** (`enabled` / `time`) changes; falls back to "daemon armed it" if the firmware doesn't surface alarms |
| **Sessions** induce / nap / end (`sessions`) | the session drives the Pod (warm→cool level move) + the session state |
| **Safe default** (`safe`) | power on + auto mode |
| **Hue dawn light** (`hue`) | the test flash returns ok (LAN bridge; only if Hue is configured) |

Each result is labelled **✅ DEVICE-CONFIRMED** (the bed's own readback matched), **🟡 COMMANDED**
(sent + accepted but the device didn't surface a confirming signal — e.g. away), or **❌ FAILED**.

> **Already validated end-to-end against the simulator** (21 checks, 0 failures), so the flow,
> polling, and confirmation logic are proven — only the *real device readings* are pending the Pod.
> Against the real Pod, `prime` should additionally become DEVICE-CONFIRMED (the simulator doesn't
> model priming).

## What you do when the Pod is plugged in

1. **Run the live daemon against the real Pod** on your host (Windows server / Pi / cloud VM):
   set your Eight Sleep creds + `SLEEPCTL_LIVE=1` and start the stack (see `deploy/LIVE_POD.md`).
   Do the first pass with the **bed empty** (it will heat/cool/prime).
2. **Run the verifier** — two ways:

   **a) Locally on the host** (simplest):
   ```bash
   python scripts/verify_live_pod.py --base http://localhost:8000 \
       --user admin --password "$DASHBOARD_PASSWORD"
   ```

   **b) Let me (Claude) run it remotely** — start the Cloudflare tunnel you used before
   (`cloudflared tunnel --url http://localhost:3000`) and send me the `https://….trycloudflare.com`
   URL + the dashboard password. I'll run:
   ```bash
   python scripts/verify_live_pod.py --base https://….trycloudflare.com --api-prefix /api \
       --user admin --password XXXX
   ```
   and report back a ✅/🟡/❌ table for every control.

3. Read the summary. Every temperature/power/prime row should be **✅ DEVICE-CONFIRMED**.

### Options & safety
- The verifier **changes the real bed** (temperature, prime, power), so run it with the bed empty.
  It restores **power-on + auto mode** at the end (`/control/safe-default`).
- Limit what it exercises: `--checks temp` (just temperature), or `temp,power`, etc.
- Add `--yes` to skip the confirmation prompt (e.g. when I run it for you).
- If it prints "daemon in SIMULATOR mode," the live daemon isn't connected to the Pod — the
  readback would be simulated, not the real bed.

## Watching it live in the dashboard

`/status` now also returns `device_level`, `device_target_level`, and `bed_presence` — the Pod's
own reported numbers — so you can watch the bed's *actual* level track the commanded one on the
Home screen while you press buttons, independent of the verifier.
