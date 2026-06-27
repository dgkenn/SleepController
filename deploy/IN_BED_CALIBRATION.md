# In-bed calibration & validation battery (Pod 2)

A push-button battery you run **once, the moment the repaired mattress is reconnected**,
to (a) confirm the repair holds water under sustained pumping and (b) capture the real
in-bed thermal calibration the earlier bucket rig could not.

Script: [`scripts/in_bed_calibration.py`](../scripts/in_bed_calibration.py)

It is standalone, watchdog-protected, and **always powers your side OFF at the end** —
even on error, water-drain, or Ctrl-C.

---

## Why this exists (lessons from the live testing)

* The trustworthy thermal signal is the Hub's water-side **`currentDeviceLevel`**
  (`frame.device_level`), **not** the cover-side `bed_temp_f` — in a hot room the cover
  temp tracked ambient air and even *rose* while the bed was commanded to MAX COOL. Pure
  artifact. This battery reads `device_level` and ignores `bed_temp_f`.
* In the bucket, the reservoir kept **draining**. A correctly sealed *in-bed* loop should
  hold water. The battery asserts `hasWater` stays True throughout, and aborts to SAFE-OFF
  the instant it goes False.
* Bucket ramp numbers do **not** transfer to control. With the real mattress thermal mass
  back in the loop, the measured levels/min and time-to-plateau are the ones the controller
  should actually use.

---

## Prerequisites

1. **Mattress reconnected, filled with water, and primed.** In the Eight Sleep app, run a
   prime and wait for it to finish. The Pod must show **online** and **not** mid-prime.
2. **pyEight available.** From the repo root:
   ```bash
   pip install -e ".[eightsleep]"
   ```
   (If your environment vendors pyEight another way, that's fine — the import just has to
   resolve, exactly as for the live daemon.)
3. **Credentials in the environment only** (never hard-coded, password never printed):
   ```bash
   export EIGHTSLEEP_EMAIL="you@example.com"
   export EIGHTSLEEP_PASSWORD="********"
   export EIGHTSLEEP_SIDE="left"            # left | right  (default left)
   export EIGHTSLEEP_TIMEZONE="America/New_York"   # optional, default UTC
   # export EIGHTSLEEP_CLIENT_ID="..."      # only if plain login fails
   # export EIGHTSLEEP_CLIENT_SECRET="..."
   ```

> The script only briefly commands cool/heat levels and then OFFs the side. It does **not**
> touch alarms, away mode, the controller, learning, or storage.

---

## How to run

```bash
cd /home/user/SleepController
python scripts/in_bed_calibration.py
```

Runtime is roughly **15–20 minutes** (a 5-minute sealed-loop phase plus two ramps capped
at 6 minutes each). For a faster smoke test that skips the long circulation phase:

```bash
python scripts/in_bed_calibration.py --quick
```

You can `Ctrl-C` at any time; the SAFE-OFF still runs.

Exit code is `0` only if every check passed and the run completed; non-zero on any FAIL,
abort, or interrupt.

---

## What each check means

| Check | What it does | PASS means |
|---|---|---|
| **device_status** | Reads `device_status()` (online / hasWater / priming / needsPriming). Gates the rest. | Pod is online, watered, and not mid-prime. |
| **f_level_mapping** | Cross-checks the controller's °F↔level table (`fahrenheit_to_level` / `level_to_fahrenheit`) and confirms the device echoes the commanded level back as `target_heating_level`. | Table is monotonic and the device target matches the commanded level (±5; a missing echo is informational, not a fail). |
| **sealed_loop_circulation** | Pumps at ~60 °F for 5 minutes, sampling `hasWater` the whole time. | `hasWater` was True on **every** sample — the repaired loop is sealed and does not drain under sustained circulation. |
| **telemetry_cadence** *(info)* | Watches how often `device_level` actually changes during circulation. | Informational: reports the median refresh interval so you know the real telemetry cadence (the controller can't react faster than this). |
| **thermal_ramp_calibration** | Drives MAX COOL then MAX HEAT, sampling `device_level`; computes levels/min and approximate time-to-plateau in both directions. | The bed actually moved — cooling drove the level meaningfully negative and heating positive (≥1 level/min each). These are the in-bed rates that feed control. |
| **thermal_health** | Summarizes the ramp samples the way the live thermal-health monitor does (device-level responded to the command). | `ok` — the device level tracked both commands (not stalled flat). |
| **safe_off** | Powers the side OFF and confirms `nowHeating == False`, retrying up to 8×. | Side confirmed OFF. If this FAILS, **turn the side off in the Eight Sleep app.** |

---

## How to read the results

At the end the script prints a `RESULTS` block, one line per check:

```
  [PASS] device_status: online + watered + not priming
  [PASS] f_level_mapping: table monotonic=True, device echo target=-92 vs -92 (tol +/-5)
  [PASS] sealed_loop_circulation: hasWater True 60/60 samples over 5 min
  [INFO] telemetry_cadence: device_level changed 9 times; median refresh interval ~30s ...
  [PASS] thermal_ramp_calibration: cool ~-6.2 levels/min (plateau 280s), heat ~+5.1 ...
  [PASS] thermal_health: state=ok; cool moved -10->-100, heat moved -100->+100
  [PASS] safe_off: nowHeating=False after ... retries
```

* **Record the `thermal_ramp_calibration` line.** The cool/heat **levels/min** and
  **time-to-plateau** are the real in-bed numbers; feed them to the controller's response-lag
  / ramp expectations (they replace the bucket figures).
* **Note the `telemetry_cadence`** median refresh interval — that's how fresh `device_level`
  truly is, and it bounds how fast the loop can sense a change.

### If something FAILs

* **device_status FAIL** → fix what it names (offline, no water, mid-prime / needs-prime),
  then re-run. The active pumping checks are skipped when this gate fails.
* **sealed_loop_circulation FAIL / WATCHDOG ABORT** → `hasWater` dropped under circulation:
  the loop is still leaking or under-filled. Re-prime / inspect the repair before trusting it.
* **thermal_ramp_calibration or thermal_health FAIL** → `device_level` stayed flat despite the
  command (stalled): re-check water level, cover seating, and hardware. (This is exactly the
  failure mode the cover-side `bed_temp_f` hid.)
* **safe_off FAIL** → the side may still be powered: **turn it off in the Eight Sleep app.**
