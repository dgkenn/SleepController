"""A slow daemon start-up must not be mistaken for a dead daemon (2026-09-25 outage).

The daemon beat only once its loop ran, and the watchdog judged tick progress against the
PREVIOUS process's last tick, so every fresh daemon was killed after 45 s and every smoke test
failed: no build could stay deployed. Pinned here: run_daemon.py beats from process start, and
the watchdog gives a fresh daemon a start-up allowance before judging tick progress."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_run_daemon_beats_before_anything_slow():
    src = (ROOT / "dashboard" / "daemon" / "run_daemon.py").read_text()
    main = src[src.index("def main() -> None:"):]
    beat = main.index("daemon-process-heartbeat")
    assert beat < main.index("ap = argparse.ArgumentParser()")
    assert beat < main.index("LiveDashboardDaemon(")


def test_watchdog_does_not_judge_tick_progress_during_start_up():
    ps = (ROOT / "scripts" / "windows-watchdog.ps1").read_text(encoding="utf-8")
    allowance = int(re.search(r"\$script:daemonStartupAllowanceS = (\d+)", ps).group(1))
    assert allowance >= 300
    wedged = ps[ps.index("function Daemon-Wedged {"):]
    wedged = wedged[:wedged.index("\n}\n")]
    assert "daemonStartedAt" in wedged.split("\n")[1]          # the first check in the function
    ensure = ps[ps.index("function Ensure-Daemon {"):]
    ensure = ensure[:ensure.index("\n}\n")]
    assert ensure.index("Start-Daemon | Out-Null") < ensure.index("$script:daemonStartedAt = (Get-Date)")
