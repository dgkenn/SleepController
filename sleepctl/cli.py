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

        # Use the latest learned setpoint profile (evolves night to night).
        from sleepctl.controller.controller import SleepController

        controller = SleepController(cfg, setpoints=repo.latest_setpoints())
        runtime = Runtime(cfg, source, actuator, repo, calendar, controller=controller)
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
    sp = repo.latest_setpoints()
    if sp:
        print(f"\nLearned setpoint v{sp.version} ({sp.source}): "
              f"neutral={sp.neutral_f:.1f}F deep={sp.deep_bias_f:.1f}F "
              f"rem_offset=+{sp.rem_warm_offset_f:.1f}F wake={sp.wake_ramp_f:.1f}F "
              f"blend_a={sp.composite_bed_weight:.2f}")
    actions = repo.recent_actions(5)
    if actions:
        print("\nRecent learning actions:")
        for a in actions:
            rw = f"{a.reward_observed:.2f}" if a.reward_observed is not None else "—"
            print(f"  {a.date}: {a.action_name} ({a.source}) conf={a.confidence:.2f} reward={rw}")
    from sleepctl.ml.phenotype import correlate_with_outcome
    corr = correlate_with_outcome(repo)
    if corr:
        print("\nPhenotype — factors most correlated with the night's reward:")
        for name, r, n in corr[:5]:
            print(f"  {name}: r={r} (n={n})")
    repo.close()
    return 0


def _cmd_night_report(args: argparse.Namespace) -> int:
    import json as _json

    from sleepctl.night_report import build_night_report
    from sleepctl.storage.repository import Repository

    repo = Repository(args.db)
    report = build_night_report(repo)
    repo.close()
    if getattr(args, "json", False):
        print(_json.dumps(report, indent=2, default=str))
        return 0
    print(f"━━ Nightly report — {report.get('date') or 'no data'} ━━")
    print(report["narrative"])
    if report.get("what_i_did", {}).get("recent"):
        print("\nWhat I did (most recent):")
        for a in report["what_i_did"]["recent"]:
            print(f"  {a.get('action')} {a.get('magnitude_f')}°F — {a.get('reason')}"
                  + ("  [held]" if a.get("held") else "")
                  + ("  [reverted]" if a.get("reverted") else ""))
    if report.get("suggestions"):
        print("\nSuggested next:")
        for s in report["suggestions"]:
            print(f"  • {s.get('reason')}")
    return 0


def _wake_context(wake: str | None, when: datetime) -> "object | None":
    """Build a ManualCalendarSource context from a --wake HH:MM (tomorrow-aware)."""
    if not wake:
        return None
    from datetime import timedelta

    from sleepctl.adapters.calendar import ManualCalendarSource

    hh, mm = (int(x) for x in wake.split(":"))
    target = when.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= when:
        target = target + timedelta(days=1)
    return ManualCalendarSource(required_wake_time=target, bedtime=when).get_context(
        when.date().isoformat()
    )


def _cmd_run(args: argparse.Namespace) -> int:
    import asyncio

    from sleepctl.loop.live import LiveDaemon
    from sleepctl.storage.repository import Repository

    cfg = AppConfig.default()
    repo = Repository(args.db)
    context = _wake_context(args.wake, datetime.now())

    if args.simulate:
        from sleepctl.loop.live import SimulatedLiveClient

        client = SimulatedLiveClient(scenario=args.scenario)
        # In simulate mode the night is finite; default a max so it terminates.
        max_ticks = args.max_ticks or (client.source.length + 5)
        poll = 0.0  # no real waiting offline
    else:
        from sleepctl.adapters.credentials import load_credentials
        from sleepctl.adapters.eightsleep_cloud import EightSleepClient

        creds = load_credentials(args.credentials)
        if not creds.is_complete():
            print("No Eight Sleep credentials found. Run `sleepctl auth` first "
                  "(or set EIGHTSLEEP_EMAIL / EIGHTSLEEP_PASSWORD).")
            repo.close()
            return 2
        client = EightSleepClient(
            email=creds.email,
            password=creds.password,
            timezone=creds.timezone,
            side=args.side or creds.side,
            client_id=creds.client_id,
            client_secret=creds.client_secret,
        )
        max_ticks = args.max_ticks
        poll = args.poll_seconds

    weather = None
    if not args.no_weather and cfg.tunables.weather_enabled:
        from sleepctl.adapters.weather import OpenMeteoWeather

        weather = OpenMeteoWeather(
            latitude=args.lat if args.lat is not None else cfg.tunables.weather_latitude,
            longitude=args.lon if args.lon is not None else cfg.tunables.weather_longitude,
        )
        t = weather.current_temp_f()
        print(f"Ambient awareness: outdoor temp = {t} °F"
              if t is not None else "Ambient awareness: weather unavailable (will retry)")

    from sleepctl.controller.controller import SleepController

    controller = SleepController(cfg, setpoints=repo.latest_setpoints())
    daemon = LiveDaemon(cfg, client, repo, context=context, weather=weather,
                        controller=controller)
    try:
        asyncio.run(daemon.run(poll_seconds=poll, dry_run=args.dry_run, max_ticks=max_ticks))
    except KeyboardInterrupt:
        print("\ninterrupted; shutting down.")
    finally:
        repo.close()
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    import getpass

    from sleepctl.adapters.credentials import Credentials, load_credentials, save_credentials

    existing = load_credentials(args.credentials)
    email = args.email or input(f"Eight Sleep email [{existing.email}]: ").strip() or existing.email
    if args.password:
        password = args.password
    else:
        password = getpass.getpass("Eight Sleep password (blank = keep existing): ") or existing.password
    timezone = args.timezone or input(f"Timezone [{existing.timezone}]: ").strip() or existing.timezone
    side = args.side or input(f"Bed side left/right [{existing.side}]: ").strip() or existing.side

    creds = Credentials(
        email=email, password=password, timezone=timezone, side=side,
        client_id=existing.client_id, client_secret=existing.client_secret,
    )
    if not creds.is_complete():
        print("email and password are required.")
        return 2
    path = save_credentials(creds, args.credentials)
    print(f"Saved credentials to {path} (mode 0600).")

    if args.test:
        import asyncio

        from sleepctl.adapters.eightsleep_cloud import EightSleepClient

        async def _probe():
            client = EightSleepClient(creds.email, creds.password, creds.timezone, creds.side,
                                      creds.client_id, creds.client_secret)
            await client.connect()
            await client.update()
            print("Connected. Capabilities:", client.capabilities())
            await client.close()

        try:
            asyncio.run(_probe())
        except Exception as exc:  # pragma: no cover - live network
            print(f"Connection test failed: {exc}")
            return 1
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    """Read-only probe of the live Pod: capabilities + current level/bed temp."""
    import asyncio

    from sleepctl.adapters.credentials import load_credentials
    from sleepctl.adapters.eightsleep_cloud import EightSleepClient

    creds = load_credentials(args.credentials)
    if not creds.is_complete():
        print("No credentials. Run `sleepctl auth` first.")
        return 2

    async def _run():
        client = EightSleepClient(creds.email, creds.password, creds.timezone, creds.side,
                                  creds.client_id, creds.client_secret)
        await client.connect()
        report = await client.probe()  # live per-field Pod 2 capability probe
        frame = client.read_frame()

        print("=== Pod capability probe (this device) ===")
        print(f"  side: {report['side']}   cooling-capable Pod: {report['is_pod_with_cooling']}"
              f"   base present: {report['has_base']}")
        print("  biometric / control fields:")
        for name, info in report["fields"].items():
            mark = "ok " if info["available"] else "-- "
            print(f"    [{mark}] {name:22} = {info['value']}")
        print("  commands available:")
        for name, ok in report["commands"].items():
            print(f"    [{'ok ' if ok else '-- '}] {name}")
        if report["warnings"]:
            print("  WARNINGS:")
            for w in report["warnings"]:
                print(f"    ! {w}")
        print("\n=== current snapshot ===")
        print(f"  heating level: {client.get_current_level()}   "
              f"bed_temp_f={frame.bed_temp_f}  room_temp_f={frame.room_temp_f}")
        print(f"  HR={frame.heart_rate} HRV={frame.hrv} RR={frame.respiratory_rate} "
              f"stage={frame.stage.value} presence={frame.presence} age={frame.data_age_seconds}s")
        from sleepctl.controller.calibration import fahrenheit_to_level
        print("\nLevel scale: 55-110 F (API -100..100, non-linear). "
              f"Controller targets map e.g. 66F->{fahrenheit_to_level(66)}, "
              f"70F->{fahrenheit_to_level(70)}, 74F->{fahrenheit_to_level(74)}.")
        print("(read-only; no commands were sent)")
        await client.close()

    try:
        asyncio.run(_run())
    except Exception as exc:  # pragma: no cover - live network
        print(f"calibrate failed: {exc}")
        return 1
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from sleepctl.ml.dataset import export_csv, export_parquet
    from sleepctl.storage.repository import Repository

    repo = Repository(args.db)
    try:
        if args.format == "parquet":
            n = export_parquet(repo, args.out)
        else:
            n = export_csv(repo, args.out)
        print(f"Wrote {n} feature rows to {args.out} ({args.format}).")
    finally:
        repo.close()
    return 0


def _cmd_train(args: argparse.Namespace) -> int:
    """Refit the ML models and propose (or apply) the next setpoint."""
    from sleepctl.ml.recommend import recommend_action
    from sleepctl.storage.repository import Repository

    cfg = AppConfig.default()
    repo = Repository(args.db)
    try:
        active = repo.latest_setpoints() or cfg.default_setpoints()
        chosen = recommend_action(repo, active, cfg)
        if chosen is None:
            n = len(repo.all_nights())
            print(f"ML deferring to rule policy: need >= {cfg.ml.min_nights} clean nights "
                  f"and sufficient confidence (have {n} nights).")
            return 0
        print(f"ML chose: {chosen.name} (confidence {chosen.confidence:.2f})")
        print(f"  reason: {chosen.reason}")
        if chosen.predicted:
            keys = ["wake_events", "deep_pct", "avg_hrv", "sleep_efficiency"]
            preds = {k: round(chosen.predicted[k], 3) for k in keys if k in chosen.predicted}
            print(f"  predicted: {preds}")
        if chosen.name != "no_change":
            p = chosen.profile
            print(f"  -> setpoint v{p.version}: deep={p.deep_bias_f:.1f}F "
                  f"neutral={p.neutral_f:.1f}F rem_off=+{p.rem_warm_offset_f:.1f}F "
                  f"blend_a={p.composite_bed_weight:.2f}")
            if args.apply:
                repo.save_setpoints(chosen.profile)
                print(f"  applied: setpoint v{chosen.profile.version} is now active.")
            else:
                print("  (dry: re-run with --apply to persist)")
    finally:
        repo.close()
    return 0


def _cmd_checkin(args: argparse.Namespace) -> int:
    """Log subjective morning data (0-10) for a night."""
    from sleepctl.models import ContextRecord
    from sleepctl.storage.repository import Repository

    repo = Repository(args.db)
    try:
        date = args.date or datetime.now().date().isoformat()
        ctx = repo.get_context(date) or ContextRecord(date=date)
        if args.quality is not None:
            ctx.subjective_quality = args.quality
        if args.grogginess is not None:
            ctx.grogginess = args.grogginess
        if args.performance is not None:
            ctx.daytime_performance = args.performance
        repo.save_context(ctx)
        print(f"Logged check-in for {date}: quality={ctx.subjective_quality} "
              f"grogginess={ctx.grogginess} performance={ctx.daytime_performance}")
    finally:
        repo.close()
    return 0


def _cmd_recalibrate(args: argparse.Namespace) -> int:
    """Monthly: re-anchor baselines + report model confidence and learned setpoint."""
    from sleepctl.ml.confounders import clean_rows
    from sleepctl.ml.dataset import build_feature_rows
    from sleepctl.ml.model import SetpointModel
    from sleepctl.storage.repository import Repository

    cfg = AppConfig.default()
    repo = Repository(args.db)
    try:
        rows = build_feature_rows(repo)
        clean = clean_rows(rows)
        print(f"Nights: {len(rows)} total, {len(clean)} clean (non-confounded).")
        if len(clean) >= 3:
            model = SetpointModel(lam=cfg.ml.ridge_lambda).fit(clean)
            print(f"Model confidence: {model.confidence():.2f}; "
                  f"trained outcomes: {model.trained_outcomes()}")
        sp = repo.latest_setpoints()
        if sp:
            print(f"Learned setpoint v{sp.version} ({sp.source}): deep={sp.deep_bias_f:.1f}F "
                  f"neutral={sp.neutral_f:.1f}F blend_a={sp.composite_bed_weight:.2f}")
    finally:
        repo.close()
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

    p_nr = sub.add_parser("night-report",
                          help="Explainable nightly intelligence report (what/why/learned)")
    p_nr.add_argument("--db", default="sleepctl.db")
    p_nr.add_argument("--json", action="store_true", help="emit the full structured report as JSON")
    p_nr.set_defaults(func=_cmd_night_report)

    p_run = sub.add_parser("run", help="Run the live closed-loop controller")
    p_run.add_argument("--dry-run", action="store_true",
                       help="read-only: log decisions but send NO temperature commands")
    p_run.add_argument("--wake", default=None, metavar="HH:MM",
                       help="required wake time (manual, v1 schedule input)")
    p_run.add_argument("--poll-seconds", type=float, default=60.0)
    p_run.add_argument("--db", default="sleepctl.db")
    p_run.add_argument("--side", default=None, help="bed side: left|right")
    p_run.add_argument("--credentials", default=None, help="path to credentials JSON")
    p_run.add_argument("--max-ticks", type=int, default=None)
    p_run.add_argument("--simulate", action="store_true",
                       help="drive the daemon from the offline simulator (no device)")
    p_run.add_argument("--scenario", default="normal",
                       help="simulator scenario: normal|short_sleep|clustered_awakenings")
    p_run.add_argument("--no-weather", action="store_true",
                       help="disable outdoor-temperature (Open-Meteo) ambient awareness")
    p_run.add_argument("--lat", type=float, default=None, help="weather latitude (default Boston)")
    p_run.add_argument("--lon", type=float, default=None, help="weather longitude (default Boston)")
    p_run.set_defaults(func=_cmd_run)

    p_auth = sub.add_parser("auth", help="Store Eight Sleep credentials")
    p_auth.add_argument("--email", default=None)
    p_auth.add_argument("--password", default=None)
    p_auth.add_argument("--timezone", default=None)
    p_auth.add_argument("--side", default=None)
    p_auth.add_argument("--credentials", default=None, help="path to credentials JSON")
    p_auth.add_argument("--test", action="store_true", help="connect to verify after saving")
    p_auth.set_defaults(func=_cmd_auth)

    p_cal = sub.add_parser("calibrate", help="Read-only probe of the live Pod")
    p_cal.add_argument("--credentials", default=None, help="path to credentials JSON")
    p_cal.set_defaults(func=_cmd_calibrate)

    p_export = sub.add_parser("export", help="Export the ML-ready feature table")
    p_export.add_argument("--db", default="sleepctl.db")
    p_export.add_argument("--out", default="features.csv")
    p_export.add_argument("--format", choices=["csv", "parquet"], default="csv")
    p_export.set_defaults(func=_cmd_export)

    p_train = sub.add_parser("train", help="Refit ML models + propose/apply the next setpoint")
    p_train.add_argument("--db", default="sleepctl.db")
    p_train.add_argument("--apply", action="store_true", help="persist the proposed setpoint")
    p_train.set_defaults(func=_cmd_train)

    p_checkin = sub.add_parser("checkin", help="Log subjective morning data (0-10)")
    p_checkin.add_argument("--db", default="sleepctl.db")
    p_checkin.add_argument("--date", default=None, help="ISO date (default today)")
    p_checkin.add_argument("--quality", type=float, default=None)
    p_checkin.add_argument("--grogginess", type=float, default=None)
    p_checkin.add_argument("--performance", type=float, default=None)
    p_checkin.set_defaults(func=_cmd_checkin)

    p_recal = sub.add_parser("recalibrate", help="Monthly: re-anchor + report ML status")
    p_recal.add_argument("--db", default="sleepctl.db")
    p_recal.set_defaults(func=_cmd_recalibrate)

    p_bt = sub.add_parser("backtest",
                          help="Prove the closed loop beats no-control on a response-aware model")
    p_bt.add_argument("--nights", type=int, default=12)
    p_bt.add_argument("--scenario", default="normal")
    p_bt.add_argument("--seed", type=int, default=7)
    p_bt.set_defaults(func=_cmd_backtest)
    return parser


def _cmd_backtest(args) -> int:
    from sleepctl.eval.backtest import backtest, format_report
    rep = backtest(nights=args.nights, scenario=args.scenario, seed=args.seed)
    print(format_report(rep))
    d = rep["delta"]
    improved = d["wake_events"] < 0 and d["outcome_score"] > 0
    print("\n" + ("✓ closed loop improves the night vs no control"
                  if improved else "✗ no improvement — investigate"))
    return 0 if improved else 1


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
