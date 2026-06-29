# Verifying the bed actually does what the buttons say (round-trip, live)

When the Pod is plugged in, this confirms that clicking a control on the dashboard produces the
**real action on the bed** — not just a command in the queue. It does that by reading the **Pod's
own reported state** back from the Eight Sleep cloud and comparing it to what was commanded.

## Why a round-trip is the only honest check

The architecture is decoupled on purpose: the UI enqueues a command → the daemon applies it to the
Pod via the Eight Sleep cloud. We already prove (in CI) that *the UI hits the right endpoint* and
*the daemon maps each command to the right device call*. The one thing only a plugged-in Pod can
confirm is that **the bed itself accepted and acted on it**. So the verifier compares:

| Control | Device-reported signal it checks |
|---|---|
| **Set temperature** | `device_target_level` — the level the **Pod accepted**, read back from the cloud (compared to the level the commanded °F maps to) + the measured `bed_temp_f` moving |
| **Nudge temperature** | `device_target_level` changes from its prior value |
| **Emergency Stop / power-off** | state goes `OFF` / `device_level` drops toward 0 / bed temp drifts to ambient |
| **Power on** | side reports active again |
| **Prime** | the Pod's `priming` flag flips on |
| **Away on/off** | commanded (the cloud's away read-back is limited, so this is reported as COMMANDED, not DEVICE-CONFIRMED) |

Each result is labelled **✅ DEVICE-CONFIRMED** (the bed's own readback matched), **🟡 COMMANDED**
(sent + accepted but the device didn't surface a confirming signal), or **❌ FAILED**.

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
