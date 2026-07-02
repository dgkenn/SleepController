# SleepController -- startup config validation (Windows).
#
# Called EARLY by windows-watchdog.ps1, before any service is started, so a bad deploy fails
# LOUDLY at boot (in watchdog.log + .run\validate.result) instead of silently at 3am. This
# script never itself blocks startup -- it's read-only / side-effect-free (aside from writing
# .run\validate.result and a throwaway probe file to test DB-directory writability, which it
# deletes immediately) -- the WATCHDOG decides what to do with a FATAL result (log CRITICAL +
# raise .run\watchdog.alert, then still attempt to start every service; never brick).
#
# Checks:
#   - deploy\.env exists and has the required keys: SLEEPCTL_DB, JWT_SECRET, DASHBOARD_USER,
#     DASHBOARD_PASSWORD (FATAL if any is missing/empty -- nothing works without these)
#   - EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD present (WARN only -- daemon falls back to SIMULATOR)
#   - DIAG_TOKEN present (WARN only -- /api/diag remote diagnostics just won't be reachable)
#   - the venv python exists, and `from pyeight.eight import EightSleep` actually imports
#     (FATAL -- without this, live Pod control cannot work at all)
#   - the SLEEPCTL_DB directory is writable (FATAL -- nothing can persist otherwise)
#
# Exit codes (mirrored as the PASS/WARN/FAIL line in .run\validate.result):
#   0 = PASS   all required config present, venv + pyEight import OK, DB path writable
#   1 = WARN   only optional config missing (EightSleep creds and/or DIAG_TOKEN)
#   2 = FAIL   something required is missing/broken
#
# Standalone use:  powershell -ExecutionPolicy Bypass -File scripts\validate_env.ps1
param(
    [string]$Root = (Join-Path $HOME "SleepController")
)
$ErrorActionPreference = "Continue"
$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null

$fails = @()
$warns = @()
$oks = @()

# --- deploy\.env + required/optional keys -------------------------------------------------------
$envPath = Join-Path $Root "deploy\.env"
$vars = @{}
if (-not (Test-Path $envPath)) {
    $fails += "deploy\.env is missing -- run scripts\windows-setup.ps1 first."
} else {
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') { $vars[$matches[1].Trim()] = $matches[2].Trim() }
    }
    foreach ($k in @("SLEEPCTL_DB", "JWT_SECRET", "DASHBOARD_USER", "DASHBOARD_PASSWORD")) {
        if ($vars.ContainsKey($k) -and $vars[$k]) {
            $oks += "$k present"
        } else {
            $fails += "required key '$k' is missing/empty in deploy\.env"
        }
    }
    $hasEmail = $vars.ContainsKey("EIGHTSLEEP_EMAIL") -and $vars["EIGHTSLEEP_EMAIL"]
    $hasPass = $vars.ContainsKey("EIGHTSLEEP_PASSWORD") -and $vars["EIGHTSLEEP_PASSWORD"]
    if ($hasEmail -and $hasPass) {
        $oks += "EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD present"
    } else {
        $warns += "EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD missing -- the daemon will run in SIMULATOR mode, not driving the real Pod."
    }
    if ($vars.ContainsKey("DIAG_TOKEN") -and $vars["DIAG_TOKEN"]) {
        $oks += "DIAG_TOKEN present"
    } else {
        $warns += "DIAG_TOKEN missing -- /api/diag remote diagnostics will 404 (by design), losing the fast self-diagnosis path."
    }
}

# --- venv python + pyEight import ----------------------------------------------------------------
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $fails += "venv python missing ($py) -- run scripts\windows-setup.ps1."
} else {
    $oks += "venv python present"
    $prevPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
    & $py -c "from pyeight.eight import EightSleep" *> $null
    if ($LASTEXITCODE -ne 0) {
        $fails += "'from pyeight.eight import EightSleep' failed to import -- live Pod control will not work (re-run windows-setup.ps1's pyEight step)."
    } else {
        $oks += "pyeight.eight import OK"
    }
    $env:PYTHONPATH = $prevPythonPath
}

# --- DB path writable -------------------------------------------------------------------------
if ($vars.ContainsKey("SLEEPCTL_DB") -and $vars["SLEEPCTL_DB"]) {
    $dbPath = $vars["SLEEPCTL_DB"]
    $dbDir = Split-Path -Parent $dbPath
    if (-not $dbDir) { $dbDir = $Root }
    try {
        if (-not (Test-Path $dbDir)) { New-Item -ItemType Directory -Force -Path $dbDir -ErrorAction Stop | Out-Null }
        $probe = Join-Path $dbDir (".validate_probe_{0}.tmp" -f ([guid]::NewGuid().ToString("N")))
        Set-Content -Path $probe -Value "probe" -ErrorAction Stop
        Remove-Item -Path $probe -Force -ErrorAction SilentlyContinue
        $oks += "DB directory writable ($dbDir)"
    } catch {
        $fails += "DB directory not writable (${dbDir}): $_"
    }
}
# (if SLEEPCTL_DB itself is missing/empty, that's already recorded as a required-key FAIL above)

# --- verdict + result file ------------------------------------------------------------------------
if ($fails.Count -gt 0) { $verdict = "FAIL" }
elseif ($warns.Count -gt 0) { $verdict = "WARN" }
else { $verdict = "PASS" }

$lines = @()
$lines += "$verdict -- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
foreach ($f in $fails) { $lines += "  [FAIL] $f" }
foreach ($w in $warns) { $lines += "  [WARN] $w" }
foreach ($o in $oks) { $lines += "  [OK]   $o" }
Set-Content -Path (Join-Path $run "validate.result") -Value $lines -Encoding ASCII

$lines | ForEach-Object { Write-Host $_ }

if ($verdict -eq "FAIL") { exit 2 }
elseif ($verdict -eq "WARN") { exit 1 }
else { exit 0 }
