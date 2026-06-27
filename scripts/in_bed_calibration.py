#!/usr/bin/env python3
"""Push-button IN-BED calibration & validation battery for the Eight Sleep Pod 2.

RUN THIS the moment you reconnect the repaired mattress (a few days out), once the
Pod is filled with water, primed, and online. It is fully self-contained and SAFE:
it watchdogs ``hasWater`` and ALWAYS powers the side OFF at the end.

What it validates
-----------------
This is the real in-bed calibration the earlier bucket rig could not do. Lessons
baked in from the recent live testing:

  * The trustworthy thermal signal is the Hub's water-side ``currentDeviceLevel``
    (``frame.device_level`` from ``read_frame()``), NOT the cover-side ``bed_temp_f``
    (an ambient artifact that tracked room air even while commanded to MAX COOL).
  * A ``hasWater`` watchdog aborts on any drain — in the bucket the reservoir kept
    draining; a correctly SEALED in-bed loop should hold water the whole run.
  * The run ALWAYS ends powering the side OFF and confirming ``leftNowHeating`` /
    ``rightNowHeating`` is False (robust retry loop).

The battery (PASS/FAIL per check):
  1. SEALED-LOOP CIRCULATION VALIDATION — sustained pumping at a cool setpoint while
     asserting ``hasWater`` stays True the whole time.
  2. IN-BED THERMAL TIME-CONSTANT + RAMP-RATE CALIBRATION — command cool then heat,
     measure ``device_level`` ramp rate (levels/min) and approximate time-to-plateau
     against the REAL in-bed thermal mass (these transfer to control; bucket numbers
     did not).
  3. °F<->level sanity (cross-check ``thermal.to_level`` / the device target) and a
     telemetry-cadence check (how often ``device_level`` actually refreshes).
  4. ``thermal_health`` + ``device_status`` sanity (online, hasWater, priming).

Credentials come ONLY from the environment — nothing is hard-coded and the password
is never printed:

    EIGHTSLEEP_EMAIL       (required)
    EIGHTSLEEP_PASSWORD    (required)
    EIGHTSLEEP_TIMEZONE    (optional, default UTC)
    EIGHTSLEEP_SIDE        (optional, default left; left|right)
    EIGHTSLEEP_CLIENT_ID   (optional; only if plain login fails)
    EIGHTSLEEP_CLIENT_SECRET (optional)

Usage:
    cd /home/user/SleepController
    pip install -e ".[eightsleep]"      # if pyEight not already vendored
    EIGHTSLEEP_EMAIL=... EIGHTSLEEP_PASSWORD=... python scripts/in_bed_calibration.py

It only ever commands COOL/HEAT levels briefly and then OFFs the side; it does not
touch alarms, away mode, learning, storage, or the controller. Ctrl-C is honored and
still runs the SAFE-OFF.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# This script is intentionally self-contained. Its ONLY repo dependencies are the
# proven cloud adapter and the pure °F<->level lookup — nothing from config/models/
# controller/learning/storage/dashboard/cli.
from sleepctl.adapters.eightsleep_cloud import EightSleepClient
from sleepctl.controller.calibration import (
    fahrenheit_to_level,
    level_to_fahrenheit,
)

# ---------------------------------------------------------------------------------
# Tunables (conservative; all brief, all reversed by the final SAFE-OFF).
# ---------------------------------------------------------------------------------
COOL_LEVEL = -100          # MAX cool — the clearest ramp signal on the device level scale
HEAT_LEVEL = 100           # MAX heat
COOL_SETPOINT_F = 60.0     # used for the °F<->level cross-check & circulation phase
POLL_INTERVAL_S = 5.0      # how often we sample read_frame()/device_status()
CIRCULATION_MINUTES = 5.0  # sustained pumping window for the sealed-loop test
RAMP_MAX_MINUTES = 6.0     # cap on how long we watch a single ramp for plateau
PLATEAU_EPS_LEVELS = 2     # |delta| below this over the plateau window => settled
PLATEAU_WINDOW_S = 60.0    # window over which we judge "no longer moving"
SETTLE_OFF_RETRIES = 8     # robustness of the final power-OFF confirmation
SETTLE_OFF_WAIT_S = 4.0


# ---------------------------------------------------------------------------------
# Logging helpers (stdout, timestamped, never prints secrets).
# ---------------------------------------------------------------------------------
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def banner(msg: str) -> None:
    print("\n" + "=" * 78, flush=True)
    print(f"  {msg}", flush=True)
    print("=" * 78, flush=True)


@dataclass
class CheckResult:
    name: str
    passed: Optional[bool]            # True/False, or None for informational
    detail: str = ""
    metrics: dict = field(default_factory=dict)


class WaterDrained(Exception):
    """Raised by the watchdog when ``hasWater`` goes False mid-run -> abort to SAFE-OFF."""


# ---------------------------------------------------------------------------------
# Credentials strictly from env (password never printed).
# ---------------------------------------------------------------------------------
def load_client() -> EightSleepClient:
    email = os.environ.get("EIGHTSLEEP_EMAIL")
    password = os.environ.get("EIGHTSLEEP_PASSWORD")
    if not email or not password:
        log("FATAL: set EIGHTSLEEP_EMAIL and EIGHTSLEEP_PASSWORD in the environment.")
        sys.exit(2)
    timezone = os.environ.get("EIGHTSLEEP_TIMEZONE", "UTC")
    side = os.environ.get("EIGHTSLEEP_SIDE", "left").strip().lower()
    if side not in ("left", "right"):
        log(f"FATAL: EIGHTSLEEP_SIDE must be left|right, got {side!r}.")
        sys.exit(2)
    client_id = os.environ.get("EIGHTSLEEP_CLIENT_ID") or None
    client_secret = os.environ.get("EIGHTSLEEP_CLIENT_SECRET") or None
    log(f"Connecting to Eight Sleep as {email} (side={side}, tz={timezone}).")
    return EightSleepClient(
        email=email,
        password=password,
        timezone=timezone,
        side=side,
        client_id=client_id,
        client_secret=client_secret,
    )


# ---------------------------------------------------------------------------------
# Low-level device helpers built on the proven adapter API.
# ---------------------------------------------------------------------------------
def read_has_water(client: EightSleepClient) -> Optional[bool]:
    """The drain watchdog signal, read straight from device_data per the methodology."""
    try:
        return client._eight.device_data.get("hasWater")
    except Exception:
        return None


def read_now_heating(client: EightSleepClient) -> Optional[bool]:
    """`leftNowHeating` / `rightNowHeating` — True while the side is actively powered."""
    key = "leftNowHeating" if client.side == "left" else "rightNowHeating"
    try:
        return client._eight.device_data.get(key)
    except Exception:
        return None


async def refresh(client: EightSleepClient) -> None:
    """Pull fresh device + user data; tolerate a transient cloud miss."""
    try:
        await client.update(user=True, device=True)
    except Exception as exc:  # a single bad poll should not kill the battery
        log(f"  (warning) update() failed transiently: {exc!r}")


def assert_water(client: EightSleepClient) -> None:
    """Watchdog: abort the whole battery the instant the loop loses water."""
    hw = read_has_water(client)
    if hw is False:
        raise WaterDrained("hasWater went False — loop is draining; aborting to SAFE-OFF.")


async def sample_device_level(client: EightSleepClient) -> tuple[datetime, Optional[int]]:
    """One fresh (timestamp, device_level) sample (the water-side truth)."""
    await refresh(client)
    assert_water(client)
    frame = client.read_frame()
    lvl = frame.device_level
    return datetime.now(), (int(lvl) if lvl is not None else None)


# ---------------------------------------------------------------------------------
# CHECK 4 (run first as a gate): device_status + thermal_health sanity.
# ---------------------------------------------------------------------------------
async def check_device_status(client: EightSleepClient) -> CheckResult:
    banner("CHECK: device_status sanity (online / hasWater / priming)")
    await refresh(client)
    status = client.device_status()
    log(f"  device_status = {status}")
    online = status.get("online")
    has_water = status.get("has_water")
    priming = status.get("priming")
    needs_priming = status.get("needs_priming")
    temp_available = status.get("temp_available")

    problems = []
    if online is False:
        problems.append("device reports OFFLINE")
    if has_water is False:
        problems.append("hasWater is False (reservoir/loop empty — prime the Pod)")
    if needs_priming is True:
        problems.append("needsPriming is True (prime the Pod before calibrating)")
    if priming is True:
        problems.append("currently PRIMING — wait for it to finish, then re-run")

    passed = len(problems) == 0
    detail = "online + watered + not priming" if passed else "; ".join(problems)
    log(f"  -> {'PASS' if passed else 'FAIL'}: {detail}")
    return CheckResult(
        "device_status",
        passed,
        detail,
        metrics={
            "online": online,
            "has_water": has_water,
            "priming": priming,
            "needs_priming": needs_priming,
            "temp_available": temp_available,
        },
    )


# ---------------------------------------------------------------------------------
# CHECK 3a: °F<->level sanity (cross-check the device target against thermal mapping).
# ---------------------------------------------------------------------------------
async def check_f_level_mapping(client: EightSleepClient) -> CheckResult:
    banner("CHECK: °F <-> level mapping sanity (vs device target)")
    # Pure-math reference points from the same lookup the controller uses (to_level).
    reference = [60.0, 66.0, 70.0, 74.0, 81.0]
    log("  reference °F -> level (controller's calibration table):")
    for f in reference:
        lvl = fahrenheit_to_level(f)
        back = level_to_fahrenheit(lvl)
        log(f"    {f:5.1f} °F -> level {lvl:+4d} -> {back:5.1f} °F")

    # Commanding COOL_SETPOINT_F should land the device target near the table's level.
    expected_level = fahrenheit_to_level(COOL_SETPOINT_F)
    log(f"  commanding setpoint {COOL_SETPOINT_F:.1f} °F (expected level {expected_level:+d}) "
        f"to confirm the device accepts & reflects it as target_heating_level ...")
    await client.turn_on_side()
    await client.set_heating_level(expected_level, duration_s=0)
    await asyncio.sleep(POLL_INTERVAL_S)
    await refresh(client)
    assert_water(client)
    frame = client.read_frame()
    target = frame.target_level
    log(f"  device target_heating_level = {target} (commanded {expected_level:+d})")

    # Round-trip monotonicity is the load-bearing invariant; the device echo is a bonus
    # (some firmwares report target lazily, so a None echo is informational, not a fail).
    mono_ok = (
        fahrenheit_to_level(60.0) < fahrenheit_to_level(70.0) < fahrenheit_to_level(81.0)
    )
    echo_ok = (target is None) or (abs(int(target) - expected_level) <= 5)
    passed = mono_ok and echo_ok
    detail = (
        f"table monotonic={mono_ok}, device echo target={target} vs {expected_level:+d} "
        f"(tol +/-5){' [echo not reported]' if target is None else ''}"
    )
    log(f"  -> {'PASS' if passed else 'FAIL'}: {detail}")
    return CheckResult(
        "f_level_mapping", passed, detail,
        metrics={"expected_level": expected_level, "device_target": target},
    )


# ---------------------------------------------------------------------------------
# CHECK 1: sealed-loop circulation validation (sustained pumping, hasWater holds).
# CHECK 3b: telemetry cadence (how often device_level refreshes) — measured here for free.
# ---------------------------------------------------------------------------------
async def check_sealed_loop(client: EightSleepClient) -> tuple[CheckResult, CheckResult]:
    banner(f"CHECK: sealed-loop circulation — pump at {COOL_SETPOINT_F:.0f} °F for "
           f"{CIRCULATION_MINUTES:.0f} min, assert hasWater stays True")
    cool_level = fahrenheit_to_level(COOL_SETPOINT_F)
    await client.turn_on_side()
    await client.set_heating_level(cool_level, duration_s=0)
    log(f"  commanded cool level {cool_level:+d}; circulating ...")

    deadline = time.monotonic() + CIRCULATION_MINUTES * 60.0
    water_samples = 0
    water_true = 0
    # For the telemetry-cadence check: record timestamps where device_level CHANGES.
    last_level: Optional[int] = None
    change_times: list[float] = []
    sample_times: list[float] = []

    while time.monotonic() < deadline:
        ts, lvl = await sample_device_level(client)  # this also runs the watchdog
        hw = read_has_water(client)
        water_samples += 1
        if hw is True:
            water_true += 1
        now_m = time.monotonic()
        sample_times.append(now_m)
        if lvl is not None and lvl != last_level:
            change_times.append(now_m)
            last_level = lvl
        log(f"  t+{CIRCULATION_MINUTES*60 - (deadline - now_m):5.0f}s "
            f"device_level={lvl} hasWater={hw}")
        await asyncio.sleep(POLL_INTERVAL_S)

    # Sealed-loop verdict: every sampled hasWater must be True (no drain anywhere).
    passed = water_samples > 0 and water_true == water_samples
    detail = (f"hasWater True {water_true}/{water_samples} samples over "
              f"{CIRCULATION_MINUTES:.0f} min")
    log(f"  -> {'PASS' if passed else 'FAIL'}: {detail}")
    if not passed:
        log("     (in the bucket the reservoir drained; a sealed in-bed loop should hold)")
    sealed = CheckResult(
        "sealed_loop_circulation", passed, detail,
        metrics={"water_true": water_true, "water_samples": water_samples,
                 "circulation_min": CIRCULATION_MINUTES},
    )

    # Telemetry cadence: median gap between observed device_level CHANGES.
    if len(change_times) >= 2:
        gaps = [b - a for a, b in zip(change_times, change_times[1:])]
        gaps.sort()
        median_gap = gaps[len(gaps) // 2]
        cadence_detail = (f"device_level changed {len(change_times)} times; "
                          f"median refresh interval ~{median_gap:.0f}s "
                          f"(polled every {POLL_INTERVAL_S:.0f}s)")
    else:
        median_gap = None
        cadence_detail = (f"device_level changed <2 times during circulation "
                          f"(refresh slower than the {CIRCULATION_MINUTES:.0f}-min window, "
                          f"or already at floor)")
    log(f"  telemetry cadence -> {cadence_detail}")
    cadence = CheckResult(
        "telemetry_cadence", None, cadence_detail,
        metrics={"median_refresh_s": median_gap, "poll_interval_s": POLL_INTERVAL_S,
                 "n_changes": len(change_times)},
    )
    return sealed, cadence


# ---------------------------------------------------------------------------------
# CHECK 2: in-bed thermal time-constant + ramp-rate calibration.
# Drives a single ramp to `target_level`, samples device_level, returns levels/min and
# approximate time-to-plateau against the REAL in-bed thermal mass.
# ---------------------------------------------------------------------------------
async def _measure_ramp(client: EightSleepClient, target_level: int, label: str) -> dict:
    banner(f"CHECK: in-bed {label} ramp — command level {target_level:+d}, "
           f"measure levels/min + time-to-plateau")
    await client.turn_on_side()
    await client.set_heating_level(target_level, duration_s=0)

    t0 = time.monotonic()
    ts0, start_level = await sample_device_level(client)
    log(f"  start device_level={start_level}, target={target_level:+d}")

    samples: list[tuple[float, int]] = []
    if start_level is not None:
        samples.append((0.0, start_level))

    plateau_t: Optional[float] = None
    deadline = t0 + RAMP_MAX_MINUTES * 60.0
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        ts, lvl = await sample_device_level(client)  # watchdog inside
        if lvl is None:
            continue
        elapsed = time.monotonic() - t0
        samples.append((elapsed, lvl))
        gap = target_level - lvl
        log(f"  t+{elapsed:5.0f}s device_level={lvl:+4d} (gap to target {gap:+d})")

        # Plateau = reached target margin, OR device_level stopped moving for a window.
        if abs(gap) <= PLATEAU_EPS_LEVELS:
            plateau_t = elapsed
            log(f"  reached target margin (+/-{PLATEAU_EPS_LEVELS}) at t+{elapsed:.0f}s")
            break
        recent = [s for s in samples if s[0] >= elapsed - PLATEAU_WINDOW_S]
        if len(recent) >= 2 and (recent[-1][0] - recent[0][0]) >= PLATEAU_WINDOW_S * 0.8:
            moved = abs(recent[-1][1] - recent[0][1])
            if moved <= PLATEAU_EPS_LEVELS:
                plateau_t = elapsed
                log(f"  device_level flat ({moved} levels over "
                    f"~{PLATEAU_WINDOW_S:.0f}s) -> plateau at t+{elapsed:.0f}s")
                break

    # Ramp rate = best-fit slope over the moving portion (levels per minute).
    rate_per_min = None
    if len(samples) >= 2:
        first_t, first_l = samples[0]
        # Use the last sample before plateau (or the last overall) as the endpoint.
        end_t, end_l = samples[-1]
        if end_t > first_t:
            rate_per_min = (end_l - first_l) / ((end_t - first_t) / 60.0)

    result = {
        "label": label,
        "target_level": target_level,
        "start_level": start_level,
        "end_level": samples[-1][1] if samples else None,
        "levels_per_min": rate_per_min,
        "time_to_plateau_s": plateau_t,
        "n_samples": len(samples),
    }
    rate_txt = f"{rate_per_min:+.1f}" if rate_per_min is not None else "n/a"
    plat_txt = f"{plateau_t:.0f}s" if plateau_t is not None else f">{RAMP_MAX_MINUTES*60:.0f}s (capped)"
    log(f"  {label} ramp rate ~ {rate_txt} levels/min; time-to-plateau ~ {plat_txt}")
    return result


async def check_thermal_ramps(client: EightSleepClient) -> CheckResult:
    cool = await _measure_ramp(client, COOL_LEVEL, "COOL")
    # Give the bed a beat, then drive the opposite direction from wherever it is.
    await asyncio.sleep(POLL_INTERVAL_S)
    heat = await _measure_ramp(client, HEAT_LEVEL, "HEAT")

    cool_rate = cool["levels_per_min"]
    heat_rate = heat["levels_per_min"]
    # PASS = the bed actually moved meaningfully in BOTH directions (real in-bed response).
    # Cooling should drive the level negative; heating positive. Require non-trivial motion.
    cool_ok = cool_rate is not None and cool_rate <= -1.0
    heat_ok = heat_rate is not None and heat_rate >= 1.0
    passed = cool_ok and heat_ok
    detail = (
        f"cool ~{cool_rate if cool_rate is not None else 'n/a'} levels/min "
        f"(plateau {cool['time_to_plateau_s']}s), "
        f"heat ~{heat_rate if heat_rate is not None else 'n/a'} levels/min "
        f"(plateau {heat['time_to_plateau_s']}s)"
    )
    log(f"\n  -> {'PASS' if passed else 'FAIL'}: {detail}")
    if not passed:
        log("     (a flat device_level despite the command = stalled: re-check water, "
            "cover seating, or hardware — these in-bed rates feed the controller)")
    return CheckResult(
        "thermal_ramp_calibration", passed, detail,
        metrics={"cool": cool, "heat": heat},
    )


# ---------------------------------------------------------------------------------
# CHECK 4b: thermal_health verdict on the samples we just gathered (post-ramp).
# Recomputed standalone (no controller import) using the same device-level logic.
# ---------------------------------------------------------------------------------
def summarize_thermal_health(ramp: CheckResult) -> CheckResult:
    banner("CHECK: thermal_health summary (device-level responded to commands)")
    cool = ramp.metrics.get("cool", {})
    heat = ramp.metrics.get("heat", {})
    responding = bool(
        (cool.get("levels_per_min") or 0) < 0 and (heat.get("levels_per_min") or 0) > 0
    )
    state = "ok" if responding else "stalled"
    detail = (f"state={state}; cool moved {cool.get('start_level')}->{cool.get('end_level')}, "
              f"heat moved {heat.get('start_level')}->{heat.get('end_level')}")
    log(f"  -> {'PASS' if responding else 'FAIL'}: {detail}")
    return CheckResult("thermal_health", responding, detail,
                       metrics={"state": state, "responding": responding})


# ---------------------------------------------------------------------------------
# SAFE-OFF: ALWAYS power the side off and confirm nowHeating is False (retry loop).
# ---------------------------------------------------------------------------------
async def safe_off(client: EightSleepClient) -> CheckResult:
    banner("SAFE-OFF: powering side OFF and confirming nowHeating == False")
    confirmed = False
    last_state: Optional[bool] = None
    for attempt in range(1, SETTLE_OFF_RETRIES + 1):
        try:
            await client.turn_off_side()
        except Exception as exc:
            log(f"  attempt {attempt}: turn_off_side() error {exc!r} (will retry)")
        await asyncio.sleep(SETTLE_OFF_WAIT_S)
        try:
            await client.update(user=False, device=True)
        except Exception:
            pass
        last_state = read_now_heating(client)
        log(f"  attempt {attempt}: nowHeating={last_state}")
        if last_state is False:
            confirmed = True
            break

    if confirmed:
        log("  -> PASS: side is OFF (nowHeating == False).")
    else:
        log(f"  -> FAIL: could not confirm OFF after {SETTLE_OFF_RETRIES} attempts "
            f"(last nowHeating={last_state}). TURN THE SIDE OFF IN THE EIGHT SLEEP APP.")
    return CheckResult("safe_off", confirmed,
                       f"nowHeating={last_state} after {SETTLE_OFF_RETRIES} retries")


# ---------------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------------
async def run_battery(skip_long: bool) -> int:
    client = load_client()
    results: list[CheckResult] = []
    aborted = False

    try:
        await client.connect()
        log("Connected.")

        # Gate first: don't pump if offline / no water / mid-prime.
        status = await check_device_status(client)
        results.append(status)
        if status.passed is False:
            log("Gate failed — fix device_status problems above, then re-run. "
                "Skipping active pumping checks.")
        else:
            results.append(await check_f_level_mapping(client))
            if not skip_long:
                sealed, cadence = await check_sealed_loop(client)
                results.append(sealed)
                results.append(cadence)
            else:
                log("(--quick) skipping the 5-min sealed-loop circulation phase.")
            ramp = await check_thermal_ramps(client)
            results.append(ramp)
            results.append(summarize_thermal_health(ramp))

    except WaterDrained as exc:
        aborted = True
        log(f"WATCHDOG ABORT: {exc}")
        results.append(CheckResult("water_watchdog", False, str(exc)))
    except KeyboardInterrupt:
        aborted = True
        log("Interrupted by user (Ctrl-C) — proceeding to SAFE-OFF.")
    except Exception as exc:
        aborted = True
        log(f"UNEXPECTED ERROR: {exc!r} — proceeding to SAFE-OFF.")
        results.append(CheckResult("run_error", False, repr(exc)))
    finally:
        # SAFE-OFF always runs, even on abort/exception/interrupt.
        try:
            results.append(await safe_off(client))
        except Exception as exc:
            log(f"SAFE-OFF itself errored: {exc!r} — TURN THE SIDE OFF IN THE APP.")
            results.append(CheckResult("safe_off", False, repr(exc)))
        try:
            await client.close()
        except Exception:
            pass

    # ---- Final report ----
    banner("RESULTS")
    n_fail = 0
    for r in results:
        if r.passed is None:
            tag = "INFO"
        elif r.passed:
            tag = "PASS"
        else:
            tag = "FAIL"
            n_fail += 1
        print(f"  [{tag}] {r.name}: {r.detail}", flush=True)

    print("", flush=True)
    if aborted:
        log("Battery ABORTED before completion (see above). Side has been SAFE-OFF'd.")
    if n_fail == 0 and not aborted:
        log("ALL CHECKS PASSED — repair validated and in-bed calibration captured.")
        return 0
    log(f"{n_fail} check(s) FAILED" + (" / run aborted" if aborted else "") + ".")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick", action="store_true",
        help="skip the 5-min sealed-loop circulation phase (ramps + status only).",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run_battery(skip_long=args.quick))
    except KeyboardInterrupt:
        # asyncio.run already cancelled; SAFE-OFF ran in the finally above.
        log("Exited.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
