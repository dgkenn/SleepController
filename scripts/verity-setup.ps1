# SleepController -- one-command Polar Verity Sense setup (Windows).
#
# Run this ONCE, after you've paired/charged the Verity, to enable the dedicated cardiac sensor:
#
#   powershell -ExecutionPolicy Bypass -File scripts\verity-setup.ps1
#
# It: (1) installs the BLE library, (2) scans to confirm the sensor is discoverable, (3) flips
# SLEEPCTL_VERITY=1 in deploy\.env, and (4) nudges the watchdog to restart so it launches the
# forwarder. After this, HR + HRV stream into the controller and show on the dashboard's
# "Cardiac Sensor (Verity)" card. See deploy\VERITY_SENSOR.md for details.
$ErrorActionPreference = "Stop"

$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = Split-Path -Parent $PSScriptRoot }
$py = Join-Path $Root ".venv\Scripts\python.exe"
$envPath = Join-Path $Root "deploy\.env"
$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null

function Say([string]$m) { Write-Host "  $m" }
Write-Host ""
Write-Host "==== Polar Verity Sense setup ===="

if (-not (Test-Path $py)) { Write-Host "ERROR: venv python missing ($py). Run scripts\windows-setup.ps1 first."; exit 1 }
if (-not (Test-Path $envPath)) { Write-Host "ERROR: deploy\.env missing. Run scripts\windows-setup.ps1 first."; exit 1 }

# --- 1. install the BLE stack -----------------------------------------------------------------
Say "installing the Bluetooth library (bleak)..."
& $py -c "import bleak" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $py -m pip install --quiet --disable-pip-version-check bleak
    & $py -c "import bleak" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: could not install 'bleak'. Check your internet connection and re-run."; exit 1 }
}
Say "bleak ready."
# mark deps done so the watchdog doesn't re-check/install
Set-Content -Path (Join-Path $run "verity-deps.ok") -Value "ok" -Encoding ASCII

# --- 2. scan so you can SEE the sensor before enabling ----------------------------------------
Write-Host ""
Say "scanning for your sensor (put the Verity in HR mode: single press -> blue LED)..."
& $py (Join-Path $Root "scripts\verity_forwarder.py") --scan

# --- 3. enable it in deploy\.env (idempotent) -------------------------------------------------
# Rewrites KEY=..., replacing a commented-out placeholder in place if there is one, else appends.
function Set-EnvKey([string]$key, [string]$value) {
    $lines = Get-Content $envPath
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match ("^\s*#?\s*" + [regex]::Escape($key) + "\s*=")) { $found = $true; "$key=$value" }
        else { $line }
    }
    if (-not $found) { $out += "$key=$value" }
    Set-Content -Path $envPath -Value $out -Encoding ASCII
}

function Get-EnvVars {
    $vars = @{}
    if (Test-Path $envPath) {
        Get-Content $envPath | ForEach-Object {
            if ($_ -match '^\s*([^#=]+)=(.*)$') { $vars[$matches[1].Trim()] = $matches[2].Trim() }
        }
    }
    return $vars
}

Write-Host ""
Say "enabling SLEEPCTL_VERITY=1 in deploy\.env ..."
Set-EnvKey "SLEEPCTL_VERITY" "1"
Say "enabled."

# The forwarder POSTs to /hr/ingest, which is auth-protected like the other phone endpoints. With
# no BCG_INGEST_TOKEN (and no BCG_INGEST_OPEN) the armband connects and streams perfectly while
# EVERY POST is rejected 401 -- the dashboard just shows "not streaming" and the real reason sits
# buried in .run\verity.log. That is the one silent failure left in this setup, so close it here:
# mint a token if there isn't one. A static token also keeps ingest authenticated over a public
# Tailscale funnel and never expires, unlike the 30-day dashboard JWT.
$vars = Get-EnvVars
$hasToken = $vars.ContainsKey("BCG_INGEST_TOKEN") -and $vars["BCG_INGEST_TOKEN"]
$isOpen = $vars.ContainsKey("BCG_INGEST_OPEN") -and $vars["BCG_INGEST_OPEN"] -notin @("0","false","off","no","")
if ($hasToken) {
    Say "ingest auth: BCG_INGEST_TOKEN already set."
} elseif ($isOpen) {
    Say "ingest auth: BCG_INGEST_OPEN=1 (token-less on the trusted LAN) -- leaving as is."
} else {
    $tok = & $py -c "import secrets; print(secrets.token_urlsafe(24))"
    if ($LASTEXITCODE -ne 0 -or -not $tok) {
        Write-Host "ERROR: could not generate an ingest token." -ForegroundColor Red; exit 1
    }
    Set-EnvKey "BCG_INGEST_TOKEN" $tok.Trim()
    Say "ingest auth: generated a BCG_INGEST_TOKEN (the forwarder reads it from deploy\.env)."
}

# --- 4. nudge the watchdog to reload (picks up the env + launches the forwarder) --------------
# Writing .run\restart.request='watchdog' triggers the watchdog's guarded self-restart, which
# reloads deploy\.env and starts the forwarder on its next cycle. Falls back to a clean no-op if
# the watchdog isn't running (it'll pick everything up whenever it next starts).
Set-Content -Path (Join-Path $run "restart.request") -Value "watchdog" -Encoding ASCII
Say "asked the watchdog to restart so it launches the forwarder."

# --- 5. VERIFY: wait for real samples rather than declaring success ---------------------------
# Every preceding step can succeed while the feed is still dead (armband asleep, ingest rejecting,
# watchdog not running). Watch the DB for a fresh live_cardiac row and say plainly which it is.
Write-Host ""
Say "waiting for the first samples (up to 3 min; wear the armband, LED blue)..."
$vars = Get-EnvVars
$dbPath = $vars["SLEEPCTL_DB"]
if (-not $dbPath) {
    Say "SLEEPCTL_DB not set in deploy\.env -- skipping the automatic check."
} else {
    $probe = @'
import sqlite3, sys, datetime
con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("SELECT updated, hr FROM live_cardiac WHERE id = 1").fetchone()
except Exception:
    print("NONE"); raise SystemExit
if not row or not row[0]:
    print("NONE"); raise SystemExit
age = (datetime.datetime.now(datetime.timezone.utc)
       - datetime.datetime.fromisoformat(row[0])).total_seconds()
print(("FRESH " if age < 120 else "STALE ") + str(int(age)) + " " + str(row[1]))
'@
    $probePath = Join-Path $run "verity-probe.py"
    Set-Content -Path $probePath -Value $probe -Encoding ASCII
    $streaming = $false
    foreach ($i in 1..18) {
        Start-Sleep -Seconds 10
        $res = (& $py $probePath $dbPath 2>$null | Select-Object -First 1)
        if ($res -like "FRESH*") {
            $parts = $res -split '\s+'
            Say "STREAMING -- live HR $($parts[2]) bpm (sample $($parts[1])s old)."
            $streaming = $true
            break
        }
        if ($i % 3 -eq 0) { Say "  ...still waiting ($($i * 10)s)" }
    }
    if (-not $streaming) {
        Write-Host ""
        Write-Host "  NO SAMPLES YET after 3 minutes." -ForegroundColor Yellow
        Say "Check, in order:"
        Say "  1. .run\verity.log -- 'POST failed ... 401' means ingest auth (re-run this script);"
        Say "     'no sensor found' means the armband is asleep or out of range."
        Say "  2. The armband is ON and worn: single press -> blue LED (HR mode), not white/green."
        Say "  3. The watchdog is running:  Get-ScheduledTask SleepController"
        Say "It can also simply need another minute -- re-check the dashboard's Cardiac Sensor card."
    }
}

Write-Host ""
Write-Host "==== done ===="
Say "Verify anytime: dashboard -> Admin -> 'Cardiac Sensor (Verity)' shows 'Streaming' with a live HR."
Say "Logs: .run\verity.log   |   Full guide: deploy\VERITY_SENSOR.md"
Write-Host ""
