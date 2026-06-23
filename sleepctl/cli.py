"""sleepctl command-line interface.

Subcommands:
  replay     run synthetic nights through the full controller loop (no hardware)
  report     show rolling baselines + recent nightly summaries
  run        run the live closed-loop daemon (requires a configured Pod adapter)
  auth       authenticate to Eight Sleep / Google Calendar
  calibrate  probe Pod 2 capabilities + build the F<->level calibration
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from sleepctl.config import AppConfig


def _cmd_replay(args: argparse.Namespace) -> int:
    from sleepctl.adapters.calendar import ManualCalendarSource
    from sleepctl.adapters.simulator import SimulatorActuator, SimulatorSource
    from sleepctl.loop.nightly import NightlyUpdater
    from sleepctl.loop.runtime import Runtime
    from sleepctl.storage.repository import Repository

    cfg = AppConfig.default()
    repo = Repository(args.db)
    scenarios = args.scenarios or ["normal", "short_sleep", "clustered_awakenings"]

    start = datetime(2026, 6, 23, 23, 0, 0)
    for i, scenario in enumerate(scenarios):
        night_start = start + timedelta(days=i)
        source = SimulatorSource(scenario, seed=7 + i, start=night_start)
        actuator = SimulatorActuator(source)
        required_wake = night_start + timedelta(minutes=source.length)
        calendar = ManualCalendarSource(required_wake_time=required_wake, bedtime=night_start)
        context = calendar.get_context(night_start.date().isoformat())

        runtime = Runtime(cfg, source, actuator, repo, calendar)
        decisions = runtime.replay(context)

        states = {}
        for d in decisions:
            states[d.state.value] = states.get(d.state.value, 0) + 1
        night = source.fetch_night_summary(night_start.date().isoformat())
        updater = NightlyUpdater(cfg, repo)
        result = updater.run(night)

        levels = actuator.commands
        max_jump = max((abs(b - a) for a, b in zip(levels, levels[1:])), default=0)

        print(f"\n=== night {i+1}: {scenario} ===")
        print(f"  states: {states}")
        print(
            f"  summary: sleep={night.total_sleep_min:.0f}m deep={night.deep_min:.0f}m "
            f"rem={night.rem_min:.0f}m wake_events={night.wake_events} eff={night.sleep_efficiency}"
        )
        print(f"  commands issued: {len(levels)}  max single level jump: {max_jump}")
        print(f"  recommendation: {result['recommendation']['action']} -> "
              f"{result['recommendation']['reason']}")

    print(f"\nDataset written to {args.db}")
    repo.close()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from sleepctl.storage.repository import Repository

    repo = Repository(args.db)
    nights = repo.recent_nights(14)
    baselines = repo.latest_baselines()
    print(f"Recent nights ({len(nights)}):")
    for n in nights:
        print(
            f"  {n.date}: sleep={n.total_sleep_min} deep={n.deep_min} "
            f"wake_events={n.wake_events} eff={n.sleep_efficiency}"
        )
    if baselines:
        print("\nBaselines (selected):")
        for key in sorted(baselines.metrics):
            if key.endswith("_7d_median"):
                print(f"  {key}: {baselines.metrics[key]:.2f}")
    repo.close()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    print("run: live daemon not wired in this build.")
    # TODO(integration): construct EightSleepCloudSource/Actuator + GoogleCalendarSource,
    # then call Runtime.tick() on a ~1-minute timer with a nightly close-out.
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    print("auth: configure Eight Sleep credentials + Google Calendar OAuth (not wired).")
    # TODO(integration): perform pyEight OAuth2 login + Google token flow.
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    print("calibrate: probe Pod 2 capabilities + build F<->level calibration (not wired).")
    # TODO(integration): call EightSleepCloudSource.capabilities() against the live Pod.
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sleepctl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_replay = sub.add_parser("replay", help="Replay synthetic nights through the controller")
    p_replay.add_argument("--source", default="simulator")
    p_replay.add_argument("--db", default=":memory:")
    p_replay.add_argument("--scenarios", nargs="*", default=None)
    p_replay.set_defaults(func=_cmd_replay)

    p_report = sub.add_parser("report", help="Show baselines and recent nights")
    p_report.add_argument("--db", default="sleepctl.db")
    p_report.set_defaults(func=_cmd_report)

    sub.add_parser("run", help="Run the live closed-loop controller").set_defaults(func=_cmd_run)
    sub.add_parser("auth", help="Authenticate to Eight Sleep / Google Calendar").set_defaults(
        func=_cmd_auth
    )
    sub.add_parser("calibrate", help="Probe device + build temperature calibration").set_defaults(
        func=_cmd_calibrate
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
