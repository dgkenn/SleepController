# SleepController -- always-on WATCHDOG (Windows).
# Starts the API + control daemon + web PWA, then supervises them forever: any process that
# dies is restarted within ~15s. Designed to be launched at boot by a Scheduled Task
# (see windows-always-on.ps1). Survives crashes; the Scheduled Task survives reboots.
#
# Uses the PRODUCTION web server (next start) -- far more stable for 24/7 than `next dev`.
$ErrorActionPreference = "Continue"
$Root = Join-Path $HOME "SleepController"
Set-Location $Root

$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null
function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path (Join-Path $run "watchdog.log") -Value $line
    Write-Host $line
}

Log "watchdog starting (root=$Root)"

# --- restart-storm limiter + shared alert marker ---------------------------------------------
# Any single component that keeps dying and getting restarted is a sign of a real problem
# (bad env, crash loop, port fight) that a restart can't fix -- hammering it forever just burns
# the box and hides the real error. Track restart TIMESTAMPS per component; if a component is
# restarted more than $StormThreshold times within $StormWindowSeconds, STOP restarting it for
# $StormCooldownSeconds ("holding"), log a CRITICAL line, and write .run\watchdog.alert so a
# future remote-action / push-alert layer can surface it. After the cooldown we try once more;
# if it storms again it goes right back on hold. The marker is removed once nothing is holding.
$StormWindowSeconds = 300
$StormThreshold = 5
$StormCooldownSeconds = 300
$script:restartHistory = @{ api = @(); daemon = @(); web = @() }
$script:stormHold = @{ api = $null; daemon = $null; web = $null }
$script:alertFile = Join-Path $run "watchdog.alert"

function Write-Alert([string]$reason) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $reason
    Set-Content -Path $script:alertFile -Value $line -Encoding ASCII
}
function Clear-AlertIfNoneStorming {
    $now = Get-Date
    $anyHolding = $false
    foreach ($k in @($script:stormHold.Keys)) {
        if ($script:stormHold[$k] -ne $null -and $now -lt $script:stormHold[$k]) { $anyHolding = $true }
    }
    if (-not $anyHolding -and (Test-Path $script:alertFile)) {
        Remove-Item -Path $script:alertFile -Force -ErrorAction SilentlyContinue
        Log "all components healthy; cleared watchdog.alert"
    }
}
# Call once per restart DECISION for $component (i.e. right before you would otherwise restart
# it). Returns $true if the restart is allowed to proceed, $false if it's being held due to a
# storm. Handles cooldown expiry (one retry) and recording the attempt into the trailing window.
function Test-CanRestart([string]$component) {
    $now = Get-Date
    if ($script:stormHold[$component] -ne $null) {
        if ($now -lt $script:stormHold[$component]) { return $false }
        Log "$component cooldown elapsed; trying once more"
        $script:stormHold[$component] = $null
        $script:restartHistory[$component] = @()
    }
    $cutoff = $now.AddSeconds(-$StormWindowSeconds)
    $script:restartHistory[$component] = @($script:restartHistory[$component] | Where-Object { $_ -gt $cutoff })
    $script:restartHistory[$component] += $now
    $count = $script:restartHistory[$component].Count
    if ($count -gt $StormThreshold) {
        $winMin = [int]($StormWindowSeconds / 60)
        Log ("CRITICAL: RESTART STORM: {0} restarted {1} times in {2} min -- HOLDING, needs attention" -f $component, $count, $winMin)
        Write-Alert ("RESTART STORM: {0} restarted {1} times in {2} min" -f $component, $count, $winMin)
        $script:stormHold[$component] = $now.AddSeconds($StormCooldownSeconds)
        return $false
    }
    return $true
}
# Call when $component is observed healthy. Resets its storm-tracking state so a later blip
# starts counting fresh, and clears the shared alert file if nothing else is holding.
function Clear-StormState([string]$component) {
    if ($script:stormHold[$component] -ne $null -or $script:restartHistory[$component].Count -gt 0) {
        $script:stormHold[$component] = $null
        $script:restartHistory[$component] = @()
        Clear-AlertIfNoneStorming
    }
}

# --- remote-restart hook -----------------------------------------------------------------------
# A future remote-action endpoint drops a one-line flag file naming what to restart. The watchdog
# side of the protocol: each supervise iteration checks for it FIRST, stops the named component's
# process(es), deletes the flag, then lets the normal loop below notice it's down and restart it.
$script:restartRequestFile = Join-Path $run "restart.request"
function Stop-ComponentProcesses([string]$component) {
    switch ($component) {
        "api" {
            Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
                    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                }
        }
        "web" {
            Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
                    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                }
        }
        "daemon" {
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        }
    }
}
function Handle-RestartRequest {
    if (-not (Test-Path $script:restartRequestFile)) { return }
    $target = $null
    try { $target = (Get-Content -Path $script:restartRequestFile -Raw -ErrorAction Stop).Trim().ToLower() } catch {}
    Remove-Item -Path $script:restartRequestFile -Force -ErrorAction SilentlyContinue
    if (-not $target) { return }
    Log "restart requested: $target"
    switch ($target) {
        "all"    {
            Stop-ComponentProcesses "api"; Stop-ComponentProcesses "web"; Stop-ComponentProcesses "daemon"
            $script:daemonGraceUntil = (Get-Date)   # let Ensure-Daemon re-judge immediately, no grace wait
        }
        "api"    { Stop-ComponentProcesses "api" }
        "web"    { Stop-ComponentProcesses "web" }
        "daemon" { Stop-ComponentProcesses "daemon"; $script:daemonGraceUntil = (Get-Date) }
        default  { Log "restart requested: unknown target '$target' -- ignoring" }
    }
}

# --- load deploy\.env into the environment ---
$envPath = Join-Path $Root "deploy\.env"
if (-not (Test-Path $envPath)) { Log "FATAL: deploy\.env missing -- run windows-setup.ps1 first."; exit 1 }
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { Set-Item -Path ("env:" + $matches[1].Trim()) -Value $matches[2].Trim() }
}
$env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Log "FATAL: venv python missing ($py) -- run windows-setup.ps1."; exit 1 }
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) { $npm = "npm.cmd" }  # fall back to PATH

# --- boot-time config validation: fail LOUDLY now, not silently at 3am -------------------------
# Never blocks startup (a broken deploy is easier to fix with the dashboard at least attempting
# to come up) -- but a FATAL result is logged as CRITICAL and raises .run\watchdog.alert so it's
# impossible to miss.
Log "running startup config validation (scripts\validate_env.ps1)"
$validateScript = Join-Path $Root "scripts\validate_env.ps1"
if (Test-Path $validateScript) {
    & $validateScript -Root $Root
    $validateExit = $LASTEXITCODE
    $validateResultPath = Join-Path $run "validate.result"
    if (Test-Path $validateResultPath) {
        Get-Content $validateResultPath | ForEach-Object { Log "validate_env: $_" }
    }
    if ($validateExit -ge 2) {
        Log "CRITICAL: validate_env reported FATAL config problems -- starting anyway, but this needs attention"
        Write-Alert "validate_env FATAL at startup -- see .run\validate.result"
    } elseif ($validateExit -eq 1) {
        Log "validate_env: WARN (non-fatal) -- see .run\validate.result for details"
    } else {
        Log "validate_env: PASS"
    }
} else {
    Log "WARN: scripts\validate_env.ps1 not found -- skipping startup validation"
}

# --- open port 3000 on the Private network (best-effort; needs admin) ---
try {
    if (-not (Get-NetFirewallRule -DisplayName "SleepController 3000" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "SleepController 3000" -Direction Inbound `
            -LocalPort 3000 -Protocol TCP -Action Allow -Profile Private -ErrorAction Stop | Out-Null
        Log "added firewall rule for port 3000"
    }
} catch { Log "firewall rule skipped (run once as admin if the phone can't connect): $_" }

# --- clean up ORPHANS from a previous run (they'd hold ports 8000/3000 and block the fresh
# start, and would still be serving stale code/env). The watchdog runs elevated via the task,
# so it can kill them even when a normal shell can't. This makes a plain task restart clean --
# no reboot needed to pick up new code or a changed deploy\.env.
foreach ($port in 8000, 3000) {
    try {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
                Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                Log "cleaned up stale process $_ on port $port"
            }
    } catch {}
}
# The control daemon holds NO listening port, so the port sweep above misses it. Kill any stale
# run_daemon.py from a previous run too -- otherwise it survives the restart and the supervise
# loop would see a "live" daemon running old code / a stale deploy\.env.
try {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Log "cleaned up stale daemon process $($_.ProcessId)"
        }
} catch {}

# --- one-time prep: DB + login user, and a PRODUCTION web build if missing ---
Log "preparing database + login user"
& $py -c "from app.db import connect; from app.security import ensure_bootstrap_user; connect(); ensure_bootstrap_user(); print('db ready')" 2>&1 | ForEach-Object { Log "db: $_" }

if (-not (Test-Path (Join-Path $Root "dashboard\web\.next"))) {
    Log "building the web app for production (first run; this takes a few minutes)"
    Push-Location (Join-Path $Root "dashboard\web")
    & $npm run build *>> (Join-Path $run "web-build.log")
    Pop-Location
    Log "web build complete"
}

# --- service starters (each returns a Process via -PassThru) ---
function Start-Api {
    Start-Process -FilePath $py -WindowStyle Hidden -PassThru `
        -ArgumentList @("-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--app-dir","dashboard\api") `
        -RedirectStandardOutput "$run\api.log" -RedirectStandardError "$run\api.err"
}
function Start-Daemon {
    # live mode + dry-run come from deploy\.env (SLEEPCTL_LIVE / SLEEPCTL_DRY_RUN)
    Start-Process -FilePath $py -WindowStyle Hidden -PassThru `
        -ArgumentList @("dashboard\daemon\run_daemon.py") `
        -RedirectStandardOutput "$run\daemon.log" -RedirectStandardError "$run\daemon.err"
}
function Start-Web {
    $env:API_URL = "http://localhost:8000"; $env:PORT = "3000"
    Start-Process -FilePath $npm -WindowStyle Hidden -PassThru `
        -WorkingDirectory (Join-Path $Root "dashboard\web") `
        -ArgumentList @("run","start","--","-H","0.0.0.0","-p","3000") `
        -RedirectStandardOutput "$run\web.log" -RedirectStandardError "$run\web.err"
}

# --- supervise by REALITY -------------------------------------------------------------------
# api/web: their listening port. Daemon: a HEARTBEAT FILE it rewrites every ~2s. The previous
# approaches (a Start-Process handle, then a CIM command-line query) both FLAPPED in the
# scheduled-task context -- the query intermittently returned 0 or 2 for one healthy daemon, so
# the watchdog spuriously started duplicates and then killed a working daemon (and the in-progress
# self-test) every ~20s. A file's mtime is unambiguous: fresh (< 90s) => the daemon is alive.
function Port-Alive([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}
$script:daemonHb = Join-Path $run "daemon.heartbeat"
$script:daemonGraceUntil = (Get-Date)   # after a (re)start, wait this long for the first beat
function Daemon-Alive {
    if (-not (Test-Path $script:daemonHb)) { return $false }
    return ((New-TimeSpan -Start (Get-Item $script:daemonHb).LastWriteTime -End (Get-Date)).TotalSeconds -lt 90)
}
function Ensure-Daemon {
    if ((Get-Date) -lt $script:daemonGraceUntil) { return }   # let a fresh daemon connect + beat
    if (Daemon-Alive) { Clear-StormState "daemon"; return }
    if (-not (Test-CanRestart "daemon")) { return }   # storming -- held; surfaced via CRITICAL log + alert
    $age = -1
    if (Test-Path $script:daemonHb) {
        $age = [int]((New-TimeSpan -Start (Get-Item $script:daemonHb).LastWriteTime -End (Get-Date)).TotalSeconds)
    }
    $ageDesc = if ($age -ge 0) { "daemon heartbeat ${age}s stale" } else { "daemon heartbeat missing" }
    Log ("{0}; restarting (restart #{1} in window)" -f $ageDesc, $script:restartHistory["daemon"].Count)
    # best-effort: clear any lingering run_daemon before starting a clean one
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    Start-Daemon | Out-Null
    $script:daemonGraceUntil = (Get-Date).AddSeconds(45)  # don't re-judge until it can beat
}

# --- post-restart smoke test --------------------------------------------------------------------
# Once things have had ~40s to come up, verify the whole stack actually works end to end -- this
# catches a broken deploy (bad build, import error, wrong port, dead creds) immediately instead of
# discovering it hours later. Runs exactly once, on whichever supervise pass crosses the 40s mark,
# so it never blocks the loop with an up-front sleep.
function Invoke-SmokeTest {
    $failures = @()
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5 -ErrorAction Stop
        if (-not $resp.ok) { $failures += "api /health responded but ok!=true" }
    } catch {
        $failures += "api /health unreachable: $($_.Exception.Message)"
    }
    if (-not (Daemon-Alive)) { $failures += "daemon heartbeat stale/missing" }
    if (-not (Port-Alive 3000)) { $failures += "web not listening on port 3000" }

    $resultPath = Join-Path $run "smoke.result"
    if ($failures.Count -eq 0) {
        Set-Content -Path $resultPath -Value "SMOKE PASS" -Encoding ASCII
        Log "smoke test: SMOKE PASS"
    } else {
        $msg = "SMOKE FAIL: " + ($failures -join "; ")
        Set-Content -Path $resultPath -Value $msg -Encoding ASCII
        Log "smoke test: $msg"
        Write-Alert $msg
    }
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
       Select-Object -First 1).IPAddress
Log "supervising; iPhone URL (same WiFi): http://${ip}:3000  login=$($env:DASHBOARD_USER)  live=$($env:SLEEPCTL_LIVE) dry_run=$($env:SLEEPCTL_DRY_RUN)"

$script:smokeTestAt = (Get-Date).AddSeconds(40)
$script:smokeTestDone = $false

while ($true) {
    Handle-RestartRequest

    if (-not (Port-Alive 8000)) {
        if (Test-CanRestart "api") {
            Log ("api not listening; starting (restart #{0} in window)" -f $script:restartHistory["api"].Count)
            Start-Api | Out-Null; Start-Sleep -Seconds 3
        }
    } else {
        Clear-StormState "api"
    }

    Ensure-Daemon

    if (-not (Port-Alive 3000)) {
        if (Test-CanRestart "web") {
            Log ("web not listening; starting (restart #{0} in window)" -f $script:restartHistory["web"].Count)
            Start-Web | Out-Null; Start-Sleep -Seconds 3
        }
    } else {
        Clear-StormState "web"
    }

    if (-not $script:smokeTestDone -and (Get-Date) -ge $script:smokeTestAt) {
        Invoke-SmokeTest
        $script:smokeTestDone = $true
    }

    Set-Content -Path (Join-Path $run "watchdog.heartbeat") -Value (Get-Date -Format o)
    Start-Sleep -Seconds 15
}
