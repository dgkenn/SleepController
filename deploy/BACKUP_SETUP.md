# Off-box encrypted database backup

## Why this exists

`sleepctl.storage.backup` already keeps rotating **local** snapshots of the SQLite DB in
`.run/backups/` (see "Rotating DB backups" in `deploy/DEBUGGING.md`). That protects you from
DB corruption or a bad write, but it does nothing if the laptop itself dies, is stolen, or the
disk fails — the local backups die with it.

The `dgkenn/SleepController` **code** repo is public on GitHub, which is exactly why the DB
(months of personal HR/HRV/sleep-stage/presence data) must never be pushed to it in the clear.
The design here: encrypt a daily snapshot with [`age`](https://github.com/FiloSottile/age)
before it ever leaves the laptop, then push the **ciphertext only** to a dedicated
`db-backups` branch of that same repo. Anyone can read the blob; nobody but you can decrypt
it, because the private key never lives on this laptop (see step 3 below — this is the whole
point, don't skip it).

This reuses the laptop's existing git credentials (whatever `git push` already uses to reach
GitHub) — no rclone, no separate cloud storage account, no extra secret to provision.

## One-time setup

Run these in an ordinary (non-elevated) PowerShell on the laptop.

### 1. Install `age`

```powershell
winget install FiloSottile.age
```

Verify it's on PATH: `age --version`. (Restart the PowerShell window if it isn't found right
after install — winget updates PATH for new shells, not the current one.)

### 2. Generate a keypair

```powershell
age-keygen -o key.txt
```

This prints something like:

```
Public key: age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpq...
```

`key.txt` (created in your current directory) contains the **private** identity — anyone who
has it can decrypt every backup you ever push. The line printed to the console is the
**public** recipient key — safe to share, it can only encrypt, never decrypt.

### 3. Save the PRIVATE key OFF this laptop — this is the entire point

Copy the **contents of `key.txt`** into a password manager entry (1Password, Bitwarden, etc.)
or a secure cloud note — anywhere that is **not** this laptop and **not** the git repo. Then
delete or securely move `key.txt` off the laptop too (or at minimum don't leave it as the only
copy).

If you skip this step and the laptop dies, every encrypted backup you've pushed becomes
permanently undecryptable garbage — you will have gone to the trouble of encrypting backups
that protect you from *disk loss* while storing the only key to unlock them *on the disk that
could be lost*. The laptop only ever needs the **public** recipient key (step 4) to keep
encrypting new backups; it never needs the private key again unless you're restoring.

### 4. Add the public key to `deploy/.env`

```
BACKUP_AGE_RECIPIENT=age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpq...
```

(Use the "Public key: ..." value from step 2 — the `age1...` string, not the file contents.)
See the commented-out `BACKUP_AGE_RECIPIENT=` line in `deploy/.env.example` for reference.
Until this is set, `scripts/backup-encrypted.ps1` logs "offsite backup not configured" and
exits cleanly (0) — it's a no-op, not an error, so it's safe to leave the always-on watchdog
and boot validation running before you get to this step.

### 5. Register the daily Scheduled Task

Run once (elevated PowerShell not required — this task only needs your own user rights):

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$HOME\SleepController\scripts\backup-encrypted.ps1`""
$trigger = New-ScheduledTaskTrigger -Daily -At 4:00AM
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "SleepController-Backup" -Action $action -Trigger $trigger -Settings $settings -Force
```

Or the equivalent one-liner with `schtasks`:

```powershell
schtasks /Create /TN "SleepController-Backup" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%USERPROFILE%\SleepController\scripts\backup-encrypted.ps1\"" /SC DAILY /ST 04:00 /RL LIMITED /F
```

This is a **separate** task from `SleepController` (the always-on watchdog registered by
`scripts/windows-always-on.ps1`) — it runs once a day, does its job, and exits; it does not
supervise anything and is not touched by the watchdog's restart logic.

Run it once by hand to confirm it works end-to-end before waiting for 4am:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup-encrypted.ps1
Get-Content .run\backup-offsite.result
Get-Content .run\backup-offsite.log -Tail 30
```

A successful run prints/writes `OK <timestamp> <blobname>` to `.run\backup-offsite.result` and
you'll see a new commit on the repo's `db-backups` branch on GitHub.

Undo later with: `Unregister-ScheduledTask -TaskName "SleepController-Backup" -Confirm:$false`

## What gets pushed where

- Branch: `db-backups` on the same `origin` remote as the code (an **orphan** branch — it
  shares no history with `main`, so it never shows up in the code's commit log/PR diffs).
- Files: `sleep-YYYYMMDD-HHMMSS.db.gz.age` (one per run) plus `latest.db.gz.age` (always the
  most recent, for convenience). Pruned to the newest 14 dated blobs each run.
- Content: gzip-compressed, then `age`-encrypted to your `BACKUP_AGE_RECIPIENT` public key.
  Without the matching private key (which you saved off-laptop in step 3), the blob is just
  opaque ciphertext — safe to sit in a public repo.
- The push happens from a **dedicated clone** at `.run\backup-repo` (gitignored, invisible to
  the main working tree's `git status`) — `scripts/backup-encrypted.ps1` never touches your
  actual checkout or its branch.

## Restore runbook

Copy-paste this verbatim when you need it — written for a panicked future-you restoring onto a
**new** machine (the laptop that had the private key backup, not the laptop that died).

```powershell
# 1. Stop services on the box you're restoring TO (so nothing has the DB file open):
#    - if it's the SleepController laptop: Stop-ScheduledTask SleepController ; Get-Process python,node | Stop-Process -Force
#    - Docker deployment: cd deploy && docker compose down

# 2. Get the encrypted blob. Any throwaway clone works -- this does NOT need to be your
#    working checkout:
git clone https://github.com/dgkenn/SleepController.git restore-tmp
cd restore-tmp
git fetch origin db-backups
git checkout db-backups

# List available dated backups (or just use latest.db.gz.age):
dir sleep-*.db.gz.age

# 3. Install age if this machine doesn't have it:
winget install FiloSottile.age

# 4. Put your PRIVATE key (from the password manager / secure note -- step 3 of setup) into a
#    local key.txt, then decrypt + decompress:
age -d -i key.txt latest.db.gz.age | gunzip > sleep.db
# (or, on plain Windows PowerShell without gunzip.exe, decrypt to a .gz first and expand it:)
age -d -i key.txt latest.db.gz.age -o sleep.db.gz
powershell -Command "$in=[System.IO.File]::OpenRead('sleep.db.gz'); $out=[System.IO.File]::Create('sleep.db'); $gz=New-Object System.IO.Compression.GZipStream($in,[System.IO.Compression.CompressionMode]::Decompress); $gz.CopyTo($out); $gz.Dispose(); $out.Dispose(); $in.Dispose()"

# 5. Replace the live DB (path is deploy/.env's SLEEPCTL_DB) with the restored file:
copy sleep.db <path from SLEEPCTL_DB in deploy\.env>

# 6. Restart services:
#    - SleepController laptop: Start-ScheduledTask SleepController
#    - Docker deployment: docker compose up -d
```

The restored DB is a point-in-time snapshot — anything written after that backup was taken is
lost, but the file itself is guaranteed internally consistent (see
`sleepctl/storage/backup.py`'s docstring for why the online backup API is used instead of a
raw file copy).

## Result contract

`scripts/backup-encrypted.ps1` writes exactly one line to `.run\backup-offsite.result` on every
run, and appends a timestamped trail to `.run\backup-offsite.log`:

| Result | Meaning |
|---|---|
| `OK <timestamp> <blobname>` | Snapshot taken, encrypted, and pushed successfully. |
| `SKIPPED <reason>` | `BACKUP_AGE_RECIPIENT` isn't set yet — see step 4 above. Exit code 0 (not an error). |
| `FAIL <reason>` | Something broke (missing `age`, git push failure, snapshot error, ...). Exit code 1 — check `.run\backup-offsite.log` for the full trail. |
