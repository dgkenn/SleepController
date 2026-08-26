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
#   3. keeps both latest.json (always current) + a dated health-<ts>.json history, pruned to 1000
#      (~7 days at the ~10-min publish cadence -- each snapshot is a few KB, so even 1000 of them
#      is a trivial single commit; see the re-rooting note by the commit step below for why the
#      BRANCH stays bounded regardless of how large this number is).
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

# Fail-fast, never HANG: this machine may have no git push credential (an anonymous clone), and a
# bare `git push` would then block FOREVER on Git Credential Manager's interactive prompt -- a
# detached, hidden process just wedges, and a new one stacks up every run. Force git fully
# non-interactive so a missing/invalid credential returns an error immediately, which the try/catch
# below records as FAIL instead of hanging.
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"

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

    # Load deploy\.env into THIS process's environment so env-based diagnostics checks
    # (eight_sleep_creds, etc.) reflect reality even when this script is run BY HAND -- outside the
    # watchdog, which normally loads .env for its children. Without it a manual publish reports
    # "creds not set" purely because the interactive shell never loaded .env, a false DEGRADED. The
    # checks read PRESENCE only, and env values are whitelisted/scrubbed out of the published snapshot.
    foreach ($k in $vars.Keys) { Set-Item -Path "Env:$k" -Value $vars[$k] -ErrorAction SilentlyContinue }

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

    # --- prune to the newest 1000 dated snapshots (latest.json is not counted) -------------------
    # ~7 days of rolling audit history at the ~10-min cadence. Generous on purpose: each snapshot
    # is a few KB (~7KB observed), so even 1000 of them in one commit tree is a few MB -- trivial
    # -- while still being a real bound, unlike the unpruned commit HISTORY this replaces.
    $keepCount = 1000
    $allBlobs = Get-ChildItem -Path $healthRepo -Filter "health-*.json" -ErrorAction SilentlyContinue |
        Sort-Object Name
    if ($allBlobs.Count -gt $keepCount) {
        $excess = $allBlobs | Select-Object -First ($allBlobs.Count - $keepCount)
        foreach ($f in $excess) {
            Remove-Item -Path $f.FullName -Force -ErrorAction SilentlyContinue
            Log "pruned old snapshot $($f.Name)"
        }
    }

    # --- commit as a SINGLE ROOT COMMIT ----------------------------------------------------------
    # This used to be a normal parented commit on top of whatever was already on `health`. Pruning
    # above deletes old snapshots from the WORKING TREE, which is all a normal commit changes --
    # every snapshot ever pushed stays reachable from earlier commits forever. At a ~10-minute
    # cadence that is ~144 new commits/day added to the branch's history, unboundedly, even though
    # the file TREE at any one commit was already capped (previously 200) -- the same shape of bug
    # already found and fixed on db-backups (see backup-encrypted.ps1), just with small JSON
    # instead of a multi-MB blob, so it took longer to notice: by 2026-08-24 this branch alone had
    # accumulated 3600+ commits since 2026-07-14, making every fetch of it (including an off-site
    # Claude session's own `git fetch origin health` to read tonight's data) slower than needed.
    #
    # These are ARTIFACTS, not source: history has no value here, only the current snapshot set
    # does. Re-root the branch on each run so it holds exactly one commit containing exactly the
    # snapshots we want to keep (latest.json + the newest $keepCount dated ones), then force-push.
    # Old objects fall out of reach and are collected remotely. Nothing is lost that the pruned
    # worktree wasn't already discarding.
    # A run that died between the orphan checkout and the rename would leave _health_tmp behind
    # and wedge every subsequent run at "branch already exists". Clear it first; it never holds
    # anything we need, since the snapshots live in the working tree at this point.
    # ONLY delete it if it actually exists. `git branch -D <missing>` writes "error: branch
    # '_health_tmp' not found" to STDERR, and under $ErrorActionPreference='Stop' PowerShell turns
    # a native command's stderr write into a TERMINATING error -- the exact hazard this script
    # already documents for `git add` above. `*> $null` does NOT save you: the redirection is what
    # wraps stderr lines as ErrorRecords in the first place. On the normal path (no leftover temp
    # branch) that made EVERY publish die right here, so health went silent from the moment this
    # re-rooting logic deployed (2026-08-25 18:38) -- and because health is the channel that would
    # have reported the fault, the failure was invisible off-box for hours. Probe with
    # `rev-parse --verify --quiet`, which exits non-zero SILENTLY, and only then delete.
    & git -C $healthRepo rev-parse --verify --quiet "refs/heads/_health_tmp" *> $null
    if ($LASTEXITCODE -eq 0) {
        & git -C $healthRepo branch -D _health_tmp *> $null
    }
    & git -C $healthRepo checkout --quiet --orphan _health_tmp 2>> $logFile
    Assert-Success "git checkout --orphan _health_tmp"
    & git -C $healthRepo add -A 2>> $logFile
    Assert-Success "git add -A"

    $statusOut = & git -C $healthRepo status --porcelain
    if (-not $statusOut) {
        Log "nothing staged (unexpected -- the dated snapshot should always be present); treating as success"
        Write-Result "OK $ts nochange"
        exit 0
    }

    & git -C $healthRepo commit --quiet -m "health $ts (single-commit artifact branch)" 2>> $logFile
    Assert-Success "git commit"
    # Move the branch label onto the new root commit, discarding the old chain locally too, so the
    # dedicated clone doesn't keep growing on this box either.
    & git -C $healthRepo branch --quiet -M _health_tmp health 2>> $logFile
    Assert-Success "git branch -M health"

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
    # --force is REQUIRED, not a shortcut: the branch was just re-rooted, so this push is
    # deliberately not a fast-forward. Safe here for the same reason the re-rooting is -- health
    # holds artifacts only, and every snapshot we intend to keep is in the commit being pushed.
    & git -C $healthRepo push --quiet --force $pushTarget health 2>> $logFile
    Assert-Success "git push --force origin health"

    Log "OK: pushed $destName"
    Write-Result "OK $ts $destName"
    exit 0
} catch {
    $msg = $_.Exception.Message
    Log "FAIL: $msg"
    Write-Result "FAIL $msg"
    exit 1
}
