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
