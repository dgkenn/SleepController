# SleepController — launch the home server on Windows (API + control daemon + web PWA).
# Reads deploy\.env, ensures the login user, and starts the three services in the background.
$ErrorActionPreference = "Stop"
$Root = Join-Path $HOME "SleepController"
Set-Location $Root

# --- load deploy\.env into the environment ---
$envPath = Join-Path $Root "deploy\.env"
if (-not (Test-Path $envPath)) { Write-Host "deploy\.env missing — run windows-setup.ps1 first." -ForegroundColor Red; exit 1 }
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { Set-Item -Path ("env:" + $matches[1].Trim()) -Value $matches[2].Trim() }
}
$env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
$py = Join-Path $Root ".venv\Scripts\python.exe"
$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null

# --- init DB + ensure the login user exists ---
Write-Host "==> Preparing database + login user..." -ForegroundColor Cyan
& $py -c "from app.db import connect; from app.security import ensure_bootstrap_user; connect(); ensure_bootstrap_user(); print('ready')"

# --- API on :8000 ---
Write-Host "==> Starting API (:8000)..." -ForegroundColor Cyan
Start-Process -FilePath $py -WindowStyle Hidden `
    -ArgumentList @("-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--app-dir","dashboard\api") `
    -RedirectStandardOutput "$run\api.log" -RedirectStandardError "$run\api.err"
Start-Sleep -Seconds 3

# --- control daemon (live; SLEEPCTL_LIVE / SLEEPCTL_DRY_RUN come from deploy\.env) ---
Write-Host "==> Starting control daemon..." -ForegroundColor Cyan
Start-Process -FilePath $py -WindowStyle Hidden `
    -ArgumentList @("dashboard\daemon\run_daemon.py") `
    -RedirectStandardOutput "$run\daemon.log" -RedirectStandardError "$run\daemon.err"

# --- web PWA on :3000 ---
Write-Host "==> Starting web (:3000)..." -ForegroundColor Cyan
$env:API_URL = "http://localhost:8000"
$env:PORT = "3000"
Start-Process -FilePath "npm.cmd" -WindowStyle Hidden -WorkingDirectory (Join-Path $Root "dashboard\web") `
    -ArgumentList @("run","dev") -RedirectStandardOutput "$run\web.log" -RedirectStandardError "$run\web.err"

# --- find the LAN IP for the iPhone URL ---
$ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
       Select-Object -First 1).IPAddress
if (-not $ip) { $ip = "<this-pc-ip>" }

Write-Host ""
Write-Host "==> Home server is starting." -ForegroundColor Green
Write-Host "  On your iPhone (same WiFi):  http://${ip}:3000"
Write-Host "  Login: $($env:DASHBOARD_USER)  /  $($env:DASHBOARD_PASSWORD)"
Write-Host "  Logs: $run\api.log  $run\daemon.log  $run\web.log"
Write-Host "  Live mode: SLEEPCTL_LIVE=$($env:SLEEPCTL_LIVE)  dry-run: SLEEPCTL_DRY_RUN=$($env:SLEEPCTL_DRY_RUN)"
Write-Host "  Stop everything:  Get-Process python,node | Stop-Process" -ForegroundColor DarkGray
