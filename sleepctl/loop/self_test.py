"""On-bed self-test / calibration battery — the push-button "is it actually working?" check.

Run this from the dashboard (Admin → Run bed test) once the Pod is filled, primed, connected
to the mattress, and YOU ARE LYING ON IT. It answers the two questions the nightly UI can't
show at a glance:

  1. Does the bed SENSE me?  presence + live HR / HRV / respiratory-rate / sleep-stage.
     (This is the whole reason to run it in-bed — the standalone thermal rig can't test it.)
  2. Does the bed RESPOND?   commanding cool/heat actually moves the Hub's water-side
     ``device_level`` (the trustworthy thermal signal), and the sealed loop holds water.

Design notes baked in from the live bring-up:
  * The trustworthy thermal signal is ``frame.device_level`` (the Hub water side), NOT the
    cover-side ``bed_temp_f`` (an ambient artifact that tracked room air even at MAX COOL).
  * A ``hasWater`` watchdog aborts the moment the loop drains, and the battery ALWAYS ends by
    powering the side OFF (the caller restores the prior control state).
  * Over a SIMULATOR client (``device_status()['simulated']``) the thermal/water phases can't be
    a real validation, so they run as INFO — the command path is still exercised end-to-end.

The battery is client-agnostic (real ``EightSleepClient`` or ``SimulatedLiveClient``) and drives
only the small surface the daemon already uses: ``update`` / ``read_frame`` / ``device_status`` /
``turn_on_side`` / ``turn_off_side`` / ``set_heating_level``. It touches no learning, storage,
alarms, or controller state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sleepctl.controller.calibration import fahrenheit_to_level, level_to_fahrenheit

# --- tunables (conservative; every active phase is undone by the final SAFE-OFF) -------------
POLL_S = 5.0                 # how often we sample read_frame()/device_status()
SENSE_ACQUIRE_S = 60.0       # keep polling up to this long for presence + HR to come in
CIRCULATION_MIN = 3.0        # sustained pumping window for the sealed-loop test (full mode)
RAMP_MAX_MIN = 4.0           # cap on how long we watch a single ramp for plateau
GENTLE_PULSE_S = 90.0        # short cool pulse window (gentle mode)
PLATEAU_EPS_LEVELS = 2       # |delta| below this over the window => settled
PLATEAU_WINDOW_S = 60.0
COOL_SETPOINT_F = 60.0       # circulation / gentle-pulse setpoint
COOL_LEVEL = -100            # MAX cool — clearest ramp signal
HEAT_LEVEL = 100             # MAX heat

# plausible in-bed ranges for the sensing checks (soft — out-of-range is INFO, not FAIL)
HR_RANGE = (30, 120)
HRV_RANGE = (5, 250)
RR_RANGE = (6, 30)


ProgressCb = Callable[["SelfTestReport"], None]
CancelCb = Callable[[], bool]


@dataclass
class CheckResult:
    name: str
    passed: Optional[bool]                 # True / False, or None for informational
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "detail": self.detail, "metrics": self.metrics}


@dataclass
class SelfTestReport:
    mode: str = "full"
    running: bool = True
    aborted: bool = False
    phase: str = "starting"                # human label of what's happening right now
    started: str = ""
    finished: Optional[str] = None
    checks: list[CheckResult] = field(default_factory=list)
    # measured cool/heat rates + lags (full mode, real device) → fed to the timing modules
    calibration: Optional[dict] = None

    @property
    def overall_passed(self) -> Optional[bool]:
        graded = [c.passed for c in self.checks if c.passed is not None]
        if not graded:
            return None
        return all(graded)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "running": self.running, "aborted": self.aborted,
            "phase": self.phase, "started": self.started, "finished": self.finished,
            "overall_passed": self.overall_passed,
            "n_fail": sum(1 for c in self.checks if c.passed is False),
            "checks": [c.to_dict() for c in self.checks],
            "calibration": self.calibration,
        }


class WaterDrained(Exception):
    """hasWater went False mid-run → abort to SAFE-OFF."""


class Cancelled(Exception):
    """The user (or an emergency stop) asked to cancel → abort to SAFE-OFF."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _in_range(v, lo, hi) -> bool:
    return v is not None and lo <= v <= hi


async def _update(client) -> None:
    try:
        await client.update()
    except Exception:
        pass  # a single bad poll shouldn't kill the battery; the watchdog still guards water


def _has_water(client) -> Optional[bool]:
    try:
        return client.device_status().get("has_water")
    except Exception:
        return None


def _is_simulated(client) -> bool:
    try:
        return bool(client.device_status().get("simulated"))
    except Exception:
        return False


class _Battery:
    def __init__(self, client, mode: str, dry_run: bool,
                 on_progress: Optional[ProgressCb], cancelled: Optional[CancelCb]):
        self.client = client
        self.mode = mode
        self.dry_run = dry_run
        self.on_progress = on_progress
        self.cancelled = cancelled
        self.simulated = False
        self.report = SelfTestReport(mode=mode, started=_now_iso())

    # -- progress plumbing ----------------------------------------------------
    def _emit(self, phase: Optional[str] = None) -> None:
        if phase is not None:
            self.report.phase = phase
        if self.on_progress is not None:
            try:
                self.on_progress(self.report)
            except Exception:
                pass

    def _add(self, check: CheckResult) -> CheckResult:
        self.report.checks.append(check)
        self._emit()
        return check

    def _guard(self) -> None:
        """Between-phase watchdog: honor cancel + hasWater drain."""
        if self.cancelled is not None and self.cancelled():
            raise Cancelled("cancelled by user / emergency stop")
        if not self.simulated and _has_water(self.client) is False:
            raise WaterDrained("hasWater went False — loop is draining")

    async def _sample_level(self) -> Optional[int]:
        await _update(self.client)
        self._guard()
        lvl = self.client.read_frame().device_level
        return int(lvl) if lvl is not None else None

    # -- CHECK: connectivity / device_status gate -----------------------------
    async def check_status(self) -> bool:
        self._emit("device status")
        await _update(self.client)
        status = self.client.device_status()
        self.simulated = bool(status.get("simulated"))
        problems = []
        if status.get("online") is False:
            problems.append("device OFFLINE")
        if status.get("has_water") is False:
            problems.append("no water (fill + prime the Pod)")
        if status.get("needs_priming") is True:
            problems.append("needs priming")
        if status.get("priming") is True:
            problems.append("currently priming — wait, then re-run")
        passed = len(problems) == 0
        detail = ("simulator (healthy)" if self.simulated
                  else ("online, watered, not priming" if passed else "; ".join(problems)))
        self._add(CheckResult("connectivity", passed, detail, metrics=status))
        return passed

    # -- CHECK: sensing — does the bed detect ME? -----------------------------
    async def check_sensing(self) -> None:
        self._emit("sensing you in bed")
        # Give the BCG a beat to acquire: poll up to SENSE_ACQUIRE_S for presence + HR.
        deadline = time.monotonic() + SENSE_ACQUIRE_S
        frame = self.client.read_frame()
        while time.monotonic() < deadline:
            await _update(self.client)
            self._guard()
            frame = self.client.read_frame()
            if frame.presence is True and frame.heart_rate is not None:
                break
            await asyncio.sleep(POLL_S)

        presence, hr = frame.presence, frame.heart_rate
        hrv, rr = frame.hrv, frame.respiratory_rate
        stage = frame.stage.value if getattr(frame, "stage", None) is not None else None

        # Presence + a live HR are the load-bearing PASS (you're lying on it, still).
        if presence is False:
            self._add(CheckResult("presence", False,
                                  "bed does not detect you — lie still on your side / reseat cover"))
        elif presence is None:
            self._add(CheckResult("presence", None,
                                  "presence unknown (Pod 2 reports it lazily) — HR is the real tell"))
        else:
            self._add(CheckResult("presence", True, "bed detects you in it"))

        if hr is None:
            self._add(CheckResult("heart_rate", False,
                                  f"no HR after {SENSE_ACQUIRE_S:.0f}s — stay still; BCG needs a motionless minute"))
        else:
            ok = _in_range(hr, *HR_RANGE)
            self._add(CheckResult("heart_rate", ok,
                                  f"{hr:.0f} bpm" + ("" if ok else " (outside typical resting range)"),
                                  metrics={"bpm": hr}))

        # HRV / RR update per-session (slower) — informational if not yet in.
        if hrv is None:
            self._add(CheckResult("hrv", None, "not yet (updates per-session; may lag a few min)"))
        else:
            self._add(CheckResult("hrv", _in_range(hrv, *HRV_RANGE), f"{hrv:.0f} ms",
                                  metrics={"ms": hrv}))
        if rr is None:
            self._add(CheckResult("respiratory_rate", None, "not yet (updates per-session)"))
        else:
            self._add(CheckResult("respiratory_rate", _in_range(rr, *RR_RANGE),
                                  f"{rr:.1f} /min", metrics={"rpm": rr}))
        self._add(CheckResult("sleep_stage", None,
                              f"reporting '{stage}'" if stage else "no stage yet (awake expected at setup)"))

    # -- CHECK: °F <-> level mapping sanity ------------------------------------
    async def check_mapping(self) -> None:
        self._emit("°F↔level mapping")
        mono = fahrenheit_to_level(60.0) < fahrenheit_to_level(70.0) < fahrenheit_to_level(81.0)
        expected = fahrenheit_to_level(COOL_SETPOINT_F)
        target = None
        if not self.dry_run:
            try:
                await self.client.turn_on_side()
                await self.client.set_heating_level(expected, duration_s=0)
                await asyncio.sleep(POLL_S)
                await _update(self.client)
                target = self.client.read_frame().target_level
            except Exception:
                target = None
        echo_ok = target is None or abs(int(target) - expected) <= 5
        passed = mono and echo_ok
        detail = (f"{COOL_SETPOINT_F:.0f}°F→level {expected:+d} "
                  f"(round-trips to {level_to_fahrenheit(expected):.0f}°F); "
                  + (f"device echoed target {target:+d}" if target is not None else "device echo lazy [ok]"))
        self._add(CheckResult("f_level_mapping", passed, detail,
                              metrics={"expected_level": expected, "device_target": target}))

    # -- CHECK: sealed-loop circulation (full mode) ---------------------------
    async def check_sealed_loop(self) -> None:
        self._emit("sealed-loop circulation")
        if self.simulated:
            self._add(CheckResult("sealed_loop_circulation", None,
                                  "simulated — no real water loop to validate"))
            return
        cool = fahrenheit_to_level(COOL_SETPOINT_F)
        if not self.dry_run:
            await self.client.turn_on_side()
            await self.client.set_heating_level(cool, duration_s=0)
        deadline = time.monotonic() + CIRCULATION_MIN * 60.0
        samples = held = 0
        while time.monotonic() < deadline:
            await _update(self.client)
            self._guard()
            samples += 1
            if _has_water(self.client) is True:
                held += 1
            self._emit()  # keep runtime_state fresh (dashboard liveness) during the long window
            await asyncio.sleep(POLL_S)
        passed = samples > 0 and held == samples
        self._add(CheckResult("sealed_loop_circulation", passed,
                              f"hasWater held {held}/{samples} samples over {CIRCULATION_MIN:.0f} min"
                              + ("" if passed else " — a sealed in-bed loop should not drain"),
                              metrics={"held": held, "samples": samples}))

    # -- CHECK: thermal response (does device_level actually move?) -----------
    async def _ramp(self, target_level: int, label: str) -> dict:
        self._emit(f"{label} ramp")
        if not self.dry_run:
            await self.client.turn_on_side()
            await self.client.set_heating_level(target_level, duration_s=0)
        t0 = time.monotonic()
        start = await self._sample_level()
        samples: list[tuple[float, int]] = []
        if start is not None:
            samples.append((0.0, start))
        plateau = None
        deadline = t0 + RAMP_MAX_MIN * 60.0
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            lvl = await self._sample_level()
            if lvl is None:
                continue
            elapsed = time.monotonic() - t0
            samples.append((elapsed, lvl))
            self._emit()  # keep runtime_state fresh (dashboard liveness) during the ramp
            if abs(target_level - lvl) <= PLATEAU_EPS_LEVELS:
                plateau = elapsed
                break
            recent = [s for s in samples if s[0] >= elapsed - PLATEAU_WINDOW_S]
            if len(recent) >= 2 and (recent[-1][0] - recent[0][0]) >= PLATEAU_WINDOW_S * 0.8 \
                    and abs(recent[-1][1] - recent[0][1]) <= PLATEAU_EPS_LEVELS:
                plateau = elapsed
                break
        rate = None
        if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
            rate = (samples[-1][1] - samples[0][1]) / ((samples[-1][0] - samples[0][0]) / 60.0)
        return {"label": label, "start_level": start,
                "end_level": samples[-1][1] if samples else None,
                "levels_per_min": rate, "time_to_plateau_s": plateau}

    async def check_thermal_full(self) -> None:
        if self.simulated:
            self._add(CheckResult("thermal_response", None,
                                  "simulated — command path exercised, but no real thermal mass"))
            if not self.dry_run:
                await self.client.set_heating_level(COOL_LEVEL)
                await self.client.set_heating_level(HEAT_LEVEL)
            return
        cool = await self._ramp(COOL_LEVEL, "cool")
        self._guard()
        await asyncio.sleep(POLL_S)
        heat = await self._ramp(HEAT_LEVEL, "heat")
        cool_ok = (cool["levels_per_min"] or 0) <= -1.0
        heat_ok = (heat["levels_per_min"] or 0) >= 1.0
        passed = cool_ok and heat_ok
        # Persist the measured rates + effect-latency so the timing modules (pre-cool lead,
        # smart-wake warm-up) start from THIS bed+mattress+body, not a generic assumption.
        self.report.calibration = {
            "cool_levels_per_min": _round(cool["levels_per_min"]),
            "heat_levels_per_min": _round(heat["levels_per_min"]),
            "cool_f_per_min": _f_rate(cool),
            "heat_f_per_min": _f_rate(heat),
            "cool_lag_min": _round((cool["time_to_plateau_s"] or 0) / 60.0) or None,
            "heat_lag_min": _round((heat["time_to_plateau_s"] or 0) / 60.0) or None,
            "source": "self_test",
        }
        gap = ""
        cf, hf = self.report.calibration["cool_f_per_min"], self.report.calibration["heat_f_per_min"]
        if cf is not None and hf is not None:
            gap = f"; {'cools' if abs(cf) >= abs(hf) else 'heats'} faster"
        self._add(CheckResult(
            "thermal_response", passed,
            f"cool ~{_fmt(cool['levels_per_min'])} lvl/min, heat ~{_fmt(heat['levels_per_min'])} lvl/min{gap}"
            + ("" if passed else " — device_level barely moved: recheck water, cover seating, hardware"),
            metrics={"cool": cool, "heat": heat}))

    async def check_thermal_gentle(self) -> None:
        self._emit("cool pulse")
        if self.simulated:
            self._add(CheckResult("thermal_response", None,
                                  "simulated — command path exercised, but no real thermal mass"))
            if not self.dry_run:
                await self.client.set_heating_level(fahrenheit_to_level(COOL_SETPOINT_F))
            return
        start = await self._sample_level()
        if not self.dry_run:
            await self.client.turn_on_side()
            await self.client.set_heating_level(fahrenheit_to_level(COOL_SETPOINT_F), duration_s=0)
        deadline = time.monotonic() + GENTLE_PULSE_S
        end = start
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            end = await self._sample_level()
        moved = (start is not None and end is not None and (end - start) <= -1)
        self._add(CheckResult("thermal_response", moved,
                              f"device_level {start}→{end} on a cool command"
                              + ("" if moved else " — expected it to drop; recheck water/cover/hardware"),
                              metrics={"start_level": start, "end_level": end}))

    # -- SAFE-OFF: always power the side off ----------------------------------
    async def safe_off(self) -> None:
        self._emit("safe-off")
        confirmed = False
        for _ in range(6):
            try:
                await self.client.turn_off_side()
            except Exception:
                pass
            await asyncio.sleep(3.0)
            try:
                await self.client.update()
            except Exception:
                pass
            key = ("leftNowHeating" if getattr(self.client, "side", "left") == "left"
                   else "rightNowHeating")
            now_heating = None
            try:
                now_heating = self.client._eight.device_data.get(key)  # type: ignore[attr-defined]
            except Exception:
                now_heating = False if self.simulated else None
            if now_heating is False or self.simulated:
                confirmed = True
                break
        self._add(CheckResult("safe_off", confirmed if not self.simulated else True,
                              "side powered OFF" if confirmed else
                              "could not confirm OFF — turn the side off in the Eight Sleep app"))

    async def run(self) -> SelfTestReport:
        try:
            if await self.check_status():
                await self.check_sensing()
                if self.mode != "sensing":
                    self._guard()
                    await self.check_mapping()
                    if self.mode == "full":
                        self._guard()
                        await self.check_sealed_loop()
                        self._guard()
                        await self.check_thermal_full()
                    else:  # gentle
                        self._guard()
                        await self.check_thermal_gentle()
        except Cancelled:
            self.report.aborted = True
            self._add(CheckResult("cancelled", None, "test cancelled — powering off"))
        except WaterDrained as exc:
            self.report.aborted = True
            self._add(CheckResult("water_watchdog", False, str(exc)))
        except Exception as exc:  # never leave the bed driven on an unexpected error
            self.report.aborted = True
            self._add(CheckResult("error", False, f"{type(exc).__name__}: {exc}"))
        finally:
            try:
                await self.safe_off()
            except Exception:
                self._add(CheckResult("safe_off", False,
                                      "SAFE-OFF errored — turn the side off in the app"))
        self.report.running = False
        self.report.finished = _now_iso()
        self.report.phase = "done"
        self._emit()
        return self.report


def _fmt(v) -> str:
    return f"{v:+.1f}" if v is not None else "n/a"


def _round(v, n: int = 2):
    return round(v, n) if v is not None else None


def _f_rate(ramp: dict):
    """Convert a device-level ramp into an approximate °F/min using the non-linear map, from the
    ramp's own start→end levels over its duration (so it reflects the real operating range)."""
    start, end = ramp.get("start_level"), ramp.get("end_level")
    rate = ramp.get("levels_per_min")
    if start is None or end is None or rate in (None, 0):
        return None
    minutes = (end - start) / rate  # elapsed = Δlevel / (Δlevel/min)
    if minutes <= 0:
        return None
    df = level_to_fahrenheit(int(end)) - level_to_fahrenheit(int(start))
    return round(df / minutes, 2)


async def run_self_test(client, *, mode: str = "full", dry_run: bool = False,
                        on_progress: Optional[ProgressCb] = None,
                        cancelled: Optional[CancelCb] = None) -> SelfTestReport:
    """Run the on-bed battery over ``client`` and return the final report.

    mode: "full" (sensing + circulation + cool/heat ramps), "gentle" (sensing + short cool
    pulse), or "sensing" (presence + physiology only, no active thermal driving). ``on_progress``
    is called with the live report after every check; ``cancelled`` is polled between phases so an
    emergency stop aborts promptly (always followed by SAFE-OFF).
    """
    if mode not in ("full", "gentle", "sensing"):
        mode = "full"
    return await _Battery(client, mode, dry_run, on_progress, cancelled).run()
