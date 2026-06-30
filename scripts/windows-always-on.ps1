# SleepController -- make the home server ALWAYS-ON on a Windows laptop.
# Run ONCE in an ELEVATED (Administrator) PowerShell. It does two things:
#   1. Keeps the laptop AWAKE on AC power (no sleep/hibernate; lid-close does nothing), so the
#      controller keeps running with the lid shut.
#   2. Registers a Scheduled Task that launches the supervising watchdog at every boot/logon and
#      restarts it if it ever dies -- so the dashboard + daemon survive reboots and crashes.
#
# Prereqs: you've already run windows-setup.ps1 and filled in deploy\.env (creds + SLEEPCTL_LIVE=1).
# Undo later with:  Unregister-ScheduledTask -TaskName "SleepController" -Confirm:$false
$ErrorActionPreference = "Stop"
$Root = Join-Path $HOME "SleepController"
$watchdog = Join-Path $Root "scripts\windows-watchdog.ps1"
if (-not (Test-Path $watchdog)) { Write-Host "ERROR: $watchdog not found -- run windows-setup.ps1 first (git pull)." -ForegroundColor Red; exit 1 }

# must be elevated to change power policy + register a system-wide boot task
$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(`
            [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "ERROR: run this in an ADMINISTRATOR PowerShell (right-click > Run as administrator)." -ForegroundColor Red; exit 1 }

Write-Host "==> 1/2  Keeping the laptop awake on AC power..." -ForegroundColor Cyan
# Never sleep / hibernate / spin down the disk while plugged in. (Battery settings left alone --
# keep it plugged in; the bed's controller laptop should be on AC.)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
# Closing the lid on AC = do nothing (LIDACTION 0), so you can shut the lid and walk away.
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
Write-Host "    AC sleep/hibernate disabled; lid-close = do nothing (on AC)." -ForegroundColor Green
Write-Host "    (Display may still turn off -- that's fine, it doesn't stop the controller.)" -ForegroundColor DarkGray

Write-Host "==> 2/2  Registering the always-on Scheduled Task 'SleepController'..." -ForegroundColor Cyan
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdog`""
# Start at boot AND at logon (covers both whether or not you sign in).
$triggers = @( (New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn) )
# S4U = run whether the user is logged on or not, WITHOUT storing a password. Highest privileges
# so it can manage the firewall rule.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "SleepController" -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "    Task registered (starts at boot/logon, auto-restarts on failure)." -ForegroundColor Green

Write-Host "==> Starting it now..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName "SleepController"

Write-Host ""
Write-Host "Always-on is set up." -ForegroundColor Green
Write-Host "  Watch it come up:   Get-Content `"$Root\.run\watchdog.log`" -Wait"
Write-Host "  Confirm it's alive: Get-ScheduledTask SleepController ; Get-Content `"$Root\.run\watchdog.heartbeat`""
Write-Host "  Stop for now:       Stop-ScheduledTask SleepController ; Get-Process python,node | Stop-Process -Force"
Write-Host "  Remove always-on:   Unregister-ScheduledTask SleepController -Confirm:`$false"
Write-Host ""
Write-Host "Open the dashboard on your iPhone (same WiFi), log in, and press PRIME, then let it run." -ForegroundColor Cyan
