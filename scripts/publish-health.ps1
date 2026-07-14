# SleepController -- OPERATIONAL HEALTH publisher (Windows).
#
# The always-on control machine can PUSH to GitHub, but the off-site operator can't reach the
# machine's Tailscale funnel. So instead of the operator pulling /diag, the machine PUSHES a
# small, SCRUBBED operational-health snapshot to a public `health` branch of the SAME repo, where
# an off-box Claude can read it straight from GitHub.
#
# What's published is OPERATIONAL ONLY -- component up/down, heartbeat/tick ages, water loop,
# thermal response, cloud errors, log sizes, credential PRESENCE (never values). No passwords /
# tokens / emails, no HR/HRV/biometrics. dashboard\api\app\health_snapshot.py builds the snapshot
# from run_diagnostics() and runs it through a belt-and-suspenders scrub. Because it's scrubbed
# operational health, it is published IN THE CLEAR on purpose -- there is NO age-encryption here
# (unlike scripts\backup-encrypted.ps1, which publishes the ciphertext of the personal-physiology
# DB and must stay encrypted).
#
# This script:
#   1. builds the snapshot JSON via the venv python (health_snapshot.py),
#   2. pushes it to the `health` branch of origin, using a DEDICATED clone under .run\health-repo
#      so the live working tree is never touched (mirrors backup-encrypted.ps1 exactly),
#   3. keeps both latest.json (always current) + a dated health-<ts>.json history, pruned to 200.
#
# Meant to run unattended on a short interval from a Scheduled Task. Every step is defensive:
# nothing is allowed to throw uncaught -- a failure is logged to .run\health-publish.log and
# recorded in .run\health-publish.result, then the script exits non-zero.
#
# Run it by hand any time:
#   powershell -ExecutionPolicy Bypass -File scripts\publish-health.ps1
#
# Result contract (.run\health-publish.result), one line:
#   OK <timestamp> <blobname>        -- pushed successfully
#   OK <timestamp> nochange          -- nothing changed (unexpected but not a failure); exit 0
#   FAIL <reason>                    -- something broke; exit 1
$ErrorActionPreference = "Stop"

# --- locate the repo root (mirrors backup-encrypted.ps1 / doctor.ps1's fallback style) ---------
$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path $Root)) { $Root = (Get-Location).Path }

$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null
$logFile = Join-Path $run "health-publish.log"
$resultFile = Join-Path $run "health-publish.result"

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

Log "==== health publish starting (root=$Root) ===="

try {
    # --- 1. load deploy\.env -------------------------------------------------------------------
    # Same KEY=VALUE parse style as backup-encrypted.ps1 / windows-watchdog.ps1 / doctor.ps1: a
    # simple line matcher into a hashtable (kept local -- we do NOT export these into the process
    # environment; we only need SLEEPCTL_DB to point the snapshot builder at the DB).
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

    # --- 2. locate the venv python (mirrors backup-encrypted.ps1) ------------------------------
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        $msg = "venv python missing ($py) -- run scripts\windows-setup.ps1."
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }

    # --- 3. build the scrubbed snapshot via health_snapshot.py ---------------------------------
    $stagingDir = Join-Path $run "health-staging"
    New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null
    $outPath = Join-Path $stagingDir "latest.json"

    Log "building operational-health snapshot from $dbPath -> $outPath"
    $prevPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
    $pyErrLog = Join-Path $run "health-publish-py.err"
    Remove-Item -Path $pyErrLog -Force -ErrorAction SilentlyContinue
    $pyOut = & $py (Join-Path $Root "dashboard\api\app\health_snapshot.py") $dbPath $outPath 2>$pyErrLog
    $pyExit = $LASTEXITCODE
    $env:PYTHONPATH = $prevPythonPath
    if ($pyExit -ne 0) {
        $errText = ""
        if (Test-Path $pyErrLog) { $errText = (Get-Content $pyErrLog -Raw) }
        throw "health_snapshot.py failed (exit $pyExit): $errText"
    }
    $pyOut | ForEach-Object { Log "health_snapshot: $_" }
    if (-not (Test-Path $outPath)) { throw "snapshot builder produced no output file ($outPath)" }

    # timestamp for the dated history filename (UTC, so the branch reads consistently off-box)
    $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

    # --- 4. push to the health branch via a DEDICATED clone (never touch the live working tree)
    #        Branch-handling logic copied VERBATIM from backup-encrypted.ps1, branch name `health`.
    $healthRepo = Join-Path $run "health-repo"
    if (-not (Test-Path (Join-Path $healthRepo ".git"))) {
        $originUrl = (& git -C $Root remote get-url origin 2>> $logFile)
        Assert-Success "git remote get-url origin"
        $originUrl = $originUrl.Trim()
        Log "cloning $originUrl -> $healthRepo (first run)"
        Remove-Item -Path $healthRepo -Recurse -Force -ErrorAction SilentlyContinue
        & git clone --quiet $originUrl $healthRepo 2>> $logFile
        Assert-Success "git clone"
    }

    # Configure the dedicated clone BEFORE any checkout/reset/add. Two settings, both essential for
    # unattended operation:
    #  1. core.autocrlf=false / core.safecrlf=false -- our snapshots are LF-terminated JSON. With
    #     Git's Windows default (core.autocrlf=true), `git add` writes "LF will be replaced by CRLF"
    #     to STDERR; under $ErrorActionPreference='Stop' PowerShell turns that native-stderr write
    #     into a TERMINATING error, aborting the publish before commit/push (observed as: orphan
    #     branch created locally but never pushed). We don't want line-ending rewriting anyway.
    #  2. a LOCAL commit identity, so a box with no global git identity can still commit (this box
    #     hit "Committer identity unknown" on a plain `git pull` during setup).
    & git -C $healthRepo config core.autocrlf false 2>> $logFile
    & git -C $healthRepo config core.safecrlf false 2>> $logFile
    & git -C $healthRepo config user.email "sleepcontroller-bot@users.noreply.github.com" 2>> $logFile
    & git -C $healthRepo config user.name "SleepController Health Bot" 2>> $logFile

    Log "fetching origin in dedicated health clone"
    & git -C $healthRepo fetch origin --quiet 2>> $logFile
    Assert-Success "git fetch origin"

    & git -C $healthRepo rev-parse --verify --quiet "refs/remotes/origin/health" *> $null
    $remoteBranchExists = ($LASTEXITCODE -eq 0)
    & git -C $healthRepo rev-parse --verify --quiet "refs/heads/health" *> $null
    $localBranchExists = ($LASTEXITCODE -eq 0)

    if ($remoteBranchExists) {
        if ($localBranchExists) {
            Log "checking out existing local health branch, syncing to origin/health"
            & git -C $healthRepo checkout --quiet health 2>> $logFile
            Assert-Success "git checkout health"
        } else {
            Log "checking out health tracking origin/health"
            & git -C $healthRepo checkout --quiet -b health origin/health 2>> $logFile
            Assert-Success "git checkout -b health origin/health"
        }
        & git -C $healthRepo reset --hard --quiet origin/health 2>> $logFile
        Assert-Success "git reset --hard origin/health"
    } else {
        if ($localBranchExists) {
            Log "checking out existing local-only health branch (not yet pushed)"
            & git -C $healthRepo checkout --quiet health 2>> $logFile
            Assert-Success "git checkout health"
        } else {
            Log "no health branch anywhere yet -- creating an ORPHAN branch (kept out of main's history)"
            & git -C $healthRepo checkout --quiet --orphan health 2>> $logFile
            Assert-Success "git checkout --orphan health"
            # `checkout --orphan` starts with the previous branch's files still staged/present --
            # clear them all out so health contains ONLY snapshots, never source code.
            & git -C $healthRepo rm -rf --quiet . *> $null
            Get-ChildItem -Path $healthRepo -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne ".git" } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # --- copy the snapshot in as BOTH latest.json + a dated health-<ts>.json --------------------
    $destName = "health-$ts.json"
    $destPath = Join-Path $healthRepo $destName
    Copy-Item -Path $outPath -Destination $destPath -Force
    Copy-Item -Path $outPath -Destination (Join-Path $healthRepo "latest.json") -Force
    Log "staged $destName (+ refreshed latest.json) in $healthRepo"

    # --- prune to the newest 200 dated snapshots (latest.json is not counted) -------------------
    $keepCount = 200
    $allBlobs = Get-ChildItem -Path $healthRepo -Filter "health-*.json" -ErrorAction SilentlyContinue |
        Sort-Object Name
    if ($allBlobs.Count -gt $keepCount) {
        $excess = $allBlobs | Select-Object -First ($allBlobs.Count - $keepCount)
        foreach ($f in $excess) {
            Remove-Item -Path $f.FullName -Force -ErrorAction SilentlyContinue
            Log "pruned old snapshot $($f.Name)"
        }
    }

    # --- commit + push ---------------------------------------------------------------------------
    # (line-ending + commit identity already configured on the clone right after checkout, above)
    & git -C $healthRepo add -A 2>> $logFile
    Assert-Success "git add -A"

    $statusOut = & git -C $healthRepo status --porcelain
    if (-not $statusOut) {
        Log "nothing changed to commit (snapshot identical to last push); treating as success"
        Write-Result "OK $ts nochange"
        exit 0
    }

    & git -C $healthRepo commit --quiet -m "health $ts" 2>> $logFile
    Assert-Success "git commit"

    Log "pushing health to origin"
    # Push using a token from deploy\.env (GIT_PUSH_TOKEN) when set, so it works regardless of WHICH
    # Windows user runs it -- the watchdog's Scheduled Task runs as a different account than the
    # interactive login, so a per-user credential store (Git Credential Manager) is invisible to it.
    # Pushing to an explicit tokenized URL depends on no credential helper. Git redacts the userinfo
    # in any error output, so the token doesn't land in the log. Falls back to `origin` when unset.
    $pushTarget = "origin"
    if ($vars["GIT_PUSH_TOKEN"]) {
        try {
            $ou = (& git -C $healthRepo remote get-url origin 2>$null).Trim()
            if ($ou -match '^https://') {
                $pushTarget = $ou -replace '^https://', ("https://x-access-token:" + $vars["GIT_PUSH_TOKEN"] + "@")
            }
        } catch {}
    }
    & git -C $healthRepo push --quiet $pushTarget health 2>> $logFile
    Assert-Success "git push origin health"

    Log "OK: pushed $destName"
    Write-Result "OK $ts $destName"
    exit 0
} catch {
    $msg = $_.Exception.Message
    Log "FAIL: $msg"
    Write-Result "FAIL $msg"
    exit 1
}
