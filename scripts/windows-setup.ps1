# SleepController -- Windows one-time setup.
# Run AFTER installing Python 3.11, Git, and Node LTS (see deploy\WINDOWS_HOME_SERVER.md Step 0),
# in a freshly-opened PowerShell. Clones the repo, vendors pyEight, builds the Python venv +
# engine, installs the web app, and generates the dashboard login.
$ErrorActionPreference = "Stop"
$Root = Join-Path $HOME "SleepController"

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
foreach ($c in @("python", "git", "node", "npm")) {
    if (-not (Have $c)) {
        Write-Host "ERROR: '$c' not found. Do Step 0 (winget installs), then open a NEW PowerShell." -ForegroundColor Red
        exit 1
    }
}

# Stop any already-running server first -- otherwise the live daemon holds the venv's python.exe
# and `pip install` fails with "Permission denied: ...\.venv\Scripts\python.exe".
Write-Host "==> Stopping any running SleepController (so pip isn't blocked)..." -ForegroundColor Cyan
try { Stop-ScheduledTask -TaskName "SleepController" -ErrorAction SilentlyContinue } catch {}
Get-Process python, node, uvicorn -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "==> Cloning / updating SleepController..." -ForegroundColor Cyan
if (Test-Path $Root) { Set-Location $Root; git pull }
else { git clone https://github.com/dgkenn/SleepController.git $Root; Set-Location $Root }

Write-Host "==> Vendoring pyEight (live Pod control)..." -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $Root "pyEight"))) {
    git clone https://github.com/lukas-clarke/pyEight.git
}

Write-Host "==> Python venv + engine install (this takes a minute)..." -ForegroundColor Cyan
python -m venv .venv
$py = Join-Path $Root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -e ".[eightsleep]" --quiet
& $py -m pip install -r dashboard\api\requirements.txt --quiet
# pyEight ships a requirements.txt but NO setup.py -- it isn't pip-installable, so we install ITS
# OWN deps here and put the package on PYTHONPATH (the launchers/watchdog already do that). Skipping
# this is the usual cause of "pyEight is required for the Eight Sleep cloud adapter".
$pyEightReq = Join-Path $Root "pyEight\requirements.txt"
if (Test-Path $pyEightReq) {
    Write-Host "==> Installing pyEight's own dependencies..." -ForegroundColor Cyan
    & $py -m pip install -r $pyEightReq --quiet
}
# Verify the cloud adapter can actually import pyEight (the real go/no-go for live control).
$env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
& $py -c "from pyeight.eight import EightSleep; print('OK')" > $null 2>&1
if ($LASTEXITCODE -eq 0) { Write-Host "    pyEight import: OK (live Pod control available)." -ForegroundColor Green }
else { Write-Host "    pyEight import: FAILED -- run:  `$env:PYTHONPATH='$Root\pyEight'; .\.venv\Scripts\python.exe -c 'import pyeight.eight'  to see why." -ForegroundColor Red }

Write-Host "==> Installing + building the web app (production build for 24/7 stability)..." -ForegroundColor Cyan
Push-Location (Join-Path $Root "dashboard\web")
npm install --no-audit --no-fund
npm run build
Pop-Location

Write-Host "==> Generating dashboard secrets (deploy\.env)..." -ForegroundColor Cyan
$envPath = Join-Path $Root "deploy\.env"
function RandHex([int]$n) { -join ((1..$n) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }) }
if (-not (Test-Path $envPath)) {
    $pw = RandHex 4
    $lines = @(
        "SLEEPCTL_DB=$Root\sleepctl.db",
        "JWT_SECRET=$(RandHex 32)",
        "DASHBOARD_USER=admin",
        "DASHBOARD_PASSWORD=$pw",
        "BCG_INGEST_OPEN=1",
        "SLEEPCTL_LIVE=1",
        "SLEEPCTL_DRY_RUN=1",
        "",
        "# --- Eight Sleep login (REQUIRED for live control) -------------------------",
        "# Without these the daemon CANNOT reach the Pod and silently runs the SIMULATOR.",
        "# Fill them in, then the daemon auto-logs-in on every start -- you never type a password.",
        "EIGHTSLEEP_EMAIL=",
        "EIGHTSLEEP_PASSWORD=",
        "EIGHTSLEEP_SIDE=right",
        "EIGHTSLEEP_TIMEZONE=America/New_York"
    )
    Set-Content -Path $envPath -Value $lines -Encoding ASCII
    Write-Host ""
    Write-Host "  Dashboard login:  admin  /  $pw   (also in deploy\.env)" -ForegroundColor Green
    Write-Host "  ACTION: open deploy\.env and fill EIGHTSLEEP_EMAIL / EIGHTSLEEP_PASSWORD" -ForegroundColor Yellow
} else {
    Write-Host "  deploy\.env already exists -- leaving it as is." -ForegroundColor Yellow
    $envText = Get-Content $envPath -Raw
    if ($envText -notmatch "EIGHTSLEEP_EMAIL") {
        Write-Host "  WARNING: deploy\.env has NO EIGHTSLEEP_EMAIL -- live control will fall back to the" -ForegroundColor Red
        Write-Host "           SIMULATOR. Add these lines to deploy\.env to actually drive the Pod:" -ForegroundColor Red
        Write-Host "             EIGHTSLEEP_EMAIL=you@example.com" -ForegroundColor Yellow
        Write-Host "             EIGHTSLEEP_PASSWORD=your-password" -ForegroundColor Yellow
        Write-Host "             EIGHTSLEEP_SIDE=right" -ForegroundColor Yellow
        Write-Host "             EIGHTSLEEP_TIMEZONE=America/New_York" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "==> Setup complete." -ForegroundColor Green
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. Put your Eight Sleep email/password in deploy\.env (EIGHTSLEEP_EMAIL / _PASSWORD)."
Write-Host "     The daemon logs in automatically from there -- no `auth` prompt, no manual login."
Write-Host "  2. (optional) read-only Pod check -- sends NOTHING to the bed:"
Write-Host "       .\.venv\Scripts\Activate.ps1"
Write-Host "       `$env:PYTHONPATH = `"$Root;$Root\pyEight`""
Write-Host "       python -m sleepctl.cli calibrate"
Write-Host "  3. go always-on (Admin PowerShell):"
Write-Host "       powershell -ExecutionPolicy Bypass -File scripts\windows-always-on.ps1"
Write-Host "     Then open the iPhone dashboard, press PRIME, and let it run."
