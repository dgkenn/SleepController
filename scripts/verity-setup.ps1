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
Write-Host ""
Say "enabling SLEEPCTL_VERITY=1 in deploy\.env ..."
$lines = Get-Content $envPath
$found = $false
$out = foreach ($line in $lines) {
    if ($line -match '^\s*#?\s*SLEEPCTL_VERITY\s*=') { $found = $true; "SLEEPCTL_VERITY=1" }
    else { $line }
}
if (-not $found) { $out += "SLEEPCTL_VERITY=1" }
Set-Content -Path $envPath -Value $out -Encoding ASCII
Say "enabled."

# --- 4. nudge the watchdog to reload (picks up the env + launches the forwarder) --------------
# Writing .run\restart.request='watchdog' triggers the watchdog's guarded self-restart, which
# reloads deploy\.env and starts the forwarder on its next cycle. Falls back to a clean no-op if
# the watchdog isn't running (it'll pick everything up whenever it next starts).
Set-Content -Path (Join-Path $run "restart.request") -Value "watchdog" -Encoding ASCII
Say "asked the watchdog to restart so it launches the forwarder."

Write-Host ""
Write-Host "==== done ===="
Say "Within ~1-2 minutes the forwarder connects and HR/HRV start streaming."
Say "Verify: dashboard -> Admin -> 'Cardiac Sensor (Verity)' should show 'Streaming' with a live HR."
Say "Logs: .run\verity.log   |   Full guide: deploy\VERITY_SENSOR.md"
Write-Host ""
