# SleepController -- standalone diagnostic (Windows).
#
# Use this when the dashboard/API is DOWN and you can't hit /diag (if the API IS up, prefer
# GET /api/diag?token=...&format=json -- it's richer and reads losslessly). This script needs
# nothing but PowerShell: no API, no venv activation, no network call except an optional
# localhost health probe.
#
# Run it:
#   powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
#
# Then paste the WHOLE output to Claude (or a human helping you debug) -- it never prints
# secret values, only whether they're set.
$ErrorActionPreference = "Continue"
$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = (Get-Location).Path }  # best-effort if run from a checkout elsewhere
$run = Join-Path $Root ".run"

function Section($title) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkGray
    Write-Host $title -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkGray
}

Write-Host ("#" * 78) -ForegroundColor Yellow
Write-Host "# SleepController doctor.ps1 -- paste this ENTIRE output to Claude for diagnosis." -ForegroundColor Yellow
Write-Host "# Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')   Root: $Root" -ForegroundColor Yellow
Write-Host ("#" * 78) -ForegroundColor Yellow

# ------------------------------------------------------------------ git state
Section "GIT"
Push-Location $Root
try {
    $commit = git rev-parse --short HEAD 2>$null
    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    Write-Host "commit: $commit   branch: $branch"
    $dirty = git status -s 2>$null
    if ($dirty) {
        Write-Host "working tree is DIRTY (uncommitted local changes):"
        Write-Host $dirty
    } else {
        Write-Host "working tree is clean"
    }
} catch {
    Write-Host "git not available or $Root is not a git checkout: $_"
}
Pop-Location

# ------------------------------------------------------------------ process inventory
Section "PROCESSES (python.exe / node.exe / powershell.exe)"
try {
    $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.Name -in @("python.exe", "node.exe", "powershell.exe", "pwsh.exe") }
    if (-not $procs) {
        Write-Host "none found"
    } else {
        $procs | Sort-Object Name, ProcessId | ForEach-Object {
            $start = try { $_.CreationDate } catch { $null }
            Write-Host ("[{0,-14}] PID={1,-7} Start={2,-22} Cmd={3}" -f $_.Name, $_.ProcessId, $start, $_.CommandLine)
        }
        # Flag likely daemon/watchdog pileups explicitly -- this is the #1 real-world cause of
        # 504-hammering / duplicate-control bugs (see windows-watchdog.ps1's Ensure-Single-Daemon).
        $daemons = $procs | Where-Object { $_.CommandLine -and $_.CommandLine -match "run_daemon\.py" }
        if ($daemons.Count -gt 1) {
            Write-Host ""
            Write-Host "WARNING: $($daemons.Count) run_daemon.py processes running at once (should be exactly 1)." -ForegroundColor Red
        }
        $watchdogs = $procs | Where-Object { $_.CommandLine -and $_.CommandLine -match "windows-watchdog\.ps1" }
        if ($watchdogs.Count -gt 1) {
            Write-Host "WARNING: $($watchdogs.Count) windows-watchdog.ps1 processes running at once (should be exactly 1)." -ForegroundColor Red
        }
    }
} catch {
    Write-Host "could not enumerate processes (needs Get-CimInstance / WMI access): $_"
}

# ------------------------------------------------------------------ ports
Section "PORTS"
foreach ($port in 8000, 3000) {
    try {
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($listening) {
            $owners = $listening | Select-Object -ExpandProperty OwningProcess -Unique
            Write-Host "port $port : LISTENING (PID $($owners -join ', '))"
        } else {
            Write-Host "port $port : NOT LISTENING"
        }
    } catch {
        Write-Host "port $port : could not check ($_)"
    }
}

# ------------------------------------------------------------------ connectivity (LAN + tailscale)
Section "CONNECTIVITY"
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
          Select-Object -First 1).IPAddress
if (-not $lanIp) { $lanIp = "(no LAN IP found -- not on WiFi/Ethernet with a DHCP lease?)" }
Write-Host "LAN IP: $lanIp"
$port3000Listening = [bool](Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue)
Write-Host "port 3000 listening: $port3000Listening  (same-WiFi dashboard URL: http://${lanIp}:3000)"

if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    Write-Host ""
    Write-Host "-- tailscale status --"
    try {
        tailscale status 2>&1 | ForEach-Object { Write-Host $_ }
    } catch {
        Write-Host "(tailscale status failed: $_)"
    }
    Write-Host ""
    Write-Host "-- tailscale funnel status (public internet access, if enabled) --"
    try {
        tailscale funnel status 2>&1 | ForEach-Object { Write-Host $_ }
    } catch {
        Write-Host "(tailscale funnel status failed / funnel not enabled: $_)"
    }
    Write-Host ""
    Write-Host "-- tailscale serve status (tailnet-only access, if enabled) --"
    try {
        tailscale serve status 2>&1 | ForEach-Object { Write-Host $_ }
    } catch {
        Write-Host "(tailscale serve status failed / serve not enabled: $_)"
    }
    Write-Host ""
    Write-Host "(if the phone can't reach the dashboard off-WiFi: funnel/serve above should show an ACTIVE https:// URL --"
    Write-Host " if neither shows one, that -- not the app -- is why the phone can't connect.)"
} else {
    Write-Host "(tailscale CLI not found)"
}

# ------------------------------------------------------------------ supervisor + power
# The single most common "everything silently stopped days ago" cause is NOT a crash -- the
# watchdog auto-restarts those within ~15s and the Scheduled Task relaunches the watchdog itself
# within a minute. It's the box going away: powered off, or asleep on BATTERY (windows-always-on
# only disables sleep on AC), or the task being stopped/disabled/unregistered. None of that is
# visible in the logs -- the logs just END -- so print the supervisor and power state explicitly.
Section "SCHEDULED TASK 'SleepController' + POWER"
try {
    $task = Get-ScheduledTask -TaskName "SleepController" -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -TaskName "SleepController" -ErrorAction Stop
    Write-Host ("state          : {0}" -f $task.State)   # Ready | Running | Disabled
    Write-Host ("last run       : {0}" -f $info.LastRunTime)
    # 0 = the action exited cleanly; 0x41301 (267009) = "task is currently running" -- the normal,
    # healthy value here, because the watchdog is meant to never exit.
    $note = "(non-zero -- the watchdog exited; see watchdog.log tail below)"
    if ($info.LastTaskResult -eq 0) { $note = "(exited cleanly -- watchdog is NOT supervising)" }
    if ($info.LastTaskResult -eq 267009) { $note = "(running -- normal)" }
    Write-Host ("last result    : 0x{0:X} {1}" -f $info.LastTaskResult, $note)
    Write-Host ("next run       : {0}" -f $info.NextRunTime)
    Write-Host ("missed runs    : {0}" -f $info.NumberOfMissedRuns)
    if ($task.State -eq "Disabled") {
        Write-Host "TASK IS DISABLED -- nothing will start it. Fix: Enable-ScheduledTask SleepController" -ForegroundColor Red
    } elseif ($task.State -ne "Running") {
        Write-Host "TASK IS NOT RUNNING -- fix: Start-ScheduledTask SleepController" -ForegroundColor Red
    }
} catch {
    Write-Host "TASK NOT REGISTERED -- the controller will NOT survive a reboot." -ForegroundColor Red
    Write-Host "Fix: run scripts\windows-always-on.ps1 in an ADMINISTRATOR PowerShell."
}

Write-Host ""
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $boot = $os.LastBootUpTime
    $up = (Get-Date) - $boot
    Write-Host ("last boot      : {0}  (up {1}d {2}h {3}m)" -f $boot, $up.Days, $up.Hours, $up.Minutes)
    Write-Host "                 (if 'last boot' is RECENT but the health snapshot is DAYS old, the box"
    Write-Host "                  was off/asleep for that gap -- not a software fault.)"
} catch { Write-Host "last boot      : (could not read)" }
try {
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bat) {
        # BatteryStatus 2 = AC connected. Anything else means we're on battery, where the
        # always-on power policy does NOT apply and Windows will sleep the box out from under us.
        $onAc = ($bat.BatteryStatus -eq 2)
        Write-Host ("power          : {0} (charge {1}%)" -f $(if ($onAc) { "AC" } else { "ON BATTERY" }), $bat.EstimatedChargeRemaining)
        if (-not $onAc) {
            Write-Host "RUNNING ON BATTERY -- plug it in. Sleep is only disabled on AC, so an unplugged" -ForegroundColor Red
            Write-Host "laptop will suspend and the controller stops until something wakes it." -ForegroundColor Red
        }
    } else {
        Write-Host "power          : no battery detected (desktop/AC-only)"
    }
} catch { Write-Host "power          : (could not read)" }

# ------------------------------------------------------------------ heartbeats + logs
Section "HEARTBEATS (.run)"
function HeartbeatAge($name) {
    $path = Join-Path $run "$name.heartbeat"
    if (-not (Test-Path $path)) { Write-Host "$name.heartbeat : MISSING"; return }
    $lastWrite = (Get-Item $path).LastWriteTime
    $span = (Get-Date) - $lastWrite
    # A heartbeat measured in DAYS is the tell that the box was away, not that a process is slow --
    # don't bury that in a six-digit second count.
    $age = "{0}s" -f [int]$span.TotalSeconds
    if ($span.TotalMinutes -ge 5) {
        $age = "{0}d {1}h {2}m" -f $span.Days, $span.Hours, $span.Minutes
    }
    $line = "$name.heartbeat : last write $age ago  ($lastWrite)"
    if ($span.TotalMinutes -gt 5) { Write-Host $line -ForegroundColor Red } else { Write-Host $line }
}
if (-not (Test-Path $run)) {
    Write-Host "$run does not exist -- nothing has ever run here."
} else {
    HeartbeatAge "daemon"
    HeartbeatAge "watchdog"

    Write-Host ""
    $alertPath = Join-Path $run "watchdog.alert"
    if (Test-Path $alertPath) {
        Write-Host "watchdog.alert : ACTIVE -- $(Get-Content $alertPath -Raw)" -ForegroundColor Red
    } else {
        Write-Host "watchdog.alert : (none -- no active restart-storm or smoke-test failure)"
    }
    foreach ($resultFile in @("validate.result", "smoke.result", "health-publish.result",
                              "update.result", "webbuild.result")) {
        $p = Join-Path $run $resultFile
        if (Test-Path $p) {
            Write-Host "${resultFile} :"
            Get-Content -Path $p | ForEach-Object { Write-Host "    $_" }
        } else {
            Write-Host "${resultFile} : MISSING (watchdog hasn't run since this feature was added, or hasn't started yet)"
        }
    }
}

function TailLog($name, $n = 15) {
    Section "LOG TAIL: $name (last $n lines)"
    $path = Join-Path $run $name
    if (-not (Test-Path $path)) { Write-Host "(file not found)"; return }
    try {
        Get-Content -Path $path -Tail $n -ErrorAction Stop | ForEach-Object { Write-Host $_ }
    } catch {
        Write-Host "(could not read: $_)"
    }
}
TailLog "watchdog.log"
TailLog "daemon.log"
TailLog "daemon.err"
TailLog "daemon-crash.log"

# ------------------------------------------------------------------ deploy\.env sanity
Section "deploy\.env"
$envPath = Join-Path $Root "deploy\.env"
if (-not (Test-Path $envPath)) {
    Write-Host "MISSING -- run scripts\windows-setup.ps1 first."
} else {
    $vars = @{}
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') { $vars[$matches[1].Trim()] = $matches[2].Trim() }
    }
    # Non-secret flags: show the actual value -- they're config, not credentials.
    foreach ($k in @("SLEEPCTL_LIVE", "SLEEPCTL_DRY_RUN")) {
        $v = $vars[$k]
        Write-Host ("{0,-20} = {1}" -f $k, $(if ($v) { $v } else { "(unset)" }))
    }
    # Secrets: NEVER print the value, only whether it's present.
    foreach ($k in @("EIGHTSLEEP_EMAIL", "EIGHTSLEEP_PASSWORD", "DIAG_TOKEN", "CALENDAR_ICS_URL",
                     "DASHBOARD_PASSWORD", "JWT_SECRET", "GIT_PUSH_TOKEN", "HEALTHCHECKS_URL")) {
        $present = $vars.ContainsKey($k) -and $vars[$k]
        Write-Host ("{0,-20} = {1}" -f $k, $(if ($present) { "SET" } else { "MISSING" }))
    }
    # These two are why an outage can go unnoticed for days, so call them out rather than
    # leaving them as one more MISSING line in a list.
    if (-not ($vars.ContainsKey("HEALTHCHECKS_URL") -and $vars["HEALTHCHECKS_URL"])) {
        Write-Host "NO DEAD-MAN'S SWITCH: nothing off-box notices if this machine goes dark." -ForegroundColor Yellow
        Write-Host "  Fix: create a free healthchecks.io check and set HEALTHCHECKS_URL in deploy\.env."
    }
    if (-not ($vars.ContainsKey("GIT_PUSH_TOKEN") -and $vars["GIT_PUSH_TOKEN"])) {
        Write-Host "NO GIT_PUSH_TOKEN: the health snapshot / offsite backup pushes rely on whatever" -ForegroundColor Yellow
        Write-Host "  credential the Scheduled Task's user happens to have -- which is usually none."
    }
}

# ------------------------------------------------------------------ live health probe
Section "LIVE PROBE: http://localhost:8000/health"
try {
    $resp = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5 -ErrorAction Stop
    Write-Host "API IS UP: $($resp | ConvertTo-Json -Compress)"
} catch {
    Write-Host "API IS DOWN (or not responding): $_" -ForegroundColor Red
}

Section "DONE -- paste everything above to Claude"
