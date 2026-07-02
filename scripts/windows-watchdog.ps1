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
    if (-not (Daemon-Alive)) {
        Log "daemon heartbeat stale/missing; restarting"
        # best-effort: clear any lingering run_daemon before starting a clean one
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
        Start-Daemon | Out-Null
        $script:daemonGraceUntil = (Get-Date).AddSeconds(45)  # don't re-judge until it can beat
    }
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
       Select-Object -First 1).IPAddress
Log "supervising; iPhone URL (same WiFi): http://${ip}:3000  login=$($env:DASHBOARD_USER)  live=$($env:SLEEPCTL_LIVE) dry_run=$($env:SLEEPCTL_DRY_RUN)"

while ($true) {
    if (-not (Port-Alive 8000)) { Log "api not listening; starting"; Start-Api | Out-Null; Start-Sleep -Seconds 3 }
    Ensure-Daemon
    if (-not (Port-Alive 3000)) { Log "web not listening; starting"; Start-Web | Out-Null; Start-Sleep -Seconds 3 }
    Set-Content -Path (Join-Path $run "watchdog.heartbeat") -Value (Get-Date -Format o)
    Start-Sleep -Seconds 15
}
