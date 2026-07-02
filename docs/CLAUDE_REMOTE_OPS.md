# Claude operator console — remote troubleshooting runbook

This is the operator guide for a **fresh Claude session** that has only two things: the
Tailscale funnel base URL for this deployment, and the `DIAG_TOKEN` secret. No repo checkout,
no RDP/SSH, no dashboard login required. Everything below is reachable over plain HTTPS.

```
BASE = https://<funnel-hostname>          # placeholder -- get the real one from the operator
TOKEN = <DIAG_TOKEN value>                # placeholder -- NEVER write the real value in a doc/commit
```

Every endpoint in this doc is called as `?token=$TOKEN` (query parameter) — that's the entire
auth mechanism for the `/diag*` surface. There is no header, no cookie, no login step. A
missing or wrong token gets a `404` (not `401`/`403`) on purpose, so the surface is invisible to
casual scanners.

## The loop

1. **`GET /diag/manifest?token=$TOKEN`** — learn the surface. Returns every `/diag*` endpoint
   (method, params, description, example), the key session-auth operational endpoints (for
   context — you can't call those with just `$TOKEN`), and a `coverage` map naming this
   project's 12 diagnostics features and which endpoint serves each one. Do this first, once,
   so nothing downstream needs to be guessed.
2. **`GET /diag/all?token=$TOKEN`** — total system context in one fetch: health verdict +
   checks, the known-issue playbook (matches + full catalog), live runtime state + device
   status, redacted config, heartbeats, recent structured events, 48h state-history trend, a
   black-box crash-dump pointer, the last self-test report, short log tails, DB backup
   status, and `project_state` — a snapshot of every OTHER subsystem in the project (see below).
3. **Read `playbook_matches`** in that response. Each match is `{symptom, likely_cause, fix}` —
   this is the fastest path from "something's wrong" to "here's the fix" without reading raw
   logs. If nothing matched, read `checks` for the first `fail`/`warn` and its `remedy`.
4. **Take the matching action** — see the action table below. Every action response has the
   uniform shape `{ok, action, result, verify_with}`.
5. **Re-fetch `verify_with`** (or just `GET /diag/all` again) to confirm the fix actually
   worked — don't assume an action succeeded just because the HTTP call returned 200.

If you get stuck reading raw material instead of a summary, `GET /diag/bundle?token=$TOKEN`
(or `&format=zip`) produces one paste-ready "send this to Claude" document — the same
aggregation `/diag/all` is built from, rendered as text instead of JSON.

## Every endpoint, one line each

Token-gated (`?token=$TOKEN`, 404 on missing/wrong token) unless noted otherwise:

| Endpoint | What it does |
|---|---|
| `GET /diag` | Self-diagnosis battery: verdict + checks + device/live status + log tails (`?format=json` for the structured dict). |
| `GET /diag/events` | Structured event-log timeline, filterable by category/severity/since. |
| `GET /diag/logs` | Raw, unsummarized tail of one whitelisted log file. |
| `GET /diag/probe` | Fresh READ-ONLY Eight Sleep cloud round-trip, bypassing runtime_state. |
| `GET /diag/history` | 48h+ runtime-state trend (newest-first). |
| `GET /diag/blackbox` | Most recent flight-recorder dump (last ~200 ticks before a crash). |
| `GET /diag/bundle` | One-artifact "send this to Claude" bundle (text or `?format=zip`). |
| `GET /diag/playbook` | Known-issue playbook catalog, each entry flagged if currently matched. |
| `POST /diag/repair` | One-click safe self-repair battery. Idempotent. |
| `POST /diag/action/restart` | Request a component restart (`target=daemon\|api\|web\|all`). |
| `POST /diag/action/reconnect` | Enqueue a benign `safe_default` re-init (deduped). |
| `GET /diag/morning-report` | Today's morning report. **Session-gated, not DIAG_TOKEN.** |
| `POST /diag/morning-report/send` | Send the daily morning-report push now (self-throttling). |
| `GET /diag/all` | **Start here for context.** One-shot total system state (see above). |
| `GET /diag/manifest` | **Start here for discovery.** This capability catalog. |
| `POST /diag/action/self-test` | Enqueue the on-bed self-test / thermal-calibration battery. |
| `POST /diag/action/backup` | Immediate consistent SQLite backup into `.run/backups`. |
| `POST /diag/action/run-diagnostics` | Convenience "re-check now" — returns a fresh `/diag/all`. |
| `POST /diag/action/update` | **Highest privilege.** Request a self-update + redeploy. See below. |
| `GET /diag/update-status` | Outcome of the last self-update attempt. |

Session-auth operational endpoints (need a dashboard login, not `$TOKEN` — listed so a fresh
session knows they exist, not because they're reachable with the token alone):
`GET /status`, `GET /tonight`, `GET /tonight/plan`, `GET /efficacy`, `GET /efficacy/config`,
`GET /learning/ledger`, `GET /learning/phases`, `GET /calendar/config`, `GET /calendar/events`,
`POST /calendar/refresh`, `GET /safety/data-quality`, `GET /safety/guardrail`, `GET /circadian`,
`POST /control/{action}`. `GET /diag/manifest` echoes this same list under
`operational_endpoints` with per-entry descriptions.

## What the console can see/do across the whole project

`/diag/all` isn't just an up/down health check — its `project_state` field is a live snapshot
of every subsystem, built by reusing the SAME reads the authenticated dashboard UI calls (no
duplicated logic):

| Subsystem | Where it shows up in `/diag/all` | How you'd act on it remotely |
|---|---|---|
| Thermal control (state, mode, target/bed/room temp, device online/water/priming) | `project_state.status`, `checks` (`device_water`, `device_online`, `priming`, `thermal_response`), `device` | `POST /diag/action/reconnect` for a wedged cloud session, `POST /diag/action/restart?target=daemon` for a dead control loop, or the matching `playbook_matches` fix. |
| Tonight's plan (wake-aware sleep plan, opportunity, thermal strategy) | `project_state.tonight_plan` | Read-only from here; adjust via the dashboard's `/tonight/*` writes (session-gated) if a change is needed. |
| ML / learning (per-learner maturity, learned vs preset, contradictions) | `project_state.learning` | Read-only; a `preset`-heavy ledger usually just means "not enough nights yet," not a bug. |
| Efficacy trial (does the controller help? CONTROLLED-vs-HELD) | `project_state.efficacy` | Read-only status; toggling the trial is a session-gated `/efficacy/config` write. |
| Calendar / shift-awareness (next work shift, ICS feed state) | `project_state.calendar`, `project_state.shift_plan` | If the ICS feed looks stale, that's usually `playbook_matches` entry `calendar_ics_unreachable` — the fix is re-copying the secret ICS URL, which this console deliberately can't do for you (it's a credential). |
| Safety gates (data-quality trust score, decision guardrail) | `project_state.safety.data_quality`, `project_state.safety.guardrail` | Read-only; a forced HOLD here explains why the bed "isn't doing anything" even though the daemon looks healthy. |
| Calibration (thermal calibration, comfort profile, resting baseline, last self-test) | `project_state.calibration` | `POST /diag/action/self-test` to (re-)run the on-bed calibration battery; poll `project_state.calibration.self_test` (or `GET /control/self-test` from a session) for live progress. |
| Backups | `backups` (count, latest, dir) | `POST /diag/action/backup` to take one immediately. |
| Crash forensics | `blackbox_available` (+ `GET /diag/blackbox` for the raw dump) | Pull the dump when `checks.recent_errors`/`daemon_heartbeat` show a recent crash. |

## The 12 diagnostics features — coverage

`GET /diag/manifest` returns a `coverage` map naming each of the project's 12 diagnostics
features and the endpoint that serves it (verdict/checks, repair, event timeline, backup +
listing, bundle, morning report, live health verdict, blackbox, playbook matches, state
history, connectivity/heartbeats, remote actions). Fetch the manifest rather than trusting this
doc to stay in sync — it's generated from the same module-level list the API serves from.

## Ship a code fix end-to-end

This is the highest-privilege capability this console has — it can update and redeploy the
running code with no human at the keyboard. Use it deliberately.

1. Make the fix locally, commit it, and **push it to the deploy branch** (whatever
   `DEPLOY_BRANCH` is set to on the box — default `main`) on the `origin` remote this
   deployment's checkout already points at. This console cannot push code for you; it can only
   trigger the box to pull what's already on that branch.
2. **`POST /diag/action/update?token=$TOKEN`** — writes `.run/update.request` = the box's
   `DEPLOY_BRANCH` value. Returns `{ok, action:"update", result:{branch}, verify_with:
   "/diag/update-status?token=..."}`.
3. **Poll `GET /diag/update-status?token=$TOKEN`** every ~15–30s until `available: true` with a
   `git_ok`/`validate_verdict`/`restarted` verdict. The record is `{timestamp, branch, git_ok,
   git_output (tail), validate_verdict, restarted, summary}`.
4. **Confirm via `GET /diag/all?token=$TOKEN`** — check `version.sha` moved to the new commit,
   `verdict` is still `HEALTHY`/`DEGRADED` (not newly `DOWN`), and the specific thing you fixed
   now checks out (e.g. a `checks` entry flips to `ok`, or a `playbook_matches` entry
   disappears).

If step 3 shows `git_ok: false` or `validate_verdict: "FAIL"`, the watchdog deliberately did
**not** restart anything — the previously-running code is still live and untouched. Read
`git_output` for why the fetch/reset failed (bad branch, network issue, diverged history) and
try again after fixing the underlying problem.

## Self-update threat model

`POST /diag/action/update` is the highest-privilege endpoint in this console, so it's built
narrowly on purpose:

- **Gated identically to every other `/diag*` endpoint** — the same constant-time `DIAG_TOKEN`
  check, 404 on missing/wrong token.
- **The branch is not a request parameter.** It's read server-side from this process's own
  `DEPLOY_BRANCH` environment variable (default `main`). A token holder can request *that* an
  update run, but cannot choose an arbitrary branch, remote, or URL through the HTTP layer —
  there is no `branch=` query param this endpoint reads.
- **That branch value is still checked against a strict allowlist regex**
  (`^[A-Za-z0-9._/-]+$`) before it's ever written to disk, and the watchdog re-checks the SAME
  regex on the file it reads — defense in depth, since the file is treated as untrusted input on
  the watchdog side even though it's trusted-by-construction on the API side.
- **The API endpoint never executes git, never shells out, never touches a process.** It writes
  exactly one flag file (`.run/update.request`). All the privileged work — `git fetch`,
  `git reset --hard`, running `validate_env.ps1`, and triggering a restart — happens in
  `scripts/windows-watchdog.ps1`'s `Handle-UpdateRequest`, a process that already runs elevated
  on the box and already has full control over its own services.
- **`git reset --hard` only ever targets `origin/<branch>` of THIS repo's existing checkout** —
  never a different remote, a different repo, or an arbitrary path/command. It is a
  fetch+hard-reset (fast, deterministic), never a merge — no merge-conflict state can be left
  behind.
- **A failed update never restarts anything.** If `git fetch`/`reset` fails, or
  `validate_env.ps1` reports `FAIL`, the watchdog logs `CRITICAL`, writes `.run/update.result`
  and `.run/watchdog.alert`, and leaves the currently-running processes exactly as they were —
  it will not restart into code it can't confirm is sane.
- **Every self-update request is logged** to the structured event log
  (`category="remote_action"`, `code="update_request"`) with the branch name, for audit.

### `.run/update.request` / `.run/update.result` contract

- `POST /diag/action/update` writes `.run/update.request` — its entire contents is just the
  branch name (e.g. `main`), no JSON, no trailing structure. The watchdog deletes this file
  immediately upon reading it, before doing anything else, so a stuck flag can never loop.
- `scripts/windows-watchdog.ps1`'s `Handle-UpdateRequest` (called once per ~15s supervise
  iteration, same place `Handle-RestartRequest` is called) is the only thing that ever acts on
  it, and it writes `.run/update.result` as a small JSON record:
  ```json
  {
    "timestamp": "2026-07-02T05:00:00+00:00",
    "branch": "main",
    "git_ok": true,
    "git_output": "<tail of the fetch+reset output>",
    "validate_verdict": "PASS",
    "restarted": true,
    "summary": "update to 'main' succeeded (validate=PASS) -- restart requested"
  }
  ```
- `GET /diag/update-status` just reads and returns that file (`{"available": false, ...}` if it
  doesn't exist yet — e.g. before the first update has ever been requested).

## Notes on secrecy

- Never write a real `DIAG_TOKEN` value into a document, a commit, a log message, or this file.
  It only ever belongs in `deploy/.env` on the box and in whatever secret store the operator
  used to hand it to you for a session.
- `config_redacted` (in `/diag/all` and `/diag/bundle`) is generated by
  `dashboard/api/app/diag_bundle.py`'s key-name-based redaction — any env var whose name
  contains `PASSWORD`, `SECRET`, `TOKEN`, `ICS_URL`, `CLIENT_SECRET`, or `JWT` is always shown
  as `<redacted>`, never its real value, no matter which endpoint you fetch it through.
