# SleepController -- OFF-BOX ENCRYPTED database backup (Windows).
#
# The repo (dgkenn/SleepController) is PUBLIC. The SQLite DB holds months of personal
# physiology (HR/HRV/sleep stages/presence) and must never leave this laptop in the clear.
# This script:
#   1. takes a CONSISTENT snapshot of the live DB (reusing sleepctl.storage.backup.run_backup --
#      the same online-backup-API copy the local rotation already uses, safe under WAL),
#   2. gzips + age-ENCRYPTS it (so the ciphertext is safe to publish -- useless without the
#      private key, which never lives on this laptop, see deploy/BACKUP_SETUP.md),
#   3. pushes the encrypted blob to the `db-backups` branch of the SAME public repo, using a
#      dedicated clone under .run\backup-repo so the live working tree is never touched,
#   4. prunes old blobs so the branch doesn't grow forever.
#
# Meant to run unattended once a day from a Scheduled Task (see deploy/BACKUP_SETUP.md for the
# one-time `age` key setup + `schtasks` registration). Every step is defensive: nothing in this
# script is allowed to throw uncaught -- a failure is logged to .run\backup-offsite.log and
# recorded in .run\backup-offsite.result, then the script exits non-zero. The one exception is
# "not configured yet" (BACKUP_AGE_RECIPIENT unset), which exits 0 -- that's not a failure, it's
# a laptop that hasn't been set up for offsite backup yet.
#
# Run it by hand any time:
#   powershell -ExecutionPolicy Bypass -File scripts\backup-encrypted.ps1
#
# Result contract (.run\backup-offsite.result), one line:
#   OK <timestamp> <blobname>        -- pushed successfully
#   SKIPPED <reason>                 -- not configured yet (BACKUP_AGE_RECIPIENT unset); exit 0
#   FAIL <reason>                    -- something broke; exit 1
$ErrorActionPreference = "Stop"

# --- locate the repo root (mirrors doctor.ps1's fallback style) --------------------------------
$Root = Join-Path $HOME "SleepController"
if (-not (Test-Path $Root)) { $Root = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path $Root)) { $Root = (Get-Location).Path }

$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null
$logFile = Join-Path $run "backup-offsite.log"
$resultFile = Join-Path $run "backup-offsite.result"

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

Log "==== offsite backup starting (root=$Root) ===="

try {
    # --- 1. load deploy\.env -------------------------------------------------------------------
    # Same parse style as windows-watchdog.ps1 / doctor.ps1 / validate_env.ps1: a simple
    # KEY=VALUE line matcher into a hashtable (kept local -- we do NOT export these into the
    # process environment, this script has no other need for them).
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

    $recipient = $vars["BACKUP_AGE_RECIPIENT"]
    if (-not $recipient) {
        $msg = "offsite backup not configured -- BACKUP_AGE_RECIPIENT is not set in deploy\.env. Run the one-time setup in deploy\BACKUP_SETUP.md (age-keygen, then add the public key here) to enable it."
        Log $msg
        Write-Result "SKIPPED not configured (BACKUP_AGE_RECIPIENT unset)"
        exit 0
    }
    if ($recipient -notmatch '^age1[a-z0-9]+$') {
        $msg = "BACKUP_AGE_RECIPIENT does not look like an age public key (expected 'age1...'): '$recipient'"
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }

    # --- 2. consistent snapshot via the venv python (mirrors how windows-watchdog.ps1 shells out
    #        to the venv python for one-off DB prep calls) -------------------------------------
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        $msg = "venv python missing ($py) -- run scripts\windows-setup.ps1."
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }

    Log "taking consistent snapshot of $dbPath via sleepctl.storage.backup.run_backup"
    $prevPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
    $pyErrLog = Join-Path $run "backup-offsite-py.err"
    $pyCode = "from sleepctl.storage.backup import run_backup; import sys; print(run_backup(sys.argv[1], keep=7))"
    $pyOut = & $py -c $pyCode $dbPath 2>$pyErrLog
    $pyExit = $LASTEXITCODE
    $env:PYTHONPATH = $prevPythonPath
    if ($pyExit -ne 0) {
        $errText = ""
        if (Test-Path $pyErrLog) { $errText = (Get-Content $pyErrLog -Raw) }
        throw "run_backup failed (exit $pyExit): $errText"
    }
    $pyOut | ForEach-Object { Log "run_backup: $_" }

    # Don't trust stdout parsing alone -- glob the SAME directory run_backup writes to
    # (default_backup_dir: <dirname(db_path)>\.run\backups) and take the newest file. The
    # sleep-YYYYMMDD-HHMMSS.db naming sorts lexicographically == chronologically.
    $dbDir = Split-Path -Parent $dbPath
    if (-not $dbDir) { $dbDir = $Root }
    $backupDir = Join-Path $dbDir ".run\backups"
    $snapshot = Get-ChildItem -Path $backupDir -Filter "sleep-*.db" -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1
    if (-not $snapshot) {
        throw "no sleep-*.db snapshot found in $backupDir after run_backup"
    }
    $snapshotPath = $snapshot.FullName
    Log "snapshot: $snapshotPath"

    # Extract the embedded timestamp for naming the encrypted blob consistently with the
    # snapshot it came from. Falls back to the current time if the filename doesn't match
    # (defensive -- should never happen given the naming convention above).
    $tsMatch = [regex]::Match($snapshot.Name, '^sleep-(\d{8}-\d{6})\.db$')
    if ($tsMatch.Success) {
        $ts = $tsMatch.Groups[1].Value
    } else {
        $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    }

    # --- 3. gzip then age-encrypt --------------------------------------------------------------
    $stagingDir = Join-Path $run "offsite-staging"
    New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null
    $gzPath = Join-Path $stagingDir "sleep-$ts.db.gz"
    $agePath = Join-Path $stagingDir "sleep-$ts.db.gz.age"
    Remove-Item -Path $gzPath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $agePath -Force -ErrorAction SilentlyContinue

    Log "gzipping snapshot -> $gzPath"
    Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $inStream = [System.IO.File]::OpenRead($snapshotPath)
    try {
        $outStream = [System.IO.File]::Create($gzPath)
        try {
            $gzip = New-Object System.IO.Compression.GZipStream($outStream, [System.IO.Compression.CompressionMode]::Compress)
            try {
                $inStream.CopyTo($gzip)
            } finally {
                $gzip.Dispose()
            }
        } finally {
            $outStream.Dispose()
        }
    } finally {
        $inStream.Dispose()
    }
    if (-not (Test-Path $gzPath)) { throw "gzip step produced no output file" }

    $ageCmd = Get-Command age -ErrorAction SilentlyContinue
    if (-not $ageCmd) {
        Remove-Item -Path $gzPath -Force -ErrorAction SilentlyContinue
        $msg = "the 'age' binary is not on PATH -- install it with: winget install FiloSottile.age  (see deploy\BACKUP_SETUP.md)"
        Log "FAIL: $msg"
        Write-Result "FAIL $msg"
        exit 1
    }

    Log "encrypting -> $agePath (recipient=$recipient)"
    & age -r $recipient -o $agePath $gzPath 2>> $logFile
    Assert-Success "age encrypt"
    if (-not (Test-Path $agePath)) { throw "age step produced no output file" }
    Remove-Item -Path $gzPath -Force -ErrorAction SilentlyContinue

    # --- 4. push to db-backups branch via a DEDICATED clone (never touch the live working tree)
    $backupRepo = Join-Path $run "backup-repo"
    if (-not (Test-Path (Join-Path $backupRepo ".git"))) {
        $originUrl = (& git -C $Root remote get-url origin 2>> $logFile)
        Assert-Success "git remote get-url origin"
        $originUrl = $originUrl.Trim()
        Log "cloning $originUrl -> $backupRepo (first run)"
        Remove-Item -Path $backupRepo -Recurse -Force -ErrorAction SilentlyContinue
        & git clone --quiet $originUrl $backupRepo 2>> $logFile
        Assert-Success "git clone"
    }

    Log "fetching origin in dedicated backup clone"
    & git -C $backupRepo fetch origin --quiet 2>> $logFile
    Assert-Success "git fetch origin"

    & git -C $backupRepo rev-parse --verify --quiet "refs/remotes/origin/db-backups" *> $null
    $remoteBranchExists = ($LASTEXITCODE -eq 0)
    & git -C $backupRepo rev-parse --verify --quiet "refs/heads/db-backups" *> $null
    $localBranchExists = ($LASTEXITCODE -eq 0)

    if ($remoteBranchExists) {
        if ($localBranchExists) {
            Log "checking out existing local db-backups branch, syncing to origin/db-backups"
            & git -C $backupRepo checkout --quiet db-backups 2>> $logFile
            Assert-Success "git checkout db-backups"
        } else {
            Log "checking out db-backups tracking origin/db-backups"
            & git -C $backupRepo checkout --quiet -b db-backups origin/db-backups 2>> $logFile
            Assert-Success "git checkout -b db-backups origin/db-backups"
        }
        & git -C $backupRepo reset --hard --quiet origin/db-backups 2>> $logFile
        Assert-Success "git reset --hard origin/db-backups"
    } else {
        if ($localBranchExists) {
            Log "checking out existing local-only db-backups branch (not yet pushed)"
            & git -C $backupRepo checkout --quiet db-backups 2>> $logFile
            Assert-Success "git checkout db-backups"
        } else {
            Log "no db-backups branch anywhere yet -- creating an ORPHAN branch (kept out of main's history)"
            & git -C $backupRepo checkout --quiet --orphan db-backups 2>> $logFile
            Assert-Success "git checkout --orphan db-backups"
            # `checkout --orphan` starts with the previous branch's files still staged/present --
            # clear them all out so db-backups contains ONLY backup blobs, never source code.
            & git -C $backupRepo rm -rf --quiet . *> $null
            Get-ChildItem -Path $backupRepo -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne ".git" } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # --- copy the blob in with a dated filename + refresh latest.db.gz.age --------------------
    $destName = "sleep-$ts.db.gz.age"
    $destPath = Join-Path $backupRepo $destName
    Copy-Item -Path $agePath -Destination $destPath -Force
    Copy-Item -Path $agePath -Destination (Join-Path $backupRepo "latest.db.gz.age") -Force
    Log "staged $destName (+ refreshed latest.db.gz.age) in $backupRepo"

    # --- prune to the newest 14 dated blobs (latest.db.gz.age is not counted) ------------------
    $keepCount = 14
    $allBlobs = Get-ChildItem -Path $backupRepo -Filter "sleep-*.db.gz.age" -ErrorAction SilentlyContinue |
        Sort-Object Name
    if ($allBlobs.Count -gt $keepCount) {
        $excess = $allBlobs | Select-Object -First ($allBlobs.Count - $keepCount)
        foreach ($f in $excess) {
            Remove-Item -Path $f.FullName -Force -ErrorAction SilentlyContinue
            Log "pruned old blob $($f.Name)"
        }
    }

    # --- commit + push ---------------------------------------------------------------------------
    & git -C $backupRepo add -A 2>> $logFile
    Assert-Success "git add -A"

    $statusOut = & git -C $backupRepo status --porcelain
    if (-not $statusOut) {
        Log "nothing changed to commit (unexpected -- the dated blob should always be new); treating as success"
        Write-Result "OK $ts $destName"
        exit 0
    }

    & git -C $backupRepo commit --quiet -m "backup $ts" 2>> $logFile
    Assert-Success "git commit"

    Log "pushing db-backups to origin"
    & git -C $backupRepo push --quiet origin db-backups 2>> $logFile
    Assert-Success "git push origin db-backups"

    Log "OK: pushed $destName"
    Write-Result "OK $ts $destName"
    exit 0
} catch {
    $msg = $_.Exception.Message
    Log "FAIL: $msg"
    Write-Result "FAIL $msg"
    exit 1
}
