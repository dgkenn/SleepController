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
