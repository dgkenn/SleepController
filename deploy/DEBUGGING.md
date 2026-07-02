# Debugging: something's wrong

Two paths, depending on whether the dashboard loads at all.

## (a) The dashboard/API loads

Open (or `curl`):

```
https://<your-host>/api/diag?token=<DIAG_TOKEN>
```

The `=== DIAGNOSIS: ... ===` block at the top of the response tells you exactly what's wrong
and the fix — no need to read raw logs. It's followed by the existing `=== STATUS ===` +
daemon/watchdog log tails for when you need more detail.

For a **lossless, machine-parseable** version of the same report (e.g. to grep/diff it, or
paste to Claude without the plaintext formatting getting in the way), add `&format=json`:

```
https://<your-host>/api/diag?token=<DIAG_TOKEN>&format=json
```

That returns the full diagnostics dict: `verdict`, `headline`, `primary_remedy`, and every
individual `checks[]` entry (`id`, `title`, `status`, `detail`, `remedy`).

`DIAG_TOKEN` lives in `deploy/.env`. If it isn't set, `/diag` 404s (by design — it's invisible
to scanners rather than merely unauthenticated).

### What each verdict means

| Verdict    | Meaning                                                                 |
|------------|--------------------------------------------------------------------------|
| `HEALTHY`  | Every check passed. Nothing to do.                                       |
| `DEGRADED` | The daemon + API are up, but something is wrong (no water, thermal stalled, missing creds, a stale calendar, a big log, ...). Read the `[FAIL]`/`[WARN]` lines and their `(fix: ...)`. |
| `DOWN`     | The control daemon isn't reporting in, or (in principle) the API itself failed its own liveness check. The system is not doing anything useful right now — restart is likely needed. |

Each check line is formatted as:

```
[FAIL] daemon_heartbeat: last heartbeat 142s ago (> 90s)  (fix: daemon down — watchdog should restart it; if it keeps flapping, run doctor.ps1)
```

Fails are listed first, then warns, then everything that's fine (`OK`/`INFO`) — so the top of
the block is always the thing to fix first.

## Deep dives: exactly the data, no summarizing

`/diag` gives you a curated verdict. Sometimes that's not enough — you need the *exact* raw
bytes of a log, or to know whether the cloud/device itself is actually responding right now,
independent of whatever the daemon last published. Two more endpoints, gated **exactly like
`/diag`** (same `DIAG_TOKEN`, 404 on missing/wrong token):

### `GET /api/diag/logs` — raw, filtered log fetch

```
https://<your-host>/api/diag/logs?token=<DIAG_TOKEN>&file=daemon&lines=300
```

Returns the last `lines` (default 100, max 1000) lines of the chosen file **verbatim, as
plain text** — never summarized, never re-worded. `file` must be one of a whitelist (mapped
internally to the real filename in `.run/`, so there's no path-traversal surface):

| `file`         | actual file           |
|----------------|------------------------|
| `daemon`       | `daemon.log`           |
| `daemon-err`   | `daemon.err`           |
| `daemon-crash` | `daemon-crash.log`     |
| `watchdog`     | `watchdog.log`         |
| `api`          | `api.log`              |
| `api-err`      | `api.err`              |
| `web`          | `web.log`              |
| `web-build`    | `web-build.log`        |

Add `&grep=<pattern>` to filter that tail window (case-insensitive; tried as a Python regex
first, falls back to a plain substring match if the pattern doesn't compile — so a literal
string like `[WARN]` always works). The response is capped at ~200KB total.

Examples:

```
# pull the last 300 lines of daemon.log, raw
/api/diag/logs?token=<DIAG_TOKEN>&file=daemon&lines=300

# did the watchdog restart anything recently?
/api/diag/logs?token=<DIAG_TOKEN>&file=watchdog&lines=1000&grep=restart
```

An unknown `file` value is rejected with `400`; a whitelisted file that doesn't exist on disk
yet returns the plain-text placeholder `(file not found)` (not an error).

### `GET /api/diag/probe` — live, read-only Eight Sleep round-trip

```
https://<your-host>/api/diag/probe?token=<DIAG_TOKEN>
```

Opens a **fresh, separate, read-only** cloud session (distinct from the daemon's) and does
exactly: `connect()` → a timed `update()` → `read_frame()` + `device_status()` → `close()`.
It never sends a device command (no heating-level change, no power/away/prime) — use this to
confirm the cloud/device is actually responding when `/tonight` or `/diag`'s `runtime_state`
looks stale and you can't tell whether that's the daemon or Eight Sleep's cloud.

Returns JSON:

```json
{
  "ok": true,
  "latency_ms": 812.4,
  "error": null,
  "device": {"online": true, "has_water": true, "priming": false, "needs_priming": false},
  "frame": {"heart_rate": 58, "hrv": 42, "respiratory_rate": 14, "stage": "deep",
            "bed_temp_f": 91.5, "presence": true, "device_level": 10, "target_level": 10,
            "data_age_seconds": 3.0},
  "note": "read-only: opened a brief separate cloud session distinct from the daemon's; sent no device command"
}
```

The whole round-trip runs under a hard ~20s timeout and always closes the session, even on
failure. If credentials are missing, pyEight isn't installed, the cloud call errors, or it
times out, you still get a `200` with `{"ok": false, "error": "..."}` — this endpoint is
designed to never 500 on you.

### `GET /api/diag/history` — 48h(+) runtime-state trend

```
https://<your-host>/api/diag/history?token=<DIAG_TOKEN>&hours=48
```

Both daemons append a throttled (~60s) copy of every `runtime_state` snapshot to a
`state_history` table (rows older than ~7 days are pruned automatically on write). This
endpoint returns those rows, newest-first, as JSON: `state`, `mode`, `target_temp_f`,
`bed_temp_f`, `room_temp_f`, `stage`, `confidence`, `target_level`, `daemon_alive`, `extra`.
Use it to see a *trend* ("was the bed slowly drifting warm all night?") instead of just the
one instant `/diag`'s `=== STATUS ===` block shows. `&limit=` caps the row count (default
2000).

### `GET /api/diag/blackbox` — crash pre-history (flight-recorder dump)

```
https://<your-host>/api/diag/blackbox?token=<DIAG_TOKEN>
```

Each daemon keeps an in-memory ring buffer of its last ~200 ticks (state, decision
summary, key frame fields, any command applied). On an unhandled tick error it's dumped to
`.run/blackbox-<timestamp>.jsonl`; on a clean shutdown it's dumped to
`.run/blackbox-latest.jsonl`. This endpoint returns the most recent of those dumps verbatim
(crash dump preferred over the clean-shutdown one), capped at ~200KB like `/diag/logs`. It's
the fastest way to see exactly what the daemon was seeing/deciding in the seconds before it
died, without needing shell access to the host.

## Rotating DB backups

The engine takes a consistent snapshot of the SQLite DB once a day (via
`sqlite3.Connection.backup()`, not a raw file copy — safe even while the DB is open under
WAL) into `.run/backups/sleep-YYYYMMDD-HHMMSS.db`, keeping the most recent 7 by default. It
runs automatically from each daemon's nightly close-out, and can also be run by hand:

```
sleepctl backup --db <path-to-sleepctl.db> --keep 7
```

**To restore:** stop the dashboard API + daemon (and the watchdog, so it doesn't restart them
mid-swap), then copy the chosen backup file over the live DB path and restart:

```
cp .run/backups/sleep-<timestamp>.db <SLEEPCTL_DB path>
```

A restored DB is a consistent point-in-time snapshot — anything written after that backup was
taken is lost, but the file itself is never a half-written/corrupt copy. See
`sleepctl/storage/backup.py` for the implementation.

### Off-box (encrypted, offsite) backup

The backups above are local to this laptop only — if the laptop dies/is lost, they die with
it. `scripts/backup-encrypted.ps1` (run daily via its own Scheduled Task, separate from the
watchdog) takes the same consistent snapshot, `age`-encrypts it, and pushes the ciphertext to
the repo's dedicated `db-backups` branch — safe to publish because it's unreadable without a
private key that's deliberately kept off this laptop. One-time setup, exact restore commands,
and the `.run\backup-offsite.result` contract are all in `deploy/BACKUP_SETUP.md`.

## (b) The dashboard/API does NOT load

You can't hit `/diag` if the API itself is down. Instead, RDP/console into the Windows host
and run the standalone diagnostic script — it needs nothing but PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
```

It prints one copy-pasteable report: the deployed git commit + dirty-tree state, every
python.exe/node.exe/powershell.exe process (PID, start time, full command line — so duplicate
daemons/watchdogs are obvious), whether ports 8000/3000 are listening, `.run` heartbeat ages,
the last ~15 lines of `watchdog.log`/`daemon.log`/`daemon.err`/`daemon-crash.log`,
`deploy\.env` sanity (which keys are set — secret VALUES are never printed, only `SET`/`MISSING`),
and a live probe of `http://localhost:8000/health`.

**Paste the whole output to Claude** (or whoever's helping debug) — it's designed to be a
complete, self-contained diagnosis on its own, with no secrets in it.

## Notes

- Both diagnostics are read-only and side-effect-free: running them never changes control
  behavior, restarts anything, or touches the device.
- The `/diag` battery reuses the same pure `health_monitor.evaluate_health` that also drives
  the always-on health-alert watchdog (Web Push) — so what you see in `/diag` and what
  triggers a push alert are the same underlying signals, just presented differently.
- If `/diag` says `HEALTHY` but the bed still isn't doing what you expect, that means the
  *infrastructure* (daemon, API, device link) is fine — the issue is in the control decision
  itself. Check `/insights/decisions` and `/tonight/plan` next.

## Watchdog self-healing (restart storms, boot validation, smoke test)

`scripts/windows-watchdog.ps1` now protects itself against the failure mode where a broken
deploy causes a component to crash-loop forever: the watchdog restarts it every ~15s, all
night, without ever surfacing that something's actually wrong.

### Restart-storm limiter

Each of `api` / `daemon` / `web` has its own trailing restart-timestamp history. If a
component is restarted **more than 5 times within a 5-minute window**, the watchdog:

1. logs a line starting `CRITICAL: RESTART STORM: <component> restarted N times in 5 min --
   HOLDING, needs attention` in `.run\watchdog.log`,
2. writes `.run\watchdog.alert` (one line: timestamp + reason),
3. stops restarting that component for a **5-minute cooldown** (it's simply left down —
   the watchdog doesn't touch it again until the cooldown expires).

After the cooldown, the watchdog tries exactly once more. If it storms again, it goes right
back on hold (and the alert stays). Once a component is observed healthy again (port
listening / heartbeat fresh), its storm history is cleared; `.run\watchdog.alert` is removed
automatically once **no** component is currently holding.

Every daemon (re)start is logged with WHY it happened, e.g.:

```
daemon heartbeat 112s stale; restarting (restart #3 in window)
```

### Remote-restart flag (`.run\restart.request`)

A future remote-action endpoint (not built yet) can request a restart by writing a one-line
flag file:

```
.run\restart.request   contents: daemon | api | web | all
```

Each supervise iteration checks for this file FIRST, before anything else. If present, the
watchdog logs `restart requested: <target>`, force-stops the matching process(es) (by the
port they own for api/web, by command-line match for the daemon), deletes the flag file, and
lets the normal loop notice the component is down and restart it on the next pass (no separate
restart path — it reuses the same storm-aware logic as an organic crash). This *does* count
against that component's storm limiter, so a broken remote-restart caller can't thrash the
system either.

### Boot-time validation (`scripts/validate_env.ps1`)

Runs automatically, early, before any service starts. Checks `deploy\.env` for the required
keys (`SLEEPCTL_DB`, `JWT_SECRET`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD` — FATAL if missing),
warns (non-fatal) if `EIGHTSLEEP_EMAIL`/`EIGHTSLEEP_PASSWORD` or `DIAG_TOKEN` are missing,
confirms the venv python exists and `from pyeight.eight import EightSleep` imports, and
confirms the `SLEEPCTL_DB` directory is writable. Writes `.run\validate.result`
(`PASS`/`WARN`/`FAIL` + a `[FAIL]`/`[WARN]`/`[OK]` line per check) and the watchdog echoes it
into `watchdog.log`. A `FAIL` also raises `.run\watchdog.alert` — but the watchdog **still
attempts to start every service**; validation only reports, it never bricks the boot. Can also
be run standalone: `powershell -ExecutionPolicy Bypass -File scripts\validate_env.ps1`.

### Post-restart smoke test (`.run\smoke.result`)

~40 seconds after the watchdog starts supervising, it runs one end-to-end check: `GET
http://localhost:8000/health` returns 200, `.run\daemon.heartbeat` is fresh (< 90s), and port
3000 is listening. Writes `.run\smoke.result` = `SMOKE PASS` or `SMOKE FAIL: <what failed>`
and logs it; a `FAIL` also raises `.run\watchdog.alert`. This turns a broken deploy (bad
build, import error, dead creds) into an immediate, loud failure instead of a silent one
discovered hours later.

### Where to look

`scripts\doctor.ps1` now prints `.run\watchdog.alert` (if active), and the full contents of
`.run\validate.result` / `.run\smoke.result`, plus a **CONNECTIVITY** section: the LAN IP, port
3000 listening state, and (best-effort) `tailscale status` / `tailscale funnel status` /
`tailscale serve status` output — so "the phone can't reach the dashboard" self-diagnoses
(look for an ACTIVE `https://` funnel/serve URL). If the `tailscale` CLI isn't installed it
prints `(tailscale CLI not found)` instead of erroring.

## How to send Claude a diagnostic bundle

The fastest way to get help debugging: generate ONE file with everything relevant and hand it
over. Two ways to get it, depending on whether the API is reachable.

**API is up** — `GET /diag/bundle?token=<DIAG_TOKEN>`:

```
https://<your-host>/api/diag/bundle?token=<DIAG_TOKEN>
```

Returns one clearly-sectioned `text/plain` document (`===== SECTION =====` headers): the full
`/diag` verdict (both the summary and the lossless JSON), recent structured events,
`.run/*.result`/`.run/*.alert` file contents, daemon/watchdog heartbeat ages, a tail of every
whitelisted log (`daemon`, `daemon-err`, `daemon-crash`, `watchdog`, `api`, `api-err`, `web`,
`web-build` — `&lines=` controls how many lines per log, default 150), and a **redacted**
config snapshot (env keys present + non-secret values only). Capped at ~1MB so it stays
paste-friendly; `curl` it straight to a file:

```
curl "https://<your-host>/api/diag/bundle?token=<DIAG_TOKEN>" -o diag-bundle.txt
```

If the text cap would cut something off, add `&format=zip` for an untruncated zip of the same
sections as individual files (`diag.json`, `diagnosis_summary.txt`, `events.json`,
`config_redacted.txt`, `logs/*.log.tail.txt`, `results/*`).

**API is down** — run the standalone PowerShell collector on the host itself:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\collect-diagnostics.ps1
```

It reads the same `.run` logs/results directly (no API needed), applies the identical
redaction rule, and writes `.run\diag-bundle-YYYYMMDD-HHMMSS.txt` — it prints the full path
when done. Paste or upload that file to Claude.

**Redaction rule** (identical in both the API endpoint and the PowerShell script): any config
key whose NAME matches `PASSWORD`, `SECRET`, `TOKEN`, `ICS_URL`, `CLIENT_SECRET`, or `JWT`
(case-insensitive) is always rendered `<redacted>` — its real value is never read into the
bundle. Every other configured key (e.g. `SLEEPCTL_LIVE`, `SLEEPCTL_DRY_RUN`, `TZ`,
`CORS_ORIGINS`) shows its actual, non-secret value so you don't have to separately ask "is
dry-run on?".

## Known-issue playbook

`/diag` and `/diag/bundle` both cross-check the live diagnostics against a small, structured
playbook of issues this project has actually hit — symptom → likely cause → concrete fix —
instead of leaving pattern-matching to memory. When anything matches, `/diag`'s plaintext
output gains a `=== LIKELY CAUSES & FIXES ===` section right under the check list, and the
same matches are in the JSON form under `playbook_matches`.

For the full catalog (every entry the playbook knows about, each annotated with whether it
`matched` right now) plus the shorthand `matches` list on its own:

```
https://<your-host>/api/diag/playbook?token=<DIAG_TOKEN>
```

Gated exactly like `/diag` (404 on missing/wrong `DIAG_TOKEN`). The knowledge lives in
`sleepctl/diagnostics_playbook.py` (engine-side, dashboard-free, unit-tested independently) so
it stays usable from the CLI/tests too, not just the API.

| id | symptom | likely cause | fix |
|----|---------|---------------|-----|
| `water_reservoir_empty` | Bed won't heat or cool / feels completely unresponsive | Hub's water reservoir is empty (`has_water=false`) | Fill the reservoir, then run PRIME (Controls → Prime, or `POST /control/prime`) |
| `watchdog_restart_storm` | A component (api/daemon/web) keeps crash-looping | Watchdog saw >5 restarts of one component in 5 minutes and put it on hold (`.run\watchdog.alert`) | Read the `CRITICAL: RESTART STORM` line in `watchdog.log`, fix the underlying crash in `daemon.err`/`daemon-crash.log`; the hold clears once the component is healthy again |
| `daemon_heartbeat_stale` | Control loop looks stuck | The daemon process is dead/hung — `daemon.heartbeat` stopped updating | Check `daemon.log`/`daemon.err`/`daemon-crash.log`; the watchdog auto-restarts within ~15s, otherwise run `doctor.ps1` |
| `dry_run_left_on` | Live mode is on but the bed never actually moves | `SLEEPCTL_DRY_RUN=1` with `SLEEPCTL_LIVE=1` — decisions are logged, nothing is sent | Unset/clear `SLEEPCTL_DRY_RUN` in `deploy/.env`, restart the daemon |
| `pyeight_auth_failure` | Eight Sleep cloud calls fail with an auth error | Stored token expired or the account password/OAuth secret changed | Verify `EIGHTSLEEP_EMAIL`/`EIGHTSLEEP_PASSWORD` are current; see `deploy/LIVE_POD.md` for OAuth client id/secret accounts |
| `no_credentials_configured` | Daemon runs in SIMULATOR mode unexpectedly | `EIGHTSLEEP_EMAIL`/`EIGHTSLEEP_PASSWORD` not both set | Set both in `deploy/.env`, restart the daemon |
| `db_locked` | Requests fail intermittently; errors mention the database | SQLite locked by two processes (e.g. a stale daemon) writing the same DB file | `doctor.ps1`'s PROCESSES section flags >1 `run_daemon.py`; stop the stale one |
| `port_in_use` | API or web server fails to start | Another process already bound to port 8000/3000 | `doctor.ps1`'s PORTS/PROCESSES sections show the owning PID; stop it |
| `calendar_ics_unreachable` | Work-shift calendar isn't updating | `CALENDAR_ICS_URL` couldn't be fetched — network issue or the secret URL was rotated | Re-copy the ICS URL into `deploy/.env` or the dashboard's calendar settings, then `POST /calendar/refresh` |
| `device_offline` | The Pod/Hub shows offline | Hub reporting offline to Eight Sleep's cloud — network/power issue, or a cloud-side outage | Check the Hub's network/power; check status.eightsleep.com; power-cycle if it stays offline |

None of these are auto-fixed today (`auto_fixable: false` on every entry) — they're all
human actions (fill water, fix creds, restart a process), which isn't something this system
should ever do to your bed without you.
