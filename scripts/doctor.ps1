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

# ------------------------------------------------------------------ heartbeats + logs
Section "HEARTBEATS (.run)"
function HeartbeatAge($name) {
    $path = Join-Path $run "$name.heartbeat"
    if (-not (Test-Path $path)) { Write-Host "$name.heartbeat : MISSING"; return }
    $lastWrite = (Get-Item $path).LastWriteTime
    $age = [int]((Get-Date) - $lastWrite).TotalSeconds
    Write-Host "$name.heartbeat : last write ${age}s ago  ($lastWrite)"
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
    foreach ($resultFile in @("validate.result", "smoke.result")) {
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
                     "DASHBOARD_PASSWORD", "JWT_SECRET")) {
        $present = $vars.ContainsKey($k) -and $vars[$k]
        Write-Host ("{0,-20} = {1}" -f $k, $(if ($present) { "SET" } else { "MISSING" }))
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
