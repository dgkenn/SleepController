# SleepController -- FULL NIGHT-DATA publisher (Windows).
#
# The user explicitly decided this data being public is fine, and that minimizing how often
# they touch the laptop matters more than keeping it private -- so unlike publish-health.ps1
# (scrubbed operational-only) or backup-encrypted.ps1 (encrypted full DB), this one publishes
# FULL per-night detail -- raw sensor samples (HR/HRV/movement/stage/presence/bed temp),
# reconstructed sleep architecture, Perfect Sleep Index, sleep-onset signals (including whether
# the accelerometer/stillness signal contributed), staging transitions, wake detection, and
# thermal interventions -- to a public `night-data` branch of the SAME repo, IN THE CLEAR, no
# age-encryption, no key to manage. An off-box Claude session reads it with a plain
# `git fetch origin night-data` -- no key, no script run by hand, nothing beyond what the
# always-on watchdog already does automatically.
#
# This script:
#   1. builds one JSON file per recent night via the venv python (app/night_export.py),
#   2. pushes them to the `night-data` branch of origin, using a DEDICATED clone under
#      .run\night-data-repo so the live working tree is never touched (mirrors
#      publish-health.ps1 exactly),
#   3. re-roots the branch to a single commit every run (same anti-bloat fix already applied
#      to publish-health.ps1 -- see the commit step below for why).
#
# Meant to run unattended on the same short interval as publish-health.ps1, from the watchdog.
# Every step is defensive: nothing is allowed to throw uncaught -- a failure is logged to
# .run\night-data-publish.log and recorded in .run\night-data-publish.result, then the script
# exits non-zero.
#
# Run it by hand any time:
#   powershell -ExecutionPolicy Bypass -File scripts\publish-night-data.ps1
#
# Result contract (.run\night-data-publish.result), one line:
#   OK <timestamp> <n files>         -- pushed successfully
#   OK <timestamp> nochange          -- nothing changed (unexpected but not a failure); exit 0
#   FAIL <reason>                    -- something broke; exit 1
$ErrorActionPreference = "Stop"

# Fail-fast, never HANG -- same reasoning as publish-health.ps1: force git fully non-interactive
# so a missing/invalid credential returns an error immediately instead of wedging on a hidden
# Git Credential Manager prompt.
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"

# --- locate the repo root (mirrors publish-health.ps1) -----------------------------------------
$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path $Root)) { $Root = (Get-Location).Path }

$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null
$logFile = Join-Path $run "night-data-publish.log"
$resultFile = Join-Path $run "night-data-publish.result"

function Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line
    Write-Host $line
}

function Write-Result([string]$msg) {
    Set-Content -Path $resultFile -Value $msg -Encoding ASCII
}

function Assert-Success([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

Log "==== night-data publish starting (root=$Root) ===="

try {
    # --- 1. load deploy\.env -------------------------------------------------------------------
    $envPath = Join-Path $Root "deploy\.env"
    if (-not (Test-Path $envPath)) {
        $msg = "deploy\.env missing -- run scripts\windows-setup.ps1 first."
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }
    $vars = @{}
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') { $vars[$matches[1].Trim()] = $matches[2].Trim() }
    }

    $dbPath = $vars["SLEEPCTL_DB"]
    if (-not $dbPath) {
        $msg = "required key 'SLEEPCTL_DB' is missing/empty in deploy\.env"
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }

    # --- 2. locate the venv python ---------------------------------------------------------------
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        $msg = "venv python missing ($py) -- run scripts\windows-setup.ps1."
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }

    # --- 3. build the per-night JSON exports via app/night_export.py --------------------------
    $stagingDir = Join-Path $run "night-data-staging"
    New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null
    Get-ChildItem -Path $stagingDir -Filter "night-*.json" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    Log "building night-data exports from $dbPath -> $stagingDir"
    $prevPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
    $pyErrLog = Join-Path $run "night-data-publish-py.err"
    Remove-Item -Path $pyErrLog -Force -ErrorAction SilentlyContinue
    $pyOut = & $py (Join-Path $Root "dashboard\api\app\night_export.py") $dbPath $stagingDir 14 2>$pyErrLog
    $pyExit = $LASTEXITCODE
    $env:PYTHONPATH = $prevPythonPath
    if ($pyExit -ne 0) {
        $errText = ""
        if (Test-Path $pyErrLog) { $errText = (Get-Content $pyErrLog -Raw) }
        throw "night_export.py failed (exit $pyExit): $errText"
    }
    $pyOut | ForEach-Object { Log "night_export: $_" }
    $builtFiles = Get-ChildItem -Path $stagingDir -Filter "*.json" -ErrorAction SilentlyContinue
    if (-not $builtFiles -or $builtFiles.Count -eq 0) { throw "export produced no output files ($stagingDir)" }

    $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

    # --- 4. push to the night-data branch via a DEDICATED clone (same pattern as publish-health.ps1)
    $ndRepo = Join-Path $run "night-data-repo"
    if (-not (Test-Path (Join-Path $ndRepo ".git"))) {
        $originUrl = (& git -C $Root remote get-url origin 2>> $logFile)
        Assert-Success "git remote get-url origin"
        $originUrl = $originUrl.Trim()
        Log "cloning $originUrl -> $ndRepo (first run)"
        Remove-Item -Path $ndRepo -Recurse -Force -ErrorAction SilentlyContinue
        & git clone --quiet $originUrl $ndRepo 2>> $logFile
        Assert-Success "git clone"
    }

    & git -C $ndRepo config core.autocrlf false 2>> $logFile
    & git -C $ndRepo config core.safecrlf false 2>> $logFile
    & git -C $ndRepo config user.email "sleepcontroller-bot@users.noreply.github.com" 2>> $logFile
    & git -C $ndRepo config user.name "SleepController Night-Data Bot" 2>> $logFile

    Log "fetching origin in dedicated night-data clone"
    & git -C $ndRepo fetch origin --quiet 2>> $logFile
    Assert-Success "git fetch origin"

    & git -C $ndRepo rev-parse --verify --quiet "refs/remotes/origin/night-data" *> $null
    $remoteBranchExists = ($LASTEXITCODE -eq 0)
    & git -C $ndRepo rev-parse --verify --quiet "refs/heads/night-data" *> $null
    $localBranchExists = ($LASTEXITCODE -eq 0)

    if ($remoteBranchExists) {
        if ($localBranchExists) {
            Log "checking out existing local night-data branch, syncing to origin/night-data"
            & git -C $ndRepo checkout --quiet night-data 2>> $logFile
            Assert-Success "git checkout night-data"
        } else {
            Log "checking out night-data tracking origin/night-data"
            & git -C $ndRepo checkout --quiet -b night-data origin/night-data 2>> $logFile
            Assert-Success "git checkout -b night-data origin/night-data"
        }
        & git -C $ndRepo reset --hard --quiet origin/night-data 2>> $logFile
        Assert-Success "git reset --hard origin/night-data"
    } else {
        if ($localBranchExists) {
            Log "checking out existing local-only night-data branch (not yet pushed)"
            & git -C $ndRepo checkout --quiet night-data 2>> $logFile
            Assert-Success "git checkout night-data"
        } else {
            Log "no night-data branch anywhere yet -- creating an ORPHAN branch (kept out of main's history)"
            & git -C $ndRepo checkout --quiet --orphan night-data 2>> $logFile
            Assert-Success "git checkout --orphan night-data"
            & git -C $ndRepo rm -rf --quiet . *> $null
            Get-ChildItem -Path $ndRepo -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne ".git" } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # --- replace the working tree with exactly this run's exports (one file per recent night) ---
    # Unlike health's latest.json + growing dated history, night-data holds a ROLLING window --
    # night_export.py already only emits the newest `nights` (14) distinct night_dates, and each
    # file for a given date is fully OVERWRITTEN every run (a still-forming tonight keeps refreshing
    # in place rather than accumulating snapshots), so the working tree IS the full desired state
    # already; wipe anything from a previous run's older window and copy this run's files in.
    Get-ChildItem -Path $ndRepo -Filter "night-*.json" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $ndRepo "error.json") -Force -ErrorAction SilentlyContinue
    foreach ($f in $builtFiles) {
        Copy-Item -Path $f.FullName -Destination $ndRepo -Force
    }
    Log "staged $($builtFiles.Count) night file(s) in $ndRepo"

    # --- commit as a SINGLE ROOT COMMIT every run ------------------------------------------------
    # Same fix already applied to publish-health.ps1 (see that script's comment for the full
    # incident writeup: a normal parented commit every ~10 min grew that branch to 3600+ commits
    # in six weeks). Re-root here from the start so night-data never has that problem to begin with.
    # Probe before deleting -- `git branch -D <missing>` writes to stderr, which under
    # $ErrorActionPreference='Stop' becomes a TERMINATING error and kills the publish on the
    # NORMAL path (no leftover temp branch). See the same guard in publish-health.ps1, where this
    # silently killed every health publish for hours. `rev-parse --verify --quiet` fails silently.
    & git -C $ndRepo rev-parse --verify --quiet "refs/heads/_night_data_tmp" *> $null
    if ($LASTEXITCODE -eq 0) {
        & git -C $ndRepo branch -D _night_data_tmp *> $null
    }
    & git -C $ndRepo checkout --quiet --orphan _night_data_tmp 2>> $logFile
    Assert-Success "git checkout --orphan _night_data_tmp"
    & git -C $ndRepo add -A 2>> $logFile
    Assert-Success "git add -A"

    $statusOut = & git -C $ndRepo status --porcelain
    if (-not $statusOut) {
        Log "nothing staged (unexpected -- exports should always be present); treating as success"
        Write-Result "OK $ts nochange"
        exit 0
    }

    & git -C $ndRepo commit --quiet -m "night-data $ts (single-commit artifact branch)" 2>> $logFile
    Assert-Success "git commit"
    & git -C $ndRepo branch --quiet -M _night_data_tmp night-data 2>> $logFile
    Assert-Success "git branch -M night-data"

    Log "pushing night-data to origin"
    $pushTarget = "origin"
    if ($vars["GIT_PUSH_TOKEN"]) {
        try {
            $ou = (& git -C $ndRepo remote get-url origin 2>$null).Trim()
            if ($ou -match '^https://') {
                $pushTarget = $ou -replace '^https://', ("https://x-access-token:" + $vars["GIT_PUSH_TOKEN"] + "@")
            }
        } catch {}
    }
    # --force is REQUIRED (branch was just re-rooted, deliberately not a fast-forward) -- same
    # justification as publish-health.ps1: night-data holds artifacts only, current window only.
    & git -C $ndRepo push --quiet --force $pushTarget night-data 2>> $logFile
    Assert-Success "git push --force origin night-data"

    Log "OK: pushed $($builtFiles.Count) night file(s)"
    Write-Result "OK $ts $($builtFiles.Count)"
    exit 0
} catch {
    $msg = $_.Exception.Message
    Log "FAIL: $msg"
    Write-Result "FAIL $msg"
    exit 1
}
