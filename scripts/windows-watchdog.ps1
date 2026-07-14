# SleepController -- always-on WATCHDOG (Windows).
# Starts the API + control daemon + web PWA, then supervises them forever: any process that
# dies is restarted within ~15s. Designed to be launched at boot by a Scheduled Task
# (see windows-always-on.ps1). Survives crashes; the Scheduled Task survives reboots.
#
# Uses the PRODUCTION web server (next start) -- far more stable for 24/7 than `next dev`.
$ErrorActionPreference = "Continue"
$Root = Join-Path $HOME "SleepController"
Set-Location $Root

$run = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $run | Out-Null
function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path (Join-Path $run "watchdog.log") -Value $line
    Write-Host $line
}

Log "watchdog starting (root=$Root)"

# --- restart-storm limiter + shared alert marker ---------------------------------------------
# Any single component that keeps dying and getting restarted is a sign of a real problem
# (bad env, crash loop, port fight) that a restart can't fix -- hammering it forever just burns
# the box and hides the real error. Track restart TIMESTAMPS per component; if a component is
# restarted more than $StormThreshold times within $StormWindowSeconds, STOP restarting it for
# $StormCooldownSeconds ("holding"), log a CRITICAL line, and write .run\watchdog.alert so a
# future remote-action / push-alert layer can surface it. After the cooldown we try once more;
# if it storms again it goes right back on hold. The marker is removed once nothing is holding.
$StormWindowSeconds = 300
$StormThreshold = 5
$StormCooldownSeconds = 300
$script:restartHistory = @{ api = @(); daemon = @(); web = @(); tailscale = @() }
$script:stormHold = @{ api = $null; daemon = $null; web = $null; tailscale = $null }
$script:alertFile = Join-Path $run "watchdog.alert"

function Write-Alert([string]$reason) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $reason
    Set-Content -Path $script:alertFile -Value $line -Encoding ASCII
}
function Clear-AlertIfNoneStorming {
    $now = Get-Date
    $anyHolding = $false
    foreach ($k in @($script:stormHold.Keys)) {
        if ($script:stormHold[$k] -ne $null -and $now -lt $script:stormHold[$k]) { $anyHolding = $true }
    }
    if (-not $anyHolding -and (Test-Path $script:alertFile)) {
        Remove-Item -Path $script:alertFile -Force -ErrorAction SilentlyContinue
        Log "all components healthy; cleared watchdog.alert"
    }
}
# Call once per restart DECISION for $component (i.e. right before you would otherwise restart
# it). Returns $true if the restart is allowed to proceed, $false if it's being held due to a
# storm. Handles cooldown expiry (one retry) and recording the attempt into the trailing window.
function Test-CanRestart([string]$component) {
    $now = Get-Date
    if ($script:stormHold[$component] -ne $null) {
        if ($now -lt $script:stormHold[$component]) { return $false }
        Log "$component cooldown elapsed; trying once more"
        $script:stormHold[$component] = $null
        $script:restartHistory[$component] = @()
    }
    $cutoff = $now.AddSeconds(-$StormWindowSeconds)
    $script:restartHistory[$component] = @($script:restartHistory[$component] | Where-Object { $_ -gt $cutoff })
    $script:restartHistory[$component] += $now
    $count = $script:restartHistory[$component].Count
    if ($count -gt $StormThreshold) {
        $winMin = [int]($StormWindowSeconds / 60)
        Log ("CRITICAL: RESTART STORM: {0} restarted {1} times in {2} min -- HOLDING, needs attention" -f $component, $count, $winMin)
        Write-Alert ("RESTART STORM: {0} restarted {1} times in {2} min" -f $component, $count, $winMin)
        $script:stormHold[$component] = $now.AddSeconds($StormCooldownSeconds)
        return $false
    }
    return $true
}
# Call when $component is observed healthy. Resets its storm-tracking state so a later blip
# starts counting fresh, and clears the shared alert file if nothing else is holding.
function Clear-StormState([string]$component) {
    if ($script:stormHold[$component] -ne $null -or $script:restartHistory[$component].Count -gt 0) {
        $script:stormHold[$component] = $null
        $script:restartHistory[$component] = @()
        Clear-AlertIfNoneStorming
    }
}

# --- remote-restart hook -----------------------------------------------------------------------
# A future remote-action endpoint drops a one-line flag file naming what to restart. The watchdog
# side of the protocol: each supervise iteration checks for it FIRST, stops the named component's
# process(es), deletes the flag, then lets the normal loop below notice it's down and restart it.
$script:restartRequestFile = Join-Path $run "restart.request"
function Stop-ComponentProcesses([string]$component) {
    switch ($component) {
        "api" {
            Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
                    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                }
            # Port-based cleanup above is a no-op for a STALE process that isn't (yet/anymore)
            # actually listening -- e.g. hung mid-startup, or dying just as Port-Alive sampled it
            # as down. That's exactly the case the main loop calls us in (Port-Alive already said
            # "not listening"), so without this a lingering api process can survive alongside a
            # freshly spawned one and both end up racing for port 8000. Match by command line
            # (interpreter-path-agnostic) the same way "daemon" below already does.
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'app\.main:app' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        }
        "web" {
            Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
                    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                }
            # Same reasoning as "api" above, for the node-based web process.
            Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'next' -and $_.CommandLine -match 'start' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        }
        "daemon" {
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        }
    }
}
# --- watchdog self-restart --------------------------------------------------------------------
# The per-component restart above can cycle api/web/daemon but NOT the supervisor itself, so a
# change to THIS script (windows-watchdog.ps1) can't take effect remotely without a manual kill.
# Restart-Watchdog closes that gap. MECHANISM CHOSEN: exit non-zero and let the Scheduled Task
# ("SleepController" from windows-always-on.ps1: RestartCount 999 / 1-min RestartInterval,
# -MultipleInstances IgnoreNew) relaunch a fresh watchdog from disk.
#
# We deliberately do NOT Start-Process a replacement watchdog ourselves and exit 0. That would
# work once, but exit 0 reads to the Scheduled Task as "the action succeeded/finished", so the
# task would stop supervising -- silently disabling its RestartCount-999 crash-recovery safety
# net for every FUTURE watchdog crash until the next reboot, AND the self-spawned watchdog would
# no longer be the task-tracked process. Delegating to the task instead keeps that safety net
# fully intact and guarantees a single instance (IgnoreNew drops any overlapping trigger).
# Cost: api/daemon/web run unsupervised for up to ~1 min (the RestartInterval) -- acceptable,
# because they are independent Start-Process children that keep serving during the gap; the
# relaunched watchdog then adopts/cycles them onto the new code via its normal startup sweep.
function Restart-Watchdog {
    Log "watchdog self-restart requested -- exiting (exit 1) so the Scheduled Task relaunches this script fresh from disk"
    exit 1
}
function Handle-RestartRequest {
    if (-not (Test-Path $script:restartRequestFile)) { return }
    $target = $null
    try { $target = (Get-Content -Path $script:restartRequestFile -Raw -ErrorAction Stop).Trim().ToLower() } catch {}
    Remove-Item -Path $script:restartRequestFile -Force -ErrorAction SilentlyContinue
    if (-not $target) { return }
    Log "restart requested: $target"
    switch ($target) {
        "all"    {
            Stop-ComponentProcesses "api"; Stop-ComponentProcesses "web"; Stop-ComponentProcesses "daemon"
            $script:daemonGraceUntil = (Get-Date)   # let Ensure-Daemon re-judge immediately, no grace wait
            # Re-arm the one-shot smoke test for EVERY full restart (not just the very first
            # watchdog-lifetime boot) -- a manual restart=all or a self-update deserves the same
            # end-to-end verification a fresh boot gets. Also what lets a self-update's rollback
            # safety net (Invoke-DeployRollback, via $script:pendingRollback) actually get checked.
            $script:smokeTestAt = (Get-Date).AddSeconds(40)
            $script:smokeTestDone = $false
            Log "restart=all: re-arming smoke test (will re-verify ~40s from now)"
        }
        "api"      { Stop-ComponentProcesses "api" }
        "web"      { Stop-ComponentProcesses "web" }
        "daemon"   { Stop-ComponentProcesses "daemon"; $script:daemonGraceUntil = (Get-Date) }
        "watchdog" { Restart-Watchdog }
        "self"     { Restart-Watchdog }
        default    { Log "restart requested: unknown target '$target' -- ignoring" }
    }
}

# --- remote self-update hook (Claude operator console) ----------------------------------------
# A token-gated API endpoint (POST /diag/action/update) writes .run\update.request containing a
# branch name (read server-side from DEPLOY_BRANCH, default "main") -- the API NEVER runs git or
# a shell itself, it only writes this one flag file. This is the watchdog side of that protocol:
# fetch + hard-reset THIS repo's checkout to origin/<branch> (never an arbitrary remote/URL --
# always this same, already-configured "origin"), validate the result, and only THEN trigger a
# normal restart via the EXISTING .run\restart.request mechanism (reused, not reimplemented --
# this function never kills a process itself). Defensive by construction: any failure here is
# logged as CRITICAL + raises .run\watchdog.alert, and the running system is left untouched (no
# restart) rather than risk restarting into a broken deploy. Whatever happens is recorded to
# .run\update.result for GET /diag/update-status to surface. PS 5.1 compatible (no ??, ?., etc).
$script:updateRequestFile = Join-Path $run "update.request"
$script:updateResultFile = Join-Path $run "update.result"
$script:updateBranchAllowlist = '^[A-Za-z0-9._/-]+$'
# Auto-rollback bookkeeping: set (below) after a self-update's git reset + restart-request
# succeed, to @{ priorSha; branch }; cleared as soon as it's acted on (rollback attempted) or the
# post-restart smoke test PASSES. Invoke-SmokeTest checks this on FAILURE and rolls back to
# priorSha -- see Invoke-DeployRollback.
$script:pendingRollback = $null

function Write-UpdateResult($record) {
    try {
        ($record | ConvertTo-Json -Depth 5) | Set-Content -Path $script:updateResultFile -Encoding UTF8
    } catch {
        Log "WARN: could not write update.result: $_"
    }
}

function Handle-UpdateRequest {
    if (-not (Test-Path $script:updateRequestFile)) { return }
    $branch = $null
    try { $branch = (Get-Content -Path $script:updateRequestFile -Raw -ErrorAction Stop).Trim() } catch {}
    # delete the flag immediately -- a stuck/re-created flag can never loop an update forever
    Remove-Item -Path $script:updateRequestFile -Force -ErrorAction SilentlyContinue
    if (-not $branch) { return }

    if ($branch -notmatch $script:updateBranchAllowlist) {
        Log "CRITICAL: self-update requested branch '$branch' fails the allowlist regex -- ignoring"
        Write-Alert "self-update rejected: branch '$branch' fails the allowlist regex"
        Write-UpdateResult @{
            timestamp = (Get-Date -Format o); branch = $branch; git_ok = $false
            git_output = ""; validate_verdict = ""; restarted = $false
            summary = "rejected: branch name '$branch' fails the allowlist regex"
        }
        return
    }

    # Everything past this point is best-effort: a crash here must never take down the
    # supervise loop that keeps api/daemon/web alive.
    try {
        Log "self-update requested: branch=$branch"
        $ts = Get-Date -Format o
        $gitOutput = @()
        $gitOk = $true

        # Capture the CURRENT commit BEFORE any git command below can move the working tree, so a
        # deploy that fails the post-restart smoke test can be rolled back to exactly this commit
        # (see Invoke-DeployRollback). Best-effort: a failure to capture it just means rollback
        # later degrades to "log CRITICAL, can't auto-revert" instead of throwing here.
        $priorSha = $null
        try {
            $priorSha = (& git -C $Root rev-parse HEAD 2>$null | Select-Object -First 1)
            if ($priorSha) { $priorSha = $priorSha.Trim() }
        } catch { $priorSha = $null }

        $fetchOut = & git -C $Root fetch --prune origin $branch 2>&1
        $gitOutput += $fetchOut
        if ($LASTEXITCODE -ne 0) { $gitOk = $false }

        if ($gitOk) {
            $resetOut = & git -C $Root reset --hard "origin/$branch" 2>&1
            $gitOutput += $resetOut
            if ($LASTEXITCODE -ne 0) { $gitOk = $false }
        }

        $gitOutputText = ($gitOutput | Out-String)
        $gitOutputTail = $gitOutputText
        if ($gitOutputTail.Length -gt 4000) {
            $gitOutputTail = $gitOutputTail.Substring($gitOutputTail.Length - 4000)
        }
        $gitOutputTail -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Log "self-update git: $_" }

        $validateVerdict = "SKIPPED"
        if ($gitOk) {
            $validateScript = Join-Path $Root "scripts\validate_env.ps1"
            if (Test-Path $validateScript) {
                & $validateScript -Root $Root
                $vExit = $LASTEXITCODE
                if ($vExit -ge 2) { $validateVerdict = "FAIL" }
                elseif ($vExit -eq 1) { $validateVerdict = "WARN" }
                else { $validateVerdict = "PASS" }
            } else {
                Log "WARN: scripts\validate_env.ps1 not found after self-update -- treating as WARN"
                $validateVerdict = "WARN"
            }
        }

        $restarted = $false
        if ($gitOk -and $validateVerdict -ne "FAIL") {
            # reuse the EXISTING restart.request protocol -- never kill a process directly here
            Set-Content -Path $script:restartRequestFile -Value "all" -Encoding ASCII
            $restarted = $true
            # Arm the rollback safety net. Handle-RestartRequest's "all" case (below, same tick)
            # re-arms the one-shot smoke test, which will check $script:pendingRollback on FAILURE
            # and revert to $priorSha -- see Invoke-DeployRollback.
            $script:pendingRollback = @{ priorSha = $priorSha; branch = $branch }
            $summary = "update to '$branch' succeeded (validate=$validateVerdict) -- restart requested"
            Log "self-update: $summary"
        } else {
            $reason = if (-not $gitOk) { "git fetch/reset failed" } else { "validate_env reported FAIL" }
            $summary = "update to '$branch' FAILED ($reason) -- leaving the running system as-is"
            Log "CRITICAL: self-update: $summary"
            Write-Alert "self-update FAILED for branch '$branch': $reason -- see .run\update.result"
        }

        Write-UpdateResult @{
            timestamp = $ts; branch = $branch; git_ok = $gitOk; git_output = $gitOutputTail
            validate_verdict = $validateVerdict; restarted = $restarted; summary = $summary
        }
    } catch {
        Log "CRITICAL: self-update handler crashed: $_"
        Write-Alert "self-update handler crashed: $_"
        try {
            Write-UpdateResult @{
                timestamp = (Get-Date -Format o); branch = $branch; git_ok = $false
                git_output = "$_"; validate_verdict = "ERROR"; restarted = $false
                summary = "self-update handler crashed: $_"
            }
        } catch {}
    }
}

# --- remote web-rebuild hook (Claude operator console) ----------------------------------------
# A token-gated API endpoint (POST /diag/action/rebuild-web) writes .run\webbuild.request -- the
# API NEVER runs npm or a shell itself, it only touches this one flag file. This is the watchdog
# side: each supervise tick, if the flag is present, delete it and run the Next.js PRODUCTION
# build (`npm run build`) in dashboard\web. CRUCIAL GUARD: web is NOT touched until the build
# exits 0 -- a FAILED build leaves the currently-serving web process completely untouched (no
# downtime on a bad build). Only on a green build do we Stop-ComponentProcesses "web" so the
# supervise loop relaunches `next start` on the fresh .next. Whatever happens is recorded to
# .run\webbuild.result for GET /diag/webbuild-status, appended to web-build.log, and failures are
# logged CRITICAL + raise .run\watchdog.alert. Best-effort by construction: a crash here must
# never take down the supervise loop that keeps api/daemon/web alive. PS 5.1 compatible.
$script:webBuildRequestFile = Join-Path $run "webbuild.request"
$script:webBuildResultFile = Join-Path $run "webbuild.result"

function Write-WebBuildResult($record) {
    try {
        ($record | ConvertTo-Json -Depth 5) | Set-Content -Path $script:webBuildResultFile -Encoding UTF8
    } catch {
        Log "WARN: could not write webbuild.result: $_"
    }
}

function Handle-WebBuildRequest {
    if (-not (Test-Path $script:webBuildRequestFile)) { return }
    # delete the flag immediately -- a stuck/re-created flag must never loop a build forever
    Remove-Item -Path $script:webBuildRequestFile -Force -ErrorAction SilentlyContinue

    try {
        Log "remote web rebuild requested -- running production 'npm run build' in dashboard\web"
        $ts = Get-Date -Format o
        $webDir = Join-Path $Root "dashboard\web"
        $stdoutFile = Join-Path $run "webbuild.out"
        $stderrFile = Join-Path $run "webbuild.err"

        # Run the build in its own working directory via Start-Process -Wait (do NOT Set-Location
        # here -- the supervise loop launches api/daemon with paths relative to $Root, so mutating
        # this process's cwd would break their relaunch). Blocks for the build's duration.
        $proc = Start-Process -FilePath $npm -ArgumentList @("run", "build") `
            -WorkingDirectory $webDir -WindowStyle Hidden -PassThru -Wait `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $buildExit = $proc.ExitCode
        if ($null -eq $buildExit) { $buildExit = -1 }   # PS 5.1: guard against a null ExitCode

        $outText = ""
        try { $outText = (Get-Content -Path $stdoutFile -Raw -ErrorAction SilentlyContinue) } catch {}
        $errText = ""
        try { $errText = (Get-Content -Path $stderrFile -Raw -ErrorAction SilentlyContinue) } catch {}
        $combined = "$outText`n$errText"
        # keep the running history in web-build.log (what /diag/logs?file=web-build tails)
        Add-Content -Path (Join-Path $run "web-build.log") `
            -Value ("=== remote rebuild {0} (exit={1}) ===`n{2}" -f $ts, $buildExit, $combined)

        $outputTail = $combined
        if ($outputTail.Length -gt 4000) {
            $outputTail = $outputTail.Substring($outputTail.Length - 4000)
        }

        $ok = ($buildExit -eq 0)
        if ($ok) {
            # Build succeeded on the FRESH source -- only NOW cycle web so `next start` picks up
            # the new .next. Reuse the existing restart plumbing: the supervise loop below sees
            # port 3000 quiet and relaunches web via Start-Web. Web was never touched before this
            # point, so a failed build (handled in the else) leaves the current web serving.
            Stop-ComponentProcesses "web"
            $summary = "web rebuild succeeded (exit=0) -- restarting web onto the fresh build"
            Log "web rebuild: $summary"
        } else {
            $summary = "web rebuild FAILED (exit=$buildExit) -- leaving the current web serving untouched"
            Log "CRITICAL: web rebuild: $summary"
            Write-Alert "web rebuild FAILED (exit=$buildExit) -- see .run\webbuild.result / web-build.log"
        }

        Write-WebBuildResult @{
            timestamp = $ts; exit_code = $buildExit; ok = $ok
            output = $outputTail; summary = $summary
        }
    } catch {
        Log "CRITICAL: web rebuild handler crashed: $_"
        Write-Alert "web rebuild handler crashed: $_"
        try {
            Write-WebBuildResult @{
                timestamp = (Get-Date -Format o); exit_code = -1; ok = $false
                output = "$_"; summary = "web rebuild handler crashed: $_"
            }
        } catch {}
    }
}

# --- load deploy\.env into the environment ---
$envPath = Join-Path $Root "deploy\.env"
if (-not (Test-Path $envPath)) { Log "FATAL: deploy\.env missing -- run windows-setup.ps1 first."; exit 1 }
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') { Set-Item -Path ("env:" + $matches[1].Trim()) -Value $matches[2].Trim() }
}
$env:PYTHONPATH = "$Root;$Root\dashboard\api;$Root\pyEight"
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Log "FATAL: venv python missing ($py) -- run windows-setup.ps1."; exit 1 }
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) { $npm = "npm.cmd" }  # fall back to PATH

# --- boot-time config validation: fail LOUDLY now, not silently at 3am -------------------------
# Never blocks startup (a broken deploy is easier to fix with the dashboard at least attempting
# to come up) -- but a FATAL result is logged as CRITICAL and raises .run\watchdog.alert so it's
# impossible to miss.
Log "running startup config validation (scripts\validate_env.ps1)"
$validateScript = Join-Path $Root "scripts\validate_env.ps1"
if (Test-Path $validateScript) {
    & $validateScript -Root $Root
    $validateExit = $LASTEXITCODE
    $validateResultPath = Join-Path $run "validate.result"
    if (Test-Path $validateResultPath) {
        Get-Content $validateResultPath | ForEach-Object { Log "validate_env: $_" }
    }
    if ($validateExit -ge 2) {
        Log "CRITICAL: validate_env reported FATAL config problems -- starting anyway, but this needs attention"
        Write-Alert "validate_env FATAL at startup -- see .run\validate.result"
    } elseif ($validateExit -eq 1) {
        Log "validate_env: WARN (non-fatal) -- see .run\validate.result for details"
    } else {
        Log "validate_env: PASS"
    }
} else {
    Log "WARN: scripts\validate_env.ps1 not found -- skipping startup validation"
}

# --- open port 3000 on the Private network (best-effort; needs admin) ---
try {
    if (-not (Get-NetFirewallRule -DisplayName "SleepController 3000" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "SleepController 3000" -Direction Inbound `
            -LocalPort 3000 -Protocol TCP -Action Allow -Profile Private -ErrorAction Stop | Out-Null
        Log "added firewall rule for port 3000"
    }
} catch { Log "firewall rule skipped (run once as admin if the phone can't connect): $_" }

# --- clean up ORPHANS from a previous run (they'd hold ports 8000/3000 and block the fresh
# start, and would still be serving stale code/env). The watchdog runs elevated via the task,
# so it can kill them even when a normal shell can't. This makes a plain task restart clean --
# no reboot needed to pick up new code or a changed deploy\.env.
foreach ($port in 8000, 3000) {
    try {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
                Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                Log "cleaned up stale process $_ on port $port"
            }
    } catch {}
}
# The control daemon holds NO listening port, so the port sweep above misses it. Kill any stale
# run_daemon.py from a previous run too -- otherwise it survives the restart and the supervise
# loop would see a "live" daemon running old code / a stale deploy\.env.
try {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Log "cleaned up stale daemon process $($_.ProcessId)"
        }
} catch {}

# --- one-time prep: DB + login user, and a PRODUCTION web build if missing ---
Log "preparing database + login user"
& $py -c "from app.db import connect; from app.security import ensure_bootstrap_user; connect(); ensure_bootstrap_user(); print('db ready')" 2>&1 | ForEach-Object { Log "db: $_" }

if (-not (Test-Path (Join-Path $Root "dashboard\web\.next"))) {
    Log "building the web app for production (first run; this takes a few minutes)"
    Push-Location (Join-Path $Root "dashboard\web")
    & $npm run build *>> (Join-Path $run "web-build.log")
    Pop-Location
    Log "web build complete"
}

# --- service starters (each returns a Process via -PassThru) ---
function Start-Api {
    Start-Process -FilePath $py -WindowStyle Hidden -PassThru `
        -ArgumentList @("-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--app-dir","dashboard\api") `
        -RedirectStandardOutput "$run\api.log" -RedirectStandardError "$run\api.err"
}
function Start-Daemon {
    # live mode + dry-run come from deploy\.env (SLEEPCTL_LIVE / SLEEPCTL_DRY_RUN)
    Start-Process -FilePath $py -WindowStyle Hidden -PassThru `
        -ArgumentList @("dashboard\daemon\run_daemon.py") `
        -RedirectStandardOutput "$run\daemon.log" -RedirectStandardError "$run\daemon.err"
}
function Start-Web {
    $env:API_URL = "http://localhost:8000"; $env:PORT = "3000"
    Start-Process -FilePath $npm -WindowStyle Hidden -PassThru `
        -WorkingDirectory (Join-Path $Root "dashboard\web") `
        -ArgumentList @("run","start","--","-H","0.0.0.0","-p","3000") `
        -RedirectStandardOutput "$run\web.log" -RedirectStandardError "$run\web.err"
}

# --- supervise by REALITY -------------------------------------------------------------------
# api/web: their listening port. Daemon: a HEARTBEAT FILE it rewrites every ~2s. The previous
# approaches (a Start-Process handle, then a CIM command-line query) both FLAPPED in the
# scheduled-task context -- the query intermittently returned 0 or 2 for one healthy daemon, so
# the watchdog spuriously started duplicates and then killed a working daemon (and the in-progress
# self-test) every ~20s. A file's mtime is unambiguous: fresh (< 90s) => the daemon is alive.
function Port-Alive([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}
$script:daemonHb = Join-Path $run "daemon.heartbeat"
$script:daemonGraceUntil = (Get-Date)   # after a (re)start, wait this long for the first beat
function Daemon-Alive {
    if (-not (Test-Path $script:daemonHb)) { return $false }
    return ((New-TimeSpan -Start (Get-Item $script:daemonHb).LastWriteTime -End (Get-Date)).TotalSeconds -lt 90)
}

# --- wedge detection: tick-progress staleness (beyond the thread heartbeat) ---------------------
# Daemon-Alive above only proves the OS THREAD that touches daemon.heartbeat is still running --
# that thread is deliberately independent of the asyncio event loop (see live_daemon.py's
# _heartbeat_thread) so it keeps beating even if the event loop itself DEADLOCKS. That's correct
# for surviving a long blocking call, but it also means a truly wedged event loop would otherwise
# never be detected. runtime_state.updated (the SQLite row live_daemon.py writes every control/
# command/telemetry tick) only advances when a tick actually completes -- so heartbeat-fresh +
# tick-progress-stale together mean "process alive, event loop stuck." Threshold is deliberately
# generous (5 min): the on-bed self-test can run ~10 min, but it streams progress via on_progress
# callbacks that refresh runtime_state.updated at least every ~60-90s (see sleepctl/loop/
# self_test.py's `_emit()` calls inside its long polling windows), so 5 min of total silence only
# trips on a REAL wedge, never a legitimate self-test. Throttled to avoid spawning python every
# 15s tick forever -- a wedge is a slow-developing condition, a minute of extra detection latency
# doesn't matter.
$script:daemonTickStaleSeconds = 300
$script:daemonTickCheckEveryS = 60
$script:daemonTickCheckAt = (Get-Date)
function Get-DaemonTickAgeSeconds {
    # Returns the age (seconds) of runtime_state.updated, or $null if it can't be determined
    # (DB missing/locked, python error, no row yet) -- callers MUST treat $null as "unknown", never
    # as "stale", so a transient read hiccup can never itself trigger a restart.
    try {
        $out = & $py -c "from app.db import connect; from app.bridge import read_runtime_state; rt = read_runtime_state(connect(), 10**9); print(rt.get('updated') or 'NONE')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $line = ($out | Select-Object -Last 1).ToString().Trim()
        if (-not $line -or $line -eq "NONE") { return $null }
        $ts = [datetime]::Parse($line, [System.Globalization.CultureInfo]::InvariantCulture,
                                 [System.Globalization.DateTimeStyles]::RoundtripKind)
        return [int]((Get-Date).ToUniversalTime() - $ts.ToUniversalTime()).TotalSeconds
    } catch {
        return $null
    }
}
function Daemon-Wedged {
    if ((Get-Date) -lt $script:daemonTickCheckAt) { return $false }   # not due -- default: not wedged
    $script:daemonTickCheckAt = (Get-Date).AddSeconds($script:daemonTickCheckEveryS)
    $age = Get-DaemonTickAgeSeconds
    if ($null -eq $age) { return $false }   # unknown reading -- never restart on it
    return ($age -gt $script:daemonTickStaleSeconds)
}

function Ensure-Daemon {
    if ((Get-Date) -lt $script:daemonGraceUntil) { return }   # let a fresh daemon connect + beat
    $wedged = Daemon-Wedged
    if ((Daemon-Alive) -and -not $wedged) { Clear-StormState "daemon"; return }
    if (-not (Test-CanRestart "daemon")) { return }   # storming -- held; surfaced via CRITICAL log + alert
    $age = -1
    if (Test-Path $script:daemonHb) {
        $age = [int]((New-TimeSpan -Start (Get-Item $script:daemonHb).LastWriteTime -End (Get-Date)).TotalSeconds)
    }
    $ageDesc = if ($wedged) {
        "daemon tick-progress stale beyond ${script:daemonTickStaleSeconds}s (event loop wedged -- heartbeat thread still alive)"
    } elseif ($age -ge 0) { "daemon heartbeat ${age}s stale" } else { "daemon heartbeat missing" }
    Log ("{0}; restarting (restart #{1} in window)" -f $ageDesc, $script:restartHistory["daemon"].Count)
    # best-effort: clear any lingering run_daemon before starting a clean one
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_daemon\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    Start-Daemon | Out-Null
    $script:daemonGraceUntil = (Get-Date).AddSeconds(45)  # don't re-judge until it can beat
}

# --- tailscale/funnel self-heal -----------------------------------------------------------------
# Targets the "502 then TLS failure" outage signature: tailscale's backend drops, or the funnel
# (HTTPS ingress that exposes port 3000) goes stale, while api/daemon/web all stay perfectly
# healthy -- silently cutting off remote/phone access with nothing LOCAL to notice. Checked once
# per supervise tick; on a bad reading, try `tailscale up` + `tailscale funnel --bg 3000` once,
# then fold into the SAME storm/backoff bookkeeping as api/daemon/web (Test-CanRestart/
# Clear-StormState, extended above with a "tailscale" key) so a persistently broken tailscale
# can't hot-loop `tailscale up` forever -- it holds + raises CRITICAL exactly like any other
# repeatedly-failing component. No-op (cleanly) if tailscale isn't installed.
$script:tailscaleCmd = (Get-Command tailscale -ErrorAction SilentlyContinue)
function Tailscale-Healthy {
    if (-not $script:tailscaleCmd) { return $true }   # not installed -- nothing to supervise
    try {
        $statusJson = & tailscale status --json 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $statusJson) { return $false }
        $status = ($statusJson | Out-String) | ConvertFrom-Json -ErrorAction Stop
        if ($status.BackendState -ne "Running") { return $false }
    } catch { return $false }
    try {
        $funnelOut = (& tailscale funnel status 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { return $false }
        if ($funnelOut -notmatch 'https://') { return $false }   # no active https funnel entry
    } catch { return $false }
    return $true
}
function Ensure-Tailscale {
    if (-not $script:tailscaleCmd) { return }
    if (Tailscale-Healthy) { Clear-StormState "tailscale"; return }
    if (-not (Test-CanRestart "tailscale")) { return }   # storming -- held; CRITICAL logged by Test-CanRestart
    Log ("tailscale/funnel unhealthy; attempting self-heal (attempt #{0} in window)" -f $script:restartHistory["tailscale"].Count)
    try { & tailscale up *> $null } catch { Log "WARN: 'tailscale up' failed: $_" }
    try { & tailscale funnel --bg 3000 *> $null } catch { Log "WARN: 'tailscale funnel --bg 3000' failed: $_" }
}

# --- post-restart smoke test --------------------------------------------------------------------
# Once things have had ~40s to come up, verify the whole stack actually works end to end -- this
# catches a broken deploy (bad build, import error, wrong port, dead creds) immediately instead of
# discovering it hours later. Runs once per arm (see Handle-RestartRequest's "all" case and
# Handle-UpdateRequest, both of which re-arm $script:smokeTestAt/$script:smokeTestDone), on
# whichever supervise pass first crosses the armed deadline, so it never blocks the loop with an
# up-front sleep.
function Invoke-SmokeTest {
    $failures = @()
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5 -ErrorAction Stop
        if (-not $resp.ok) { $failures += "api /health responded but ok!=true" }
    } catch {
        $failures += "api /health unreachable: $($_.Exception.Message)"
    }
    if (-not (Daemon-Alive)) { $failures += "daemon heartbeat stale/missing" }
    if (-not (Port-Alive 3000)) { $failures += "web not listening on port 3000" }

    $resultPath = Join-Path $run "smoke.result"
    if ($failures.Count -eq 0) {
        Set-Content -Path $resultPath -Value "SMOKE PASS" -Encoding ASCII
        Log "smoke test: SMOKE PASS"
        $script:pendingRollback = $null   # this deploy is verified good -- nothing to roll back
    } else {
        $msg = "SMOKE FAIL: " + ($failures -join "; ")
        Set-Content -Path $resultPath -Value $msg -Encoding ASCII
        Log "smoke test: $msg"
        Write-Alert $msg
        if ($script:pendingRollback -ne $null) { Invoke-DeployRollback }
    }
}

# --- deploy rollback: undo a self-update that fails its post-restart smoke test -----------------
# Handle-UpdateRequest captured the pre-update commit ($priorSha) and armed $script:pendingRollback
# BEFORE resetting to the new branch; if the smoke test that follows the resulting restart fails,
# revert to that exact commit and restart once more so the box lands back on the last-known-good
# deploy instead of serving a broken one until a human notices. Never retries more than once per
# update (pendingRollback is cleared unconditionally below) -- a rollback target that ALSO fails
# its smoke test just logs CRITICAL again on that next pass rather than bouncing forever.
function Invoke-DeployRollback {
    $rb = $script:pendingRollback
    $script:pendingRollback = $null
    if (-not $rb -or -not $rb.priorSha) {
        Log "CRITICAL: smoke test failed after self-update but no prior commit was captured -- cannot auto-rollback; needs manual attention"
        Write-Alert "smoke test FAILED after self-update; no prior SHA captured -- manual rollback needed"
        return
    }
    Log "CRITICAL: smoke test FAILED after self-update to '$($rb.branch)' -- rolling back to prior commit $($rb.priorSha)"
    try {
        $resetOut = & git -C $Root reset --hard $rb.priorSha 2>&1
        $resetOk = ($LASTEXITCODE -eq 0)
        ($resetOut | Out-String) -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Log "rollback git: $_" }
        if (-not $resetOk) {
            Log "CRITICAL: rollback 'git reset --hard $($rb.priorSha)' FAILED -- system is left on the broken deploy"
            Write-Alert "auto-rollback FAILED (git reset to $($rb.priorSha) failed) -- system is on a broken deploy, needs manual attention"
            return
        }
        Write-Alert "auto-rolled-back self-update (branch '$($rb.branch)') after smoke test failure -- reverted to $($rb.priorSha)"
        # reuse the EXISTING restart.request protocol -- never kill a process directly here
        Set-Content -Path $script:restartRequestFile -Value "all" -Encoding ASCII
        Log "rollback: restart requested to bring the reverted build up (smoke test re-arms automatically)"
    } catch {
        Log "CRITICAL: rollback handler crashed: $_"
        Write-Alert "auto-rollback handler crashed: $_"
    }
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
       Select-Object -First 1).IPAddress
Log "supervising; iPhone URL (same WiFi): http://${ip}:3000  login=$($env:DASHBOARD_USER)  live=$($env:SLEEPCTL_LIVE) dry_run=$($env:SLEEPCTL_DRY_RUN)"

$script:smokeTestAt = (Get-Date).AddSeconds(40)
$script:smokeTestDone = $false

# --- remote-visibility: how often to publish the health snapshot to GitHub ---------------------
# The operator's sandbox cannot reach this box's Tailscale funnel (network egress policy), but
# BOTH sides can reach GitHub -- so every $healthPublishEveryMin minutes the watchdog fires the
# publish-health script DETACHED (see the loop below). First push a couple minutes after boot so
# the components have settled.
$script:healthPublishEveryMin = 10
$script:healthPublishAt = (Get-Date).AddMinutes(2)

while ($true) {
    # Update BEFORE restart so a same-tick "all" restart request written by Handle-UpdateRequest
    # (on a successful update) is picked up immediately by Handle-RestartRequest below, instead
    # of waiting for the next ~15s tick.
    Handle-UpdateRequest
    Handle-RestartRequest
    # Web rebuild AFTER restart handling: on a green build it stops web here, and the web check
    # below (same tick) relaunches it on the fresh .next. A failed build never touches web.
    Handle-WebBuildRequest

    if (-not (Port-Alive 8000)) {
        if (Test-CanRestart "api") {
            Log ("api not listening; starting (restart #{0} in window)" -f $script:restartHistory["api"].Count)
            Stop-ComponentProcesses "api"   # best-effort: clear any lingering api before starting a clean one
            Start-Api | Out-Null; Start-Sleep -Seconds 3
        }
    } else {
        Clear-StormState "api"
    }

    Ensure-Daemon

    if (-not (Port-Alive 3000)) {
        if (Test-CanRestart "web") {
            Log ("web not listening; starting (restart #{0} in window)" -f $script:restartHistory["web"].Count)
            Stop-ComponentProcesses "web"   # best-effort: clear any lingering web before starting a clean one
            Start-Web | Out-Null; Start-Sleep -Seconds 3
        }
    } else {
        Clear-StormState "web"
    }

    Ensure-Tailscale

    if (-not $script:smokeTestDone -and (Get-Date) -ge $script:smokeTestAt) {
        Invoke-SmokeTest
        $script:smokeTestDone = $true
    }

    # --- off-box dead-man's-switch (e.g. healthchecks.io) ---------------------------------------
    # If ALL supervised components (api, daemon, web) are healthy THIS cycle, ping HEALTHCHECKS_URL
    # (read from deploy\.env, loaded into $env: at startup above). If ANY of them is down/unhealthy,
    # SKIP the ping on purpose -- a MISSED ping is what triggers the external alert, so this single
    # mechanism reports BOTH "the box is completely dark" (no pings at all -- power/network/OS
    # failure) and "the box is up but broken" (a component down, so pings stop even though the
    # laptop itself is fine). Cleanly a no-op if HEALTHCHECKS_URL is unset/blank. Best-effort: a
    # network hiccup reaching the external service must never affect the supervise loop.
    if ($env:HEALTHCHECKS_URL) {
        $allHealthy = (Port-Alive 8000) -and (Daemon-Alive) -and (Port-Alive 3000)
        if ($allHealthy) {
            try {
                Invoke-RestMethod -Uri $env:HEALTHCHECKS_URL -Method Post -TimeoutSec 10 -ErrorAction Stop | Out-Null
            } catch {
                Log "WARN: healthcheck ping failed: $_"
            }
        }
    }

    # --- remote-visibility: publish scrubbed operational-health snapshot to GitHub --------------
    # An off-site Claude cannot reach this box's funnel (egress policy) but CAN read GitHub, and
    # this box can push to GitHub -- so every ~10 min push a secrets-free / biometrics-free health
    # snapshot (the /diag diagnosis) to the orphan `health` branch. That gives the operator remote
    # VISIBILITY; ACTION still flows back through the existing self-update path (a merged fix on
    # main is pulled by Handle-UpdateRequest). Launched DETACHED via Start-Process so a slow or
    # hung git push can NEVER stall this supervise loop -- publish-health.ps1 records its own
    # verdict in .run\health-publish.result. Cleanly a no-op if the script isn't present yet
    # (e.g. an older checkout that hasn't self-updated to this version).
    if ((Get-Date) -ge $script:healthPublishAt) {
        $script:healthPublishAt = (Get-Date).AddMinutes($script:healthPublishEveryMin)
        try {
            $hp = Join-Path $Root "scripts\publish-health.ps1"
            if (Test-Path $hp) {
                Start-Process -FilePath "powershell" -WindowStyle Hidden `
                    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $hp) | Out-Null
            }
        } catch {
            Log "WARN: could not launch publish-health: $_"
        }
    }

    Set-Content -Path (Join-Path $run "watchdog.heartbeat") -Value (Get-Date -Format o)
    Start-Sleep -Seconds 15
}
