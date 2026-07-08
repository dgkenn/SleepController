# SleepController -- ONE-SHOT bootstrap for a fresh, always-on Windows machine.
# ============================================================================
# Paste this whole file into an **Administrator** PowerShell (Start > type
# "powershell" > right-click > Run as administrator), or run the one-liner:
#     irm https://raw.githubusercontent.com/dgkenn/SleepController/main/scripts/windows-bootstrap.ps1 | iex
#
# It does EVERYTHING from a bare machine:
#   1. Installs Git, Python 3.11, Node LTS, Tailscale (via winget)
#   2. Refreshes PATH in-session (no need to reopen PowerShell)
#   3. Clones the repo + runs windows-setup.ps1 (venv, pyEight, web build, .env)
#   4. Prompts for your Eight Sleep login and writes it into deploy\.env
#   5. Runs windows-always-on.ps1 (disable sleep on AC + boot Scheduled Task)
#   6. Brings up Tailscale + the public funnel so your iPhone can reach it
# Re-runnable: safe to run again (it updates rather than duplicating).
# ============================================================================
$ErrorActionPreference = "Stop"

# --- 0. must be elevated (power policy + boot task need admin) ---------------
$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "ERROR: open PowerShell as ADMINISTRATOR (right-click > Run as administrator) and paste again." -ForegroundColor Red
    return
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: winget not found. Install 'App Installer' from the Microsoft Store, then re-run." -ForegroundColor Red
    return
}

# --- 1. prerequisites via winget (tolerate 'already installed') --------------
function Install-Pkg($id) {
    Write-Host "==> Installing $id ..." -ForegroundColor Cyan
    winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
    # 0 = installed; -1978335189 = already installed; -1978335215 = no newer version -> all fine
    if ($LASTEXITCODE -notin 0, -1978335189, -1978335215) {
        Write-Host "    (winget returned $LASTEXITCODE for $id -- continuing; verify manually if a later step fails)" -ForegroundColor DarkYellow
    }
}
Install-Pkg "Git.Git"
Install-Pkg "Python.Python.3.11"
Install-Pkg "OpenJS.NodeJS.LTS"
Install-Pkg "tailscale.tailscale"

# --- 2. refresh PATH in this session so the new tools are usable now ---------
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path","User")
foreach ($c in "git","python","node","npm") {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: '$c' still not on PATH after install. Close this window, open a NEW Admin PowerShell, and paste again (the installs are done; PATH just needs a fresh shell)." -ForegroundColor Red
        return
    }
}

# --- 3. Eight Sleep login (asked up front; written after setup makes .env) ---
Write-Host ""
Write-Host "Your Eight Sleep login (stored locally in deploy\.env; used to drive the Pod)." -ForegroundColor Cyan
$esEmail = Read-Host "  Eight Sleep email"
$esSecure = Read-Host "  Eight Sleep password" -AsSecureString
$esPass = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($esSecure))

# --- 4. clone + core setup (venv, pyEight, web build, deploy\.env) -----------
$Root = Join-Path $HOME "SleepController"
Write-Host "==> Running core setup (clone, venv, pyEight, web build)..." -ForegroundColor Cyan
if (-not (Test-Path $Root)) { git clone https://github.com/dgkenn/SleepController.git $Root }
# windows-setup.ps1 clones/updates, builds the venv+web, and writes deploy\.env with a random
# dashboard password + DIAG_TOKEN and blank Eight Sleep creds + EIGHTSLEEP_SIDE=right.
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\windows-setup.ps1")

# --- 5. inject the Eight Sleep creds into deploy\.env ------------------------
$envPath = Join-Path $Root "deploy\.env"
if (Test-Path $envPath) {
    $txt = Get-Content $envPath -Raw
    $txt = [regex]::Replace($txt, "(?m)^EIGHTSLEEP_EMAIL=.*$",    "EIGHTSLEEP_EMAIL=$esEmail")
    $txt = [regex]::Replace($txt, "(?m)^EIGHTSLEEP_PASSWORD=.*$", "EIGHTSLEEP_PASSWORD=$esPass")
    if ($txt -notmatch "(?m)^EIGHTSLEEP_EMAIL=") { $txt += "`r`nEIGHTSLEEP_EMAIL=$esEmail`r`nEIGHTSLEEP_PASSWORD=$esPass`r`nEIGHTSLEEP_SIDE=right`r`n" }
    Set-Content -Path $envPath -Value $txt -Encoding ASCII -NoNewline
    Write-Host "    Eight Sleep login written to deploy\.env." -ForegroundColor Green
}

# --- 6. always-on: disable AC sleep + register the boot Scheduled Task -------
Write-Host "==> Enabling always-on (no AC sleep + auto-start watchdog)..." -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\windows-always-on.ps1")

# --- 7. Tailscale funnel so the iPhone can reach it from anywhere ------------
Write-Host ""
Write-Host "==> Tailscale (remote access). A browser will open to log in..." -ForegroundColor Cyan
try {
    tailscale up 2>&1 | Out-Host
    # Expose the dashboard (web on :3000, which proxies /api) over the public funnel.
    tailscale funnel --bg 3000 2>&1 | Out-Host
    Start-Sleep -Seconds 3
    $dns = $null
    try { $dns = (tailscale status --json | ConvertFrom-Json).Self.DNSName.TrimEnd(".") } catch {}
    if ($dns) {
        $url = "https://$dns"
        # point CORS at the real funnel host so the PWA's requests aren't rejected
        if (Test-Path $envPath) {
            $t2 = Get-Content $envPath -Raw
            if ($t2 -match "(?m)^CORS_ORIGINS=") { $t2 = [regex]::Replace($t2,"(?m)^CORS_ORIGINS=.*$","CORS_ORIGINS=$url") }
            else { $t2 += "`r`nCORS_ORIGINS=$url`r`n" }
            Set-Content -Path $envPath -Value $t2 -Encoding ASCII -NoNewline
        }
        Write-Host ""
        Write-Host "  Dashboard URL (open on your iPhone):  $url" -ForegroundColor Green
    } else {
        Write-Host "  Tailscale is up; run 'tailscale status' to get your machine's URL, and" -ForegroundColor Yellow
        Write-Host "  'tailscale funnel --bg 3000' if the funnel didn't attach." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Tailscale step needs a hand: run 'tailscale up' then 'tailscale funnel --bg 3000' manually." -ForegroundColor Yellow
}

# --- done -------------------------------------------------------------------
$pw = ""
if (Test-Path $envPath) { $m = Select-String -Path $envPath -Pattern "^DASHBOARD_PASSWORD=(.+)$"; if ($m) { $pw = $m.Matches[0].Groups[1].Value } }
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " SleepController is set up and running always-on." -ForegroundColor Green
Write-Host "   Dashboard login:  admin  /  $pw" -ForegroundColor Green
Write-Host "   Watch it boot:    Get-Content `"$Root\.run\watchdog.log`" -Wait"
Write-Host "   deploy\.env holds your creds + secrets (dashboard pw, DIAG_TOKEN)." -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Green
Write-Host "NOTE: first boot runs with SLEEPCTL_DRY_RUN=1 (read-only). When you've confirmed" -ForegroundColor Cyan
Write-Host "the dashboard loads and physiology/telemetry look right, set SLEEPCTL_DRY_RUN=0 in" -ForegroundColor Cyan
Write-Host "deploy\.env to let it actually drive the bed." -ForegroundColor Cyan
