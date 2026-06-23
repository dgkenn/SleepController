"""Dashboard control daemon.

Owns the device. Each tick it (1) applies any pending override commands from the API, (2)
runs one sense->decide->act cycle via the sleepctl ControlCycle, (3) writes a runtime_state
snapshot the API/SSE reads. Emergency stop / mode / manual-temp overrides are honored here so
the API never touches the device directly. Runs against the simulator by default (no Pod
needed) or the live Eight Sleep client.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# make sleepctl + dashboard.api importable when run as a script
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/dashboard/api")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

from sleepctl.adapters.simulator import SimulatorActuator, SimulatorSource  # noqa: E402
from sleepctl.config import AppConfig  # noqa: E402
from sleepctl.controller.controller import SleepController  # noqa: E402
from sleepctl.loop.cycle import ControlCycle  # noqa: E402
from sleepctl.models import ContextRecord  # noqa: E402

from app import bridge  # noqa: E402
from app.db import get_repo  # noqa: E402


class DashboardDaemon:
    def __init__(self, simulate: bool = True, poll_seconds: float = 5.0) -> None:
        self.cfg = AppConfig.default()
        self.repo = get_repo()
        self.cycle = ControlCycle(self.cfg, self.repo, SleepController(self.cfg,
                                  setpoints=self.repo.latest_setpoints()))
        self.poll_seconds = poll_seconds
        self.mode = "auto"
        self.paused = False
        self.manual_target_f = None
        self.simulate = simulate
        if simulate:
            self.source = SimulatorSource("normal", seed=7,
                                          start=datetime.now() - timedelta(minutes=1))
            self.actuator = SimulatorActuator(self.source)
        self.context = ContextRecord(date=datetime.now().date().isoformat())

    def _apply_commands(self) -> None:
        while True:
            cmd = bridge.next_pending_command(self.repo.conn)
            if cmd is None:
                break
            t, p = cmd["type"], cmd["payload"]
            if t == "stop":
                self.paused = True
                if self.simulate:
                    self.actuator.set_level(0)  # neutral/off
            elif t == "pause":
                self.paused = True
            elif t in ("start", "resume"):
                self.paused = False
            elif t == "safe_default":
                self.paused = False
                self.manual_target_f = None
                self.mode = "auto"
                self.repo.save_setpoints(self.cfg.default_setpoints())
            elif t == "set_mode":
                self.mode = p.get("mode", "auto")
            elif t == "set_temp":
                self.manual_target_f = p.get("target_f")
                self.mode = "manual"
            elif t == "set_wake":
                # store required wake for tonight's context
                hh, mm = (int(x) for x in p["wake_time"].split(":"))
                wake = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                if wake <= datetime.now():
                    wake += timedelta(days=1)
                self.context.required_wake_time = wake
            bridge.mark_applied(self.repo.conn, cmd["id"])

    def _snapshot(self, decision, frame) -> dict:
        return {
            "state": decision.state.value if decision else "IDLE",
            "objective": decision.objective.value if decision else None,
            "mode": "paused" if self.paused else self.mode,
            "target_temp_f": decision.target_temp_f if decision else None,
            "bed_temp_f": frame.bed_temp_f if frame else None,
            "room_temp_f": frame.room_temp_f if frame else None,
            "stage": frame.stage.value if frame else None,
            "confidence": decision.confidence if decision else None,
            "target_level": decision.target_level if decision else None,
            "daemon_alive": True,
            "extra": {"manual_target_f": self.manual_target_f},
        }

    def tick(self) -> None:
        self._apply_commands()
        frame = self.source.read_frame()
        now = self.source.now()
        decision = None
        if not self.paused:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                level = self.cycle.controller.thermal.to_level(self.manual_target_f)
                self.actuator.set_level(level)
            elif self.mode == "auto":
                level = self.cycle.pending_level(decision, frame, now)
                if level is not None:
                    self.actuator.set_level(level)
                alarm = self.cycle.pending_alarm()
                if alarm is not None:
                    self.actuator.set_alarm(alarm.time, alarm.vibration_power, alarm.thermal_level)
            self.cycle.log(frame, decision, now)
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    def run(self, max_ticks=None) -> None:
        ticks = 0
        while True:
            try:
                self.tick()
            except Exception as exc:  # keep the daemon alive; surface via stale state
                print(f"daemon tick error: {exc}", flush=True)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(self.poll_seconds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real Eight Sleep client")
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--max-ticks", type=int, default=None)
    args = ap.parse_args()
    DashboardDaemon(simulate=not args.live, poll_seconds=args.poll_seconds).run(args.max_ticks)


if __name__ == "__main__":
    main()
