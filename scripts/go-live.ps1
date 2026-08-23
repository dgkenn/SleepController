# SleepController -- ONE COMMAND to bring the Verity online. Run this and nothing else:
#
#   powershell -ExecutionPolicy Bypass -File scripts\go-live.ps1
#
# It does, in the order that actually works:
#   0. UPDATES THE CHECKOUT FIRST, then re-runs itself if this script changed. This step exists
#      because of a trap that is very easy to fall into: a box that has been off for a while is
#      running old scripts, so running "the setup script" runs the OLD setup script -- the one
#      without the ingest-token minting or the streaming verification. You would follow the
#      instructions exactly and still end up with a silently dead feed.
#   1. Checks the machine is fit to run at all (venv, deploy\.env, disk, power).
#   2. Makes sure the always-on Scheduled Task exists and the api/daemon/web stack is UP --
#      the Verity forwarder is launched BY the watchdog, so a dead watchdog means no sensor
#      however well the armband is paired.
#   3. Runs the Verity setup (BLE library, scan, ingest token, enable, wait for real samples).
#   4. Runs the preflight and prints the GO / NO-GO verdict for tonight.
#   5. Prints exactly how to reach it: the dashboard login and every LAN URL your phone could
#      open right now -- so this one command is also the answer to "what's the URL and password".
#
# Safe to re-run at any time: every step is idempotent, and nothing here touches the Pod.
[CmdletBinding()]
param(
    # Set by the re-exec in step 0 so the refreshed copy doesn't pull-and-re-exec forever.
    [switch]$SkipUpdate,
    # Pod-only night: don't treat a silent Verity as blocking in the final verdict.
    [switch]$NoSensor
)
$ErrorActionPreference = "Continue"

$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = Split-Path -Parent $PSScriptRoot }
$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null
$py = Join-Path $Root ".venv\Scripts\python.exe"

$script:failed = @()
function Step($n, $title) {
    Write-Host ""
    Write-Host ("=" * 74) -ForegroundColor DarkGray
    Write-Host "  STEP $n -- $title" -ForegroundColor Cyan
    Write-Host ("=" * 74) -ForegroundColor DarkGray
}
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Info($m) { Write-Host "  [..]   $m" }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:failed += $m }

Write-Host ""
Write-Host "  SleepController -- go live" -ForegroundColor Cyan
Write-Host "  root: $Root"

# ---------------------------------------------------------------- 0. update, then re-exec
Step 0 "Update the checkout"
if ($SkipUpdate) {
    Info "already updated (re-executed after pull)"
} else {
    $self = $PSCommandPath
    $before = if (Test-Path $self) { (Get-FileHash $self -Algorithm SHA256).Hash } else { "" }

    Push-Location $Root
    $dirty = (& git status --porcelain 2>$null)
    if ($dirty) {
        Warn "the working tree has local edits; pulling anyway (git will refuse if it conflicts)"
    }
    $branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
    Info "pulling origin/$branch ..."
    & git pull --ff-only origin $branch 2>&1 | ForEach-Object { Write-Host "         $_" }
    if ($LASTEXITCODE -ne 0) {
        Warn "git pull failed -- continuing with the checkout as it is. If this box has been off"
        Warn "for a while, some of the steps below may be running older logic than expected."
    } else {
        Ok "checkout updated"
    }
    Pop-Location

    $after = if (Test-Path $self) { (Get-FileHash $self -Algorithm SHA256).Hash } else { "" }
    if ($before -and $after -and $before -ne $after) {
        Write-Host ""
        Write-Host "  This script was itself updated by the pull -- re-running the NEW version." -ForegroundColor Yellow
        $argsList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $self, "-SkipUpdate")
        if ($NoSensor) { $argsList += "-NoSensor" }
        & powershell @argsList
        exit $LASTEXITCODE
    }
}

# ---------------------------------------------------------------- 1. is this box fit to run
Step 1 "Machine prerequisites"
if (-not (Test-Path $py)) {
    Bad "venv python missing ($py). Run scripts\windows-setup.ps1 first, then re-run this."
} else { Ok "venv python present" }

$envPath = Join-Path $Root "deploy\.env"
if (-not (Test-Path $envPath)) {
    Bad "deploy\.env missing. Run scripts\windows-setup.ps1 first, then re-run this."
} else { Ok "deploy\.env present" }

# Power: the always-on policy only disables sleep on AC, so an unplugged laptop suspends and
# takes the controller (and the sensor feed) with it -- overnight, silently.
try {
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bat -and $bat.BatteryStatus -ne 2) {
        Warn "RUNNING ON BATTERY -- plug it in. Sleep is only disabled on AC, so an unplugged"
        Warn "laptop suspends overnight and the feed stops."
    } elseif ($bat) { Ok "on AC power" }
} catch {}

# Disk: the DB, the logs and the BLE stack all need room; a full disk fails WRITES while deletes
# still succeed, which presents as the controller mysteriously losing data.
try {
    $drive = Get-PSDrive -Name ($Root.Substring(0, 1)) -ErrorAction SilentlyContinue
    if ($drive -and $drive.Free -lt 2GB) {
        Warn ("only {0:N1} GB free on {1}: -- free some space before an overnight run" -f ($drive.Free / 1GB), $drive.Name)
    } elseif ($drive) { Ok ("{0:N1} GB free on {1}:" -f ($drive.Free / 1GB), $drive.Name) }
} catch {}

if ($script:failed.Count -gt 0) {
    Write-Host ""
    Write-Host "  Cannot continue until the FAIL items above are fixed." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 2. the stack must be running
Step 2 "Controller stack"
# The forwarder is launched BY the watchdog. A perfectly paired armband streams into nothing if
# the watchdog isn't running, so this is checked BEFORE the sensor rather than after.
$taskOk = $false
try {
    $task = Get-ScheduledTask -TaskName "SleepController" -ErrorAction Stop
    Info "Scheduled Task state: $($task.State)"
    if ($task.State -eq "Disabled") {
        Info "task is disabled -- enabling"
        Enable-ScheduledTask -TaskName "SleepController" -ErrorAction SilentlyContinue | Out-Null
    }
    if ((Get-ScheduledTask -TaskName "SleepController").State -ne "Running") {
        Info "starting the task"
        Start-ScheduledTask -TaskName "SleepController" -ErrorAction SilentlyContinue
    }
    $taskOk = $true
    Ok "always-on task present"
} catch {
    Warn "the 'SleepController' Scheduled Task is NOT registered -- the controller will not"
    Warn "survive a reboot, and nothing will launch the Verity forwarder."
    Warn "Fix (one-time, needs an ADMINISTRATOR PowerShell):"
    Warn "    powershell -ExecutionPolicy Bypass -File scripts\windows-always-on.ps1"
}

Info "waiting for the API to answer on 127.0.0.1:8000 (up to 90s)..."
$apiUp = $false
foreach ($i in 1..18) {
    try {
        Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 3 -ErrorAction Stop | Out-Null
        $apiUp = $true; break
    } catch { Start-Sleep -Seconds 5 }
}
if ($apiUp) { Ok "API is up" }
else {
    Warn "the API did not come up. The Verity steps below will still configure things, but"
    Warn "nothing will ingest until it does. Check .run\api.err and .run\watchdog.log."
}

$hb = Join-Path $run "daemon.heartbeat"
if (Test-Path $hb) {
    $age = [int]((Get-Date) - (Get-Item $hb).LastWriteTime).TotalSeconds
    if ($age -lt 120) { Ok "control daemon heartbeat is fresh (${age}s)" }
    else { Warn "daemon heartbeat is ${age}s old -- the daemon may not be running" }
} else {
    Warn "no daemon heartbeat yet (it may still be starting)"
}

# ---------------------------------------------------------------- 3. the sensor itself
Step 3 "Polar Verity Sense"
$verity = Join-Path $Root "scripts\verity-setup.ps1"
if (-not (Test-Path $verity)) {
    Bad "scripts\verity-setup.ps1 not found -- the pull in step 0 did not bring it in."
} else {
    Write-Host "  Put the armband ON your upper forearm and single-press the button" -ForegroundColor Cyan
    Write-Host "  until the LED shows the Bluetooth/HR mode (blue), then press Enter." -ForegroundColor Cyan
    [void](Read-Host "  ready?")
    & powershell -NoProfile -ExecutionPolicy Bypass -File $verity
}

# ---------------------------------------------------------------- 3b. every sensor channel
Step "3b" "Sensor channels"
# Each channel is OPTIONAL by design, so a silent one looks exactly like a broken one and both
# look exactly like one that was never set up. This separates them: PATH = does the code work,
# LIVE = is data actually arriving.
$verify = Join-Path $Root "scripts\verify_sensors.py"
if (Test-Path $verify) {
    $prevPP = $env:PYTHONPATH
    $env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
    $vdb = $null
    if (Test-Path $envPath) {
        Get-Content $envPath | ForEach-Object {
            if ($_ -match '^\s*SLEEPCTL_DB\s*=\s*(.+)$') { $vdb = $matches[1].Trim() }
        }
    }
    Push-Location $Root
    if ($vdb) { & $py $verify --db $vdb } else { & $py $verify --paths-only }
    Pop-Location
    $env:PYTHONPATH = $prevPP
} else {
    Warn "scripts\verify_sensors.py not found -- skipping the per-channel report"
}

# ---------------------------------------------------------------- 4. the verdict
Step 4 "Tonight's verdict"
$prev = $env:PYTHONPATH
$env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
$dbPath = $null
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*SLEEPCTL_DB\s*=\s*(.+)$') { $dbPath = $matches[1].Trim() }
    }
}
$pfArgs = @("-m", "sleepctl.cli", "preflight")
if ($dbPath) { $pfArgs += @("--db", $dbPath) }
if ($NoSensor) { $pfArgs += "--no-sensor" }
Push-Location $Root
& $py @pfArgs
$verdictCode = $LASTEXITCODE
Pop-Location
$env:PYTHONPATH = $prev

Write-Host ""
Write-Host ("=" * 74) -ForegroundColor DarkGray
if ($verdictCode -eq 0) {
    Write-Host "  READY. Sleep on it -- the controller will steer tonight." -ForegroundColor Green
    Write-Host "  In the morning:  sleepctl checkin        (the felt-recovery learners need it)"
} else {
    Write-Host "  NOT READY -- see the BLOCKING list above." -ForegroundColor Red
    Write-Host "  Most useful next step:  powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1"
}
Write-Host ("=" * 74) -ForegroundColor DarkGray

# ---------------------------------------------------------------- 5. how to reach it
# Printed EVERY run, GO or NO-GO -- "where's the URL, what's the password" is the single most
# common thing to lose track of between nights, and until now the only answer was "reread
# deploy\.env" or "scroll back to whenever windows-setup.ps1 first printed it". Both credentials
# and every LAN address the phone could actually reach live here, every time.
Step 5 "How to reach it"
$dashUser = $null; $dashPass = $null
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*DASHBOARD_USER\s*=\s*(.+)$')     { $dashUser = $matches[1].Trim() }
        if ($_ -match '^\s*DASHBOARD_PASSWORD\s*=\s*(.+)$') { $dashPass = $matches[1].Trim() }
    }
}
if ($dashUser -and $dashPass) {
    Write-Host "  Login:  $dashUser  /  $dashPass" -ForegroundColor Green
} else {
    Bad "no DASHBOARD_USER/DASHBOARD_PASSWORD in deploy\.env -- run scripts\windows-setup.ps1 first"
}

$addrs = @()
try {
    $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and
            $_.PrefixOrigin -ne 'WellKnown'
        }
} catch { }
if ($addrs) {
    Write-Host "  URL (same WiFi as this PC):"
    foreach ($a in $addrs) {
        Write-Host "    https://$($a.IPAddress)/   ($($a.InterfaceAlias))" -ForegroundColor Green
    }
    Write-Host "  First visit: the browser will warn about the certificate -- accept it, that's"
    Write-Host "  expected (it's self-signed, not a real threat on your own LAN)."
} else {
    Warn "couldn't detect a LAN IPv4 address -- check Windows network settings"
}
Write-Host "  Check 'remember me' at login -- the session is persistent, so you only do this once."
Write-Host "  Off-WiFi access (phone on cellular): deploy\tunnel.sh gives a public HTTPS URL, but"
Write-Host "  it needs bash (WSL or Git Bash) -- see deploy\REMOTE_ACCESS.md for that and for a"
Write-Host "  STABLE address (Tailscale / a named Cloudflare tunnel) instead of one that rotates."

Write-Host ""
Write-Host ("=" * 74) -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Once the water loop is confirmed healthy, the two in-bed calibrations are what"
Write-Host "  turn the generic presets into YOUR numbers -- see docs\PREFLIGHT_AND_MONITORING.md."
Write-Host ""
exit $verdictCode
