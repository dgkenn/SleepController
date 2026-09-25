"""Self-update rollback goes to a commit that has RUN healthy, and the smoke test judges ticks.

2026-09-25: the start-up fix deployed at 14:51 failed its smoke test (the daemon was still
starting) and was rolled back to the previous HEAD, 81897bd, which had the very stall the fix
removed, so the box stayed down. Pinned here:

- a commit becomes last-known-good only after api, web, a fresh daemon heartbeat AND advancing
  runtime_state ticks for a sustained period, and a failed smoke test rolls back to it (the
  pre-update commit only when none is recorded), never onto the failing commit itself;
- the smoke test waits out the daemon's start-up allowance for ticks instead of failing at 40 s,
  but still fails a daemon that dies, is restarted mid-test, or never ticks;
- a rolled-back commit is not auto-redeployed ten minutes later.

There is no PowerShell in CI, so the script is checked as source and the decision rules are
mirrored in Python.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS = (ROOT / "scripts" / "windows-watchdog.ps1").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    body = PS[PS.index(f"function {name}"):]
    return body[:body.index("\n}\n")]


def test_watchdog_stays_ascii():
    bad = [(i + 1, ln) for i, ln in enumerate(PS.splitlines()) if any(ord(c) > 127 for c in ln)]
    assert not bad, bad[:3]


# --------------------------------------------------------------------------- last-known-good
def test_last_known_good_needs_a_sustained_healthy_run_with_advancing_ticks():
    minutes = int(re.search(r"\$script:lkgHealthyMinutes = (\d+)", PS).group(1))
    assert 10 <= minutes <= 30
    fn = _fn("Update-LastKnownGood")
    for need in ("Port-Alive 8000", "Port-Alive 3000", "Daemon-Alive", "Get-DaemonTickStamp",
                 "$stamp -ne $script:lkgLastStamp", "$script:lkgTickFreshS",
                 "$null -eq $script:pendingRollback"):
        assert need in fn, need
    # any unhealthy check restarts the clock; a new HEAD restarts it too
    assert "if (-not $healthy) { $script:lkgHealthySince = $null; return }" in fn
    assert "$script:lkgSha = $head; $script:lkgHealthySince = $null" in fn
    assert "Set-Content -Path $script:lkgFile" in fn
    # checked every supervise pass (throttled inside)
    loop = PS[PS.index("while ($true) {"):]
    assert "Update-LastKnownGood" in loop


def test_rollback_prefers_last_known_good_and_never_resets_onto_the_failing_commit():
    fn = _fn("Invoke-DeployRollback")
    assert fn.index("Read-LastKnownGood") < fn.index('$source = "pre-update commit"')
    assert "cat-file -e" in fn                         # a pruned LKG falls back, not a failed reset
    guard = fn.index("$target -eq $failing")
    assert guard < fn.index("reset --hard $target")
    assert "Write-Alert" in fn[guard:fn.index("reset --hard $target")]
    assert "reset --hard $rb.priorSha" not in fn
    # the failing commit is recorded so the auto-updater does not redeploy it
    assert fn.index("Set-Content -Path $script:deployFailedFile") < fn.index("reset --hard $target")


def _rollback_target(lkg, lkg_present, prior, failing):
    """Python mirror of Invoke-DeployRollback's choice: (action, target)."""
    target = lkg if (lkg and lkg_present) else None
    if not target and prior:
        target = prior
    if not target:
        return "alert", None
    if failing and target == failing:
        return "alert", target
    return "reset", target


def test_rollback_target_rules():
    # the 2026-09-25 case: the previous HEAD had the stall; the recorded good commit did not
    assert _rollback_target("good", True, "81897bd", "fix") == ("reset", "good")
    # nothing recorded yet: today's behaviour
    assert _rollback_target(None, False, "81897bd", "fix") == ("reset", "81897bd")
    # recorded commit no longer in the repository: pre-update commit
    assert _rollback_target("gone", False, "81897bd", "fix") == ("reset", "81897bd")
    # the target IS the failing commit: alert, do not loop
    assert _rollback_target("fix", True, "81897bd", "fix") == ("alert", "fix")
    assert _rollback_target(None, False, "fix", "fix") == ("alert", "fix")
    assert _rollback_target(None, False, None, "fix") == ("alert", None)


def test_update_arms_rollback_with_the_deployed_commit():
    assert "deployedSha = (Get-HeadSha)" in _fn("Handle-UpdateRequest")


def test_auto_update_does_not_redeploy_a_rolled_back_commit():
    fn = _fn("Check-AutoUpdate")
    skip = fn.index("$failed.sha -eq $remote")
    assert skip < fn.index("Set-Content -Path $script:updateRequestFile")
    assert "$script:deployFailedRetryHours" in fn
    # a later pass of the same commit clears the block
    assert "Remove-Item -Path $script:deployFailedFile" in _fn("Invoke-SmokeTest")


# --------------------------------------------------------------------------- smoke test
def test_smoke_test_waits_for_ticks_within_the_start_up_allowance():
    fn = _fn("Invoke-SmokeTest")
    assert '"daemon heartbeat stale/missing"' in fn   # a dead daemon still fails at once
    assert "Test-SmokeDaemonTicks" in fn
    # it only waits when nothing else failed, and re-arms itself instead of being marked done
    assert "if ($waiting -and $failures.Count -eq 0) {" in fn
    assert "$script:smokeTestDone = $false" in fn
    loop = PS[PS.index("while ($true) {"):]
    smoke = loop[loop.index("Invoke-SmokeTest"):]
    assert "$script:smokeTestDone = $true" not in smoke[:smoke.index("Update-LastKnownGood")]
    ticks = _fn("Test-SmokeDaemonTicks")
    assert "$p.changes -ge 2" in ticks                 # the start-up stamp alone is not a tick
    assert "$p.startedAt -ne $script:daemonStartedAt" in ticks
    assert "$script:daemonStartupAllowanceS" in ticks
    # every full restart starts a fresh probe
    assert "$script:smokeProbe = $null" in _fn("Handle-RestartRequest")


def _smoke(passes, allowance=600):
    """Python mirror of Test-SmokeDaemonTicks over successive passes.

    ``passes`` is a list of (seconds since the daemon started, daemon start id, tick stamp)."""
    probe = None
    for up_s, started, stamp in passes:
        if probe is None:
            probe = {"started": started, "stamp": stamp, "changes": 0}
            verdict = "wait"
            continue
        if probe["started"] != started:
            return "fail: restarted"
        if stamp and stamp != probe["stamp"]:
            probe["changes"] += 1
            probe["stamp"] = stamp
        if probe["changes"] >= 2:
            return "pass"
        verdict = "fail: no ticks" if up_s >= allowance else "wait"
        if verdict != "wait":
            return verdict
    return verdict


def test_smoke_rules():
    # slow start-up: only the start-up stamp for minutes, then real ticks -> pass
    assert _smoke([(40, 1, "old"), (60, 1, "stamp")] + [(t, 1, "stamp") for t in range(80, 400, 20)]
                  + [(400, 1, "t1"), (420, 1, "t2")]) == "pass"
    # the start-up stamp alone never passes, and the allowance ends the wait
    assert _smoke([(40, 1, "old"), (60, 1, "stamp")] + [(t, 1, "stamp") for t in range(80, 620, 20)]) \
        == "fail: no ticks"
    # a daemon that dies and is restarted by Ensure-Daemon mid-test fails
    assert _smoke([(40, 1, "old"), (60, 1, "stamp"), (20, 2, "stamp2")]) == "fail: restarted"
    # a healthy start passes within a couple of re-checks
    assert _smoke([(40, 1, "t1"), (60, 1, "t2"), (80, 1, "t3")]) == "pass"


def test_a_killed_daemon_leaves_no_heartbeat_to_be_judged():
    stop = _fn("Stop-ComponentProcesses")
    daemon = stop[stop.index('"daemon" {'):]
    assert "Clear-DaemonHeartbeat" in daemon
    assert "Remove-Item" in _fn("Clear-DaemonHeartbeat")
    # the start-up orphan sweep too, and the helper exists before that script-level code runs
    sweep = PS.index("cleaned up stale daemon process")
    assert PS.index("function Clear-DaemonHeartbeat") < sweep
    assert "Clear-DaemonHeartbeat" in PS[sweep:sweep + 400]
