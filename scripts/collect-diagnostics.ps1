# SleepController -- "send this to Claude" diagnostic bundle (Windows, standalone).
#
# Produces the SAME single-artifact bundle as `GET /diag/bundle` (dashboard/api/app/
# diag_bundle.py), but reads the local .run logs/results directly -- no API needed. Use this
# when the dashboard/API is DOWN and you can't hit /diag/bundle; if the API IS up, prefer
# that endpoint (it also includes the structured events table, which only the API can read).
#
# Run it:
#   powershell -ExecutionPolicy Bypass -File scripts\collect-diagnostics.ps1
#
# Writes .run\diag-bundle-YYYYMMDD-HHMMSS.txt and prints its path. Paste/upload that ONE file
# to Claude (or a human helping you debug) -- it never contains secret values, only whether
# they're set (same redaction rule as the API bundle and doctor.ps1: any env KEY matching
# PASSWORD/SECRET/TOKEN/ICS_URL/CLIENT_SECRET/JWT, case-insensitive, is always <redacted>).
$ErrorActionPreference = "Continue"
$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = (Get-Location).Path }  # best-effort if run from a checkout elsewhere
$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$outPath = Join-Path $run "diag-bundle-$ts.txt"

# Growing list of lines -- written to $outPath once at the end.
$script:lines = New-Object System.Collections.Generic.List[string]

function Add-Line([string]$text) {
    $script:lines.Add($text) | Out-Null
}
function Add-Section([string]$title) {
    Add-Line ""
    Add-Line ("===== {0} =====" -f $title)
}

# ------------------------------------------------------------------ header
Add-Section "SLEEPCONTROLLER DIAGNOSTIC BUNDLE"
Add-Line ("generated_at = {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss K"))
Add-Line ("root         = {0}" -f $Root)
Add-Line "This is a single self-contained diagnostic snapshot -- paste/upload the whole thing"
Add-Line "when asking for help debugging. Generated locally (API was not required)."

# ------------------------------------------------------------------ git state
Add-Section "GIT"
Push-Location $Root
try {
    $commit = git rev-parse --short HEAD 2>$null
    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    Add-Line ("commit: {0}   branch: {1}" -f $commit, $branch)
    $dirty = git status -s 2>$null
    if ($dirty) {
        Add-Line "working tree is DIRTY (uncommitted local changes):"
        $dirty -split "`n" | ForEach-Object { Add-Line $_ }
    } else {
        Add-Line "working tree is clean"
    }
} catch {
    Add-Line "git not available or $Root is not a git checkout: $_"
}
Pop-Location

# ------------------------------------------------------------------ process inventory
Add-Section "PROCESSES (python.exe / node.exe / powershell.exe)"
try {
    $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.Name -in @("python.exe", "node.exe", "powershell.exe", "pwsh.exe") }
    if (-not $procs) {
        Add-Line "none found"
    } else {
        $procs | Sort-Object Name, ProcessId | ForEach-Object {
            $start = try { $_.CreationDate } catch { $null }
            Add-Line ("[{0,-14}] PID={1,-7} Start={2,-22} Cmd={3}" -f $_.Name, $_.ProcessId, $start, $_.CommandLine)
        }
        $daemons = $procs | Where-Object { $_.CommandLine -and $_.CommandLine -match "run_daemon\.py" }
        if ($daemons.Count -gt 1) {
            Add-Line ""
            Add-Line ("WARNING: {0} run_daemon.py processes running at once (should be exactly 1)." -f $daemons.Count)
        }
    }
} catch {
    Add-Line "could not enumerate processes (needs Get-CimInstance / WMI access): $_"
}

# ------------------------------------------------------------------ ports
Add-Section "PORTS"
foreach ($port in 8000, 3000) {
    try {
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($listening) {
            $owners = $listening | Select-Object -ExpandProperty OwningProcess -Unique
            Add-Line ("port {0} : LISTENING (PID {1})" -f $port, ($owners -join ", "))
        } else {
            Add-Line ("port {0} : NOT LISTENING" -f $port)
        }
    } catch {
        Add-Line ("port {0} : could not check ({1})" -f $port, $_)
    }
}

# ------------------------------------------------------------------ heartbeats
Add-Section "HEARTBEATS"
function Get-HeartbeatAgeLine([string]$name) {
    $path = Join-Path $run "$name.heartbeat"
    if (-not (Test-Path $path)) { return "$name.heartbeat : MISSING" }
    $lastWrite = (Get-Item $path).LastWriteTime
    $age = [int]((Get-Date) - $lastWrite).TotalSeconds
    return "$name.heartbeat : last write ${age}s ago  ($lastWrite)"
}
if (-not (Test-Path $run)) {
    Add-Line "$run does not exist -- nothing has ever run here."
} else {
    Add-Line (Get-HeartbeatAgeLine "daemon")
    Add-Line (Get-HeartbeatAgeLine "watchdog")
}

# ------------------------------------------------------------------ .run/*.result + *.alert
Add-Section "RESULT / ALERT FILES"
$resultAndAlert = @()
if (Test-Path $run) {
    $resultAndAlert = @(Get-ChildItem -Path $run -Filter "*.result" -ErrorAction SilentlyContinue) +
                      @(Get-ChildItem -Path $run -Filter "*.alert" -ErrorAction SilentlyContinue)
}
if (-not $resultAndAlert -or $resultAndAlert.Count -eq 0) {
    Add-Line "(none found -- no .run\*.result or .run\*.alert files)"
} else {
    foreach ($f in $resultAndAlert) {
        Add-Line ""
        Add-Line ("-- {0} --" -f $f.Name)
        Get-Content -Path $f.FullName -ErrorAction SilentlyContinue | ForEach-Object { Add-Line $_ }
    }
}

# ------------------------------------------------------------------ log tails (same whitelist
# as app.diag_bundle._DIAG_LOG_FILES / app.main._DIAG_LOG_FILES -- keep these in sync)
$TailLines = 150
$logFiles = @("daemon.log", "daemon.err", "daemon-crash.log", "watchdog.log",
              "api.log", "api.err", "web.log", "web-build.log")
foreach ($name in $logFiles) {
    Add-Section ("LOG: {0}" -f $name)
    $path = Join-Path $run $name
    if (-not (Test-Path $path)) {
        Add-Line "(file not found)"
        continue
    }
    try {
        Get-Content -Path $path -Tail $TailLines -ErrorAction Stop | ForEach-Object { Add-Line $_ }
    } catch {
        Add-Line "(could not read: $_)"
    }
}

# ------------------------------------------------------------------ deploy\.env sanity (REDACTED)
Add-Section "CONFIG SNAPSHOT (redacted -- secret values NEVER included)"
# Same rule as diag_bundle.py's is_secret_key(): any KEY matching this pattern (case-
# insensitive substring) is always <redacted>, never its real value.
$SecretKeyPattern = "PASSWORD|SECRET|TOKEN|ICS_URL|CLIENT_SECRET|JWT"
$EnvKeysOfInterest = @(
    "SLEEPCTL_DB", "SLEEPCTL_LIVE", "SLEEPCTL_DRY_RUN", "SLEEPCTL_LAT", "SLEEPCTL_LON",
    "SLEEPCTL_WEATHER", "SLEEPCTL_PHONE_SENSOR",
    "DASHBOARD_USER", "DASHBOARD_PASSWORD",
    "JWT_SECRET", "JWT_TTL_HOURS", "JWT_REMEMBER_HOURS", "JWT_SESSION_HOURS",
    "CORS_ORIGINS", "BCG_INGEST_OPEN",
    "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT",
    "EIGHTSLEEP_EMAIL", "EIGHTSLEEP_PASSWORD", "EIGHTSLEEP_SIDE", "EIGHTSLEEP_TIMEZONE",
    "EIGHTSLEEP_CLIENT_ID", "EIGHTSLEEP_CLIENT_SECRET", "EIGHTSLEEP_CREDENTIALS",
    "CALENDAR_ICS_URL", "DIAG_TOKEN", "TZ"
)
$envPath = Join-Path $Root "deploy\.env"
if (-not (Test-Path $envPath)) {
    Add-Line "deploy\.env MISSING -- run scripts\windows-setup.ps1 first."
} else {
    $vars = @{}
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') { $vars[$matches[1].Trim()] = $matches[2].Trim() }
    }
    foreach ($k in $EnvKeysOfInterest) {
        $present = $vars.ContainsKey($k) -and $vars[$k]
        if ($k -match $SecretKeyPattern) {
            $shown = if ($present) { "<redacted>" } else { "(unset)" }
        } else {
            $shown = if ($present) { $vars[$k] } else { "(unset)" }
        }
        Add-Line ("{0,-24} = {1}" -f $k, $shown)
    }
}

# ------------------------------------------------------------------ live health probe
Add-Section "LIVE PROBE: http://localhost:8000/health"
try {
    $resp = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5 -ErrorAction Stop
    Add-Line ("API IS UP: {0}" -f ($resp | ConvertTo-Json -Compress))
} catch {
    Add-Line "API IS DOWN (or not responding) -- that's expected if you're running this because /diag/bundle wasn't reachable."
}

Add-Section "DONE"
Add-Line "Paste/upload the whole file above to Claude (or whoever's helping debug)."

# ------------------------------------------------------------------ write it out
Set-Content -Path $outPath -Value $script:lines -Encoding UTF8
Write-Host ""
Write-Host "Diagnostic bundle written to:" -ForegroundColor Cyan
Write-Host "  $outPath" -ForegroundColor Yellow
Write-Host ""
Write-Host "Paste or upload that file to Claude (or a human helping you debug)."
