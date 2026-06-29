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

Write-Host "==> Installing the web app..." -ForegroundColor Cyan
Push-Location (Join-Path $Root "dashboard\web")
npm install --no-audit --no-fund
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
        "SLEEPCTL_DRY_RUN=1"
    )
    Set-Content -Path $envPath -Value $lines -Encoding ASCII
    Write-Host ""
    Write-Host "  Dashboard login:  admin  /  $pw" -ForegroundColor Green
    Write-Host "  (also saved in deploy\.env)" -ForegroundColor Green
} else {
    Write-Host "  deploy\.env already exists -- leaving it as is." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Setup complete." -ForegroundColor Green
Write-Host "Next (Step 2): connect to your Pod, read-only:" -ForegroundColor Cyan
Write-Host "    cd $Root"
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "    `$env:PYTHONPATH = `"$Root\pyEight`""
Write-Host "    python -m sleepctl.cli auth"
Write-Host "    python -m sleepctl.cli calibrate     # read-only, sends nothing"
