# Thermal & Water-Loop Debugging (Pod 2)

Hard-won field notes from a live debugging session where "heating/cooling feels weak / bed
temp is stuck." Read this first when thermal performance is off — it will save hours.

## TL;DR decision tree

1. **Is our telemetry even live?** If `bed_temp_f` / `device_level` are frozen at one value
   across many minutes → it's a **software/telemetry** problem, not the bed. Almost always the
   daemon is crash-looping or wedged. Check `/diag` daemon-crash tails; restart the daemon
   (`POST /diag/action/restart?target=daemon`). Historically caused by a UTF-8 log crash on an
   emoji — now fixed, but any tick exception can re-freeze telemetry.
2. **Does the Eight Sleep *app* show cooling/heating working while ours doesn't?** → our data
   path is stale/wedged (see #1), the bed itself is fine. Fix the daemon.
3. **Does even the app show weak thermal at max for 20+ min?** → likely **physical**:
   air-bound water loop (esp. after leak repairs / a low-water event). See "Air-bound loop".
4. **Is the setpoint being yanked back toward ~-68 every ~30s?** → **competing controllers**
   fighting ours. See "Three controllers".

## Direct cloud access — diagnose without the daemon

The single most useful technique: talk to the Eight Sleep cloud **directly** via pyEight,
bypassing our (possibly dead) daemon. This is exactly what `/diag/probe` does, but you can also
do it from any Python with the vendored pyEight on `PYTHONPATH`:

```python
import aiohttp
_o = aiohttp.ClientSession.__init__
aiohttp.ClientSession.__init__ = lambda self,*a,**k:(k.setdefault("trust_env",True),_o(self,*a,**k))[1]  # honor HTTPS_PROXY
from pyeight.eight import EightSleep
e = EightSleep(EMAIL, PASSWORD, "America/New_York")
await e.start(); await e.update_device_data(); await e.update_user_data()   # device BEFORE user (user_data reads device_data)
u = list(e.users.values())[0]
```

**Fields that actually matter** (from `e.device_data`, per side `left*`/`right*`):
- `priming` (bool), `needsPriming`, `hasWater`, `lastPrime` (ISO — advances only on a
  **completed** prime), `lastLowWater` (ISO).
- `leftHeatingLevel` = the **actual** plate level; `leftTargetHeatingLevel` = the **commanded**
  target. These differ — the device ramps the actual toward the target *slowly* (~1 level/min).
- `leftNowHeating` / `leftNowCooling` (mode flags).
- `leftKelvin` is **NOT a temperature** — it's the smart-schedule state:
  `{currentActivity:'schedule', currentTargetLevel:<int>, active:bool}`.
- `sensorInfo.connected`, `sensorInfo.model` (e.g. `Pod2.1`).

Commands: `await u.set_heating_level(level, duration)` (level -100..+100, clamps; turns the side
on). `set_heating_level` sends `currentLevel` **and** a `timeBased` level. Re-assert every ~60s
if a competing controller keeps overriding you. Space cloud calls >=60s and don't run multiple
sessions/rapid commands or Eight Sleep **rate-limits** you (RequestError -> fields read `None`).

## Objective thermal test (proves heat/cool work, room-temp-agnostic)

Bed empty. Command max cool, hold, watch the bed's **own** sensor move; then max heat. Judge by
**direction + magnitude of the bed sensor**, not any assumed room temp:
- Cooling works <=> `current_bed_temp` falls well below its side-*off* baseline (side off ~= room).
- Heating works <=> it climbs well above.
Measured baseline example: **side OFF ~= 27C; cooling -> 19-20C; heating -> 26C**. Both
directions flipped `nowCooling`/`nowHeating` correctly every time. Note the **actual level
ramps slowly** and the effective floor/ceiling is narrower than -100/+100 in a warm room and/or
when air-bound.

## Air-bound loop (the "weak cooling" culprit)

After leak repairs / a low-water event, air gets trapped in the hub<->cover water loop and
**kills heat transfer** — the plate can't get cold/hot no matter the command. Signatures:
- A **prime that never completes**: `priming` stays `True` for >~6 min and `lastPrime` never
  advances. (A healthy prime is ~2-4 min and updates `lastPrime`.)
- Strong cool/heat command but **little bed-temp AND little level movement** over ~15 min.
- Recent `lastLowWater`; possibly `needsPriming`.

**Fix is physical (no software path):** top off the reservoir with **distilled** water (air
enters when the pump sucks below the intake) -> **reseat both hub<->cover connectors**
(water-tight != air-tight after repairs) -> straighten hoses (no high loops; hub at/below
mattress height) -> **re-prime 2-4x** with gentle corner-burping. If air returns after a night,
a fitting is drawing air on the suction side -> re-inspect or contact Eight Sleep support.

## Three controllers fight over the setpoint

The bed can be driven by **three** actors at once, all pulling toward ~-68:
1. **Eight Sleep's native app schedule / Autopilot** (`leftKelvin.currentActivity=='schedule'`).
2. **Old/zombie instances of our daemon** (a crash-loop leaves several `run_daemon.py` running).
3. **Our current daemon.**
Symptom: you command max cool (-100), and the target snaps back to ~-68 within ~30s.
**Fix:** turn off the Eight Sleep app's schedule/Autopilot so our controller has sole control;
and deploy/restart so exactly one daemon runs (the watchdog's restart-storm limiter + the
single-instance deploy handle the zombies). To measure cleanly, pause our daemon
(`POST /control/pause`) and re-assert your command each cycle.

## Cloud HR / presence latency

`bed_presence` is derived from **recent heart-rate data**, and this pyEight path surfaces HR
mostly from **processed session data** — so it **lags** (minutes) and may not show a live HR for
a brief awake lie-down even though the sensor works (the Eight Sleep app's real-time view will
show it). Validate presence/HR with an actual sleep session, and expect the controller's
presence signal to be latency-tolerant, not instant. An empty bed correctly reads
`presence=False`, `HR/HRV/RR=None` — that is NOT a broken sensor.

## Alarms (silent thermal wake)

pyEight's `set_alarm_vibration`/`set_alarm_thermal` convenience wrappers **clobber each other's
settings** and the vibration-disable path can 400. Always set alarms via the **full
`set_alarm_direct(...)`** call with every field specified (this is what our adapter's
`set_thermal_alarm` does: `vibration_enabled=False`). pyEight can only **modify** an existing
alarm slot, not create one — keep one alarm on the device via the app.
