# sleepctl Dashboard — Design Document

iPhone-first web control interface for the `sleepctl` sleep-optimization system.

---

## 1. Executive Summary

`sleepctl` is a closed-loop bed-temperature controller for the Eight Sleep Pod 2 built for one user: a late-night anesthesiology trainee who runs hot, wakes in the night, needs silence, and wants data. The dashboard is its single control surface — a self-hosted progressive web app (PWA) designed to be added to the iPhone Home Screen and to answer, within one second of opening, every question that matters:

- Is the controller running right now?
- What did it do last night and how did that go?
- What is it planning tonight, and should I override anything?
- Is tonight a short-sleep night?
- What does the model recommend, and does it believe itself?
- Is my sleep getting better over time?

The stack is **FastAPI** (backend) + **Next.js App Router / TypeScript / Tailwind / Recharts** (PWA frontend), both reading the shared **SQLite** database the engine already writes. Auth is single-user **HS256 JWT + PBKDF2**, implemented using Python's stdlib only (no `jose`, no `passlib`, no `cryptography` package — those are broken on this host). The entire system — API, control daemon, web frontend, and Caddy reverse proxy — runs under **Docker Compose** with no external accounts or API keys.

The defining architectural constraint: **the control daemon owns the device; the API and dashboard never call pyEight directly.** The dashboard enqueues commands into a `commands` table; the daemon applies them on its next tick and writes a `runtime_state` snapshot the API streams back via SSE. A total UI or API crash cannot interrupt the closed loop, and Emergency Stop always works because it is just a database write.

---

## 2. System Architecture

```
iPhone (Safari PWA)
      │  HTTPS (Caddy local CA)
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Docker Compose network                                              │
│                                                                     │
│  ┌──────────────┐   ┌──────────────────────────────────────────┐   │
│  │  web          │   │  api (FastAPI / uvicorn)                 │   │
│  │  Next.js PWA  │──▶│  /status, /stream/status (SSE)          │   │
│  │  :3000        │   │  /tonight, /control/*, /nights/*        │   │
│  └──────────────┘   │  /ml/*, /analytics/*, /settings          │   │
│                      │  /admin/*, /alerts/*                     │   │
│  ┌──────────────┐   │                                          │   │
│  │  Caddy        │   │  Reads:  runtime_state, nightly_        │   │
│  │  :443 / :80   │   │          summaries, actions, baselines  │   │
│  │  local CA TLS │   │  Writes: commands (enqueue only)        │   │
│  └──────────────┘   └──────────────────┬─────────────────────┘   │
│                                          │  shared SQLite (WAL)   │
│  ┌───────────────────────────────────────▼──────────────────────┐  │
│  │  daemon (DashboardDaemon)                                     │  │
│  │  Reads:  commands (pending)                                   │  │
│  │  Writes: runtime_state (singleton, every tick)               │  │
│  │  Calls:  ControlCycle.decide() → actuator.set_level()        │  │
│  │  Never:  touched by the API                                   │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

**Key contracts:**

1. The `runtime_state` table holds exactly one row (`id = 1`). The daemon upserts it every tick (default: 5 s). The API reads it; the `stale` flag is set true if `updated` is more than 180 s ago. When stale, `daemon_alive` is forced false and the dashboard shows a red STALE banner.

2. The `commands` table is an append-only queue. Valid command types: `start`, `pause`, `resume`, `stop`, `safe_default`, `set_mode`, `set_temp`, `set_wake`. The API enqueues; the daemon drains with `next_pending_command()` / `mark_applied()`. A pending command that has not been applied yet is visible in the UI as "queued."

3. Manual temperature overrides are logged as `ActionRecord(source="manual")` in the `actions` table immediately on the API side, so the ML's revealed-preference learner (`ml/preference.py`) picks them up before the next nightly update.

4. `--dry-run` mode (daemon flag) skips `actuator.set_level()` calls, so the whole stack can run safely with the simulator without touching any device.

---

## 3. Frontend Structure

**Tech stack:** Next.js 14 App Router, TypeScript, Tailwind CSS, Recharts for charts, native `EventSource` for SSE.

**PWA features:** `manifest.json` (standalone display, dark theme color `#0f0f0f`, 192 × 192 + 512 × 512 icons), service worker for offline shell caching, `apple-mobile-web-app-capable` meta tags.

### Pages and routes

| Route | Page | Purpose |
|---|---|---|
| `/login` | Login | Username + password; sets `session` cookie |
| `/` | Home / Status | Live status card, last-night brief, tonight plan, active alerts |
| `/tonight` | Tonight | Full control: mode, temp override, wake time, Emergency Stop |
| `/data` | Data | Night-by-night history table + sample hypnogram for selected night |
| `/learning` | Learning | ML state: setpoint, confidence meter, action history, phenotype |
| `/analytics` | Analytics | Recharts trend lines: wake events, deep %, HRV, efficiency, outcome score |
| `/settings` | Settings | Tunable knobs (neutral temp, deep bias, wake window, vibration power) |
| `/admin` | Admin | Data-source health, daemon liveness, pending command count, raw decision log |

**Navigation:** Fixed bottom tab bar with five primary tabs (Home, Tonight, Data, Learning, Settings). Admin is accessible from Settings. All touch targets are 48 × 48 px minimum. Font sizes: labels 14 sp, primary values 24–36 sp.

**Theme:** Dark-only. Background `#0f0f0f`, surface `#1a1a1a`, accent `#3b82f6` (blue). State colors: green (`#22c55e`) = running/good, amber (`#f59e0b`) = warning/manual, red (`#ef4444`) = stale/alert, gray (`#6b7280`) = idle.

**SSE integration:** The Home and Tonight pages open an `EventSource` to `/stream/status?token=<jwt>`. On each event (every 5 s) the status card re-renders in place with no page reload. If the stream drops, the UI shows a reconnecting indicator and retries with exponential backoff.

---

## 4. Backend Structure

The API is a single FastAPI application (`dashboard/api/app/main.py`) that imports the `sleepctl` Python engine directly. All sleep-data reads go through `sleepctl.storage.repository.Repository`; all device interactions go through `app.bridge`.

```
dashboard/api/app/
├── __init__.py
├── main.py        # FastAPI app, all routes
├── bridge.py      # API↔daemon contract: enqueue_command, read_runtime_state
├── db.py          # SQLite connection: engine schema + dashboard DDL, get_repo()
├── security.py    # PBKDF2 hashing, HS256 JWT (stdlib only), AuthDep
├── services.py    # build_status, ml_overview, ml_recommendation, trends,
│                  # effectiveness, generate_alerts, data_health
├── config.py      # Settings dataclass (env-driven, auto-generated JWT secret)
└── seed.py        # bootstrap user creation helper
```

`get_repo()` returns a `Repository` over the shared database after ensuring the dashboard DDL has been applied. Every route handler receives a fresh connection via `repo_dep()` and closes it on exit — SQLite WAL mode allows the daemon to write concurrently without blocking reads.

The daemon (`dashboard/daemon/run_daemon.py`) is a separate Python process (`DashboardDaemon`) that imports `sleepctl` directly. It runs a tight loop: apply pending commands → run one `ControlCycle.decide()` tick → write `runtime_state`. Exceptions in a tick are caught and printed; the loop continues, so a bug in the ML path cannot stop the daemon.

---

## 5. Data Model

The dashboard shares the engine's SQLite file (`sleepctl.db`). The engine creates eight tables; the dashboard adds nine more. Both sets of DDL are idempotent (`CREATE TABLE IF NOT EXISTS`) and are applied on every connection open.

### Engine tables (read by the API, written by the daemon/engine)

| Table | Role | Key columns |
|---|---|---|
| `raw_samples` | Per-minute sensor data | `ts`, `night_date`, `stage`, `heart_rate`, `hrv`, `bed_temp_f`, `room_temp_f`, `commanded_level`, `wake_event` |
| `nightly_summaries` | End-of-night rollup | `date` PK, `total_sleep_min`, `deep_min`, `rem_min`, `wake_events`, `sleep_efficiency`, `avg_hrv`, `outcome_score`, `setpoint_version` |
| `context` | Schedule / daytime antecedents | `date` PK, `required_wake_time`, `is_short_sleep_day`, `sleep_opportunity_min`, `outdoor_temp_f`, `late_night_work`, behavioral flags |
| `interventions` | What the controller changed | `ts`, `night_date`, `action`, `magnitude_f`, `reason`, `held`, `reverted`, `outcome_delta` |
| `decisions` | Per-tick controller output | `ts`, `state`, `objective`, `thermal_intent`, `target_temp_f`, `confidence`, `reason` |
| `baselines` | Rolling 7/14-day stats | `ts`, `metrics` (JSON: `hrv_7d_median`, `wake_events_7d_median`, etc.) |
| `setpoints` | Versioned SetpointProfile | `version` PK, `ts`, `source` (`default`/`policy`/`ml`), `profile` (JSON) |
| `actions` | Per-night ML action ledger | `night_date`, `action_name`, `params`, `confidence`, `reward_observed`, `source` (`policy`/`ml`/`manual`) |

### Dashboard-only tables (DDL in `db.py`)

```sql
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,    -- pbkdf2_sha256$200000$<salt_hex>$<hash_hex>
    role             TEXT DEFAULT 'owner',
    created          TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,            -- ISO date (night date)
    text     TEXT,
    created  TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    type           TEXT,               -- wake_events | low_hrv | short_sleep | stale_data | low_confidence
    severity       TEXT,               -- critical | warning | info
    message        TEXT,
    acknowledged   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key    TEXT PRIMARY KEY,
    value  TEXT                        -- JSON-encoded value
);

CREATE TABLE IF NOT EXISTS settings_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    key        TEXT,
    old_value  TEXT,
    new_value  TEXT
);

-- Singleton snapshot written by daemon every tick; read by API/SSE.
CREATE TABLE IF NOT EXISTS runtime_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    updated       TEXT,
    state         TEXT,                -- IDLE | INDUCTION | MAINTENANCE | WAKE_RECOVERY | WAKE_WINDOW
    objective     TEXT,                -- optimize | damage_control
    mode          TEXT,                -- auto | manual | view | paused
    target_temp_f REAL,
    bed_temp_f    REAL,
    room_temp_f   REAL,
    stage         TEXT,                -- awake | light | deep | rem | unknown
    confidence    REAL,
    target_level  INTEGER,             -- -100..100 device level
    daemon_alive  INTEGER,
    extra         TEXT                 -- JSON (manual_target_f, etc.)
);

-- API-to-daemon command queue.
CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    type        TEXT,                  -- start | pause | resume | stop | safe_default | set_mode | set_temp | set_wake
    payload     TEXT,                  -- JSON
    status      TEXT DEFAULT 'pending', -- pending | applied
    applied_ts  TEXT
);

CREATE TABLE IF NOT EXISTS data_sync (
    source     TEXT PRIMARY KEY,       -- eightsleep_cloud | calendar | weather
    last_sync  TEXT,
    status     TEXT,
    message    TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint  TEXT UNIQUE,
    p256dh    TEXT,
    auth      TEXT,
    created   TEXT
);

CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date);
CREATE INDEX IF NOT EXISTS idx_alerts_ack ON alerts(acknowledged);
```

---

## 6. API Design

Base path: `https://<host>/api` (Caddy proxies `/api` to the FastAPI container on port 8000). All endpoints except `/health` and `/auth/login` require a valid JWT, supplied as a `session` HttpOnly cookie or an `Authorization: Bearer <token>` header. The SSE endpoint also accepts `?token=<jwt>` because `EventSource` cannot set headers.

### Auth

| Method | Path | Body / Params | Response |
|---|---|---|---|
| POST | `/auth/login` | `{username, password}` | `{token, user}` + `session` cookie (30-day TTL) |
| POST | `/auth/logout` | — | Clears cookie |
| GET | `/auth/me` | — | `{user}` |

### Status + SSE

| Method | Path | Notes |
|---|---|---|
| GET | `/status` | Full status snapshot (see example JSON below) |
| GET | `/stream/status` | SSE; emits the same payload every 5 s; `?token=<jwt>` auth |

### Tonight / control

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/tonight` | — | mode, state, target_temp_f, schedule brief, ML recommendation, setpoint |
| POST | `/control/start` | — | Enqueue `start` command |
| POST | `/control/pause` | — | Enqueue `pause` command |
| POST | `/control/resume` | — | Enqueue `resume` command |
| POST | `/control/stop` | — | Enqueue `stop` command (Emergency Stop) |
| POST | `/control/safe-default` | — | Reset setpoint to defaults, resume auto |
| POST | `/tonight/temp` | `{target_f: float}` | Enqueue `set_temp`; logs `ActionRecord(source="manual")` |
| POST | `/tonight/mode` | `{mode: "auto"\|"manual"\|"view"}` | Enqueue `set_mode` |
| POST | `/tonight/wake` | `{wake_time: "HH:MM", window_min?: int}` | Enqueue `set_wake` |

### Data

| Method | Path | Notes |
|---|---|---|
| GET | `/nights?limit=30` | Array of nightly summary briefs |
| GET | `/nights/{date}` | Full summary for one night + context |
| GET | `/nights/{date}/samples` | Array of raw samples (ts, stage, hr, hrv, bed_temp_f, room_temp_f) |
| GET | `/interventions?limit=50` | Recent interventions with reason + magnitude |
| GET | `/notes?date=YYYY-MM-DD` | Fetch notes (all or for a night) |
| POST | `/notes` | `{date, text}` — add a note |

### ML / learning

| Method | Path | Notes |
|---|---|---|
| GET | `/ml/overview` | setpoint, baselines, model_confidence, clean_nights, recent actions, phenotype |
| GET | `/ml/recommendation` | action, reason, confidence, predicted, low_confidence flag |

### Analytics

| Method | Path | Notes |
|---|---|---|
| GET | `/analytics/trends?metric=wake_events&window=30` | `{metric, points: [{date, value}]}` |
| GET | `/analytics/effectiveness` | Mean reward by action name |

### Settings

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/settings` | — | `{stored: {key: value}, defaults: {...}}` |
| PUT | `/settings` | `{values: {key: value}}` | Upserts key-value pairs; logs changes |

### Admin / alerts

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/health` | Daemon liveness, data-source sync status, pending command count |
| GET | `/admin/logs?limit=50` | Raw `decisions` table dump (most recent) |
| GET | `/alerts` | Active (unacknowledged) alerts; also runs `generate_alerts()` |
| POST | `/alerts/{id}/ack` | Acknowledge an alert |

### Example JSON — `/status`

```json
{
  "state": "MAINTENANCE",
  "objective": "optimize",
  "mode": "auto",
  "target_temp_f": 66.5,
  "bed_temp_f": 67.2,
  "room_temp_f": 71.0,
  "stage": "deep",
  "confidence": 0.82,
  "daemon_alive": true,
  "stale": false,
  "updated": "2026-06-23T03:17:45+00:00",
  "recommendation": {
    "action": "slight_cool",
    "reason": "wake_events above 7-day median; cooling deep-sleep phase should reduce fragmentation",
    "confidence": 0.61
  },
  "last_night": {
    "date": "2026-06-22",
    "total_sleep_min": 362,
    "deep_min": 61,
    "rem_min": 78,
    "wake_events": 3,
    "sleep_efficiency": 0.83,
    "avg_hrv": 58.4,
    "outcome_score": 0.54
  },
  "alerts": [
    {
      "id": 12,
      "ts": "2026-06-23T08:05:00+00:00",
      "type": "wake_events",
      "severity": "warning",
      "message": "3 wake events last night (target ≤2).",
      "acknowledged": 0
    }
  ],
  "schedule": {
    "required_wake_time": "06:30:00",
    "sleep_opportunity_min": 390,
    "is_short_sleep_day": false
  }
}
```

### Example JSON — an intervention

```json
{
  "ts": "2026-06-23T02:44:10+00:00",
  "state": "wake_recovery",
  "action": "cooler",
  "magnitude_f": -1.5,
  "reason": "wake detected (3 signals: movement spike, HR rise, LIGHT stage return); stabilizing with slight cool"
}
```

### Example JSON — a nightly summary

```json
{
  "date": "2026-06-22",
  "total_sleep_min": 362,
  "sleep_onset_latency_min": 14,
  "deep_min": 61,
  "rem_min": 78,
  "light_min": 223,
  "wake_events": 3,
  "waso_min": 22,
  "sleep_efficiency": 0.83,
  "avg_hr": 56.1,
  "avg_hrv": 58.4,
  "avg_respiratory_rate": 14.2,
  "outcome_score": 0.54,
  "setpoint_version": 7,
  "context": {
    "required_wake_time": "2026-06-23T06:30:00",
    "sleep_opportunity_min": 390,
    "is_short_sleep_day": false,
    "late_night_work": true,
    "outdoor_temp_f": 68.0
  }
}
```

---

## 7. Mobile UX Design

The entire UI is designed for one hand, one thumb, in a dark room at 2 AM.

### Global conventions

- **Dark background only.** No light mode. Background `#0f0f0f`, card surface `#1a1a1a`.
- **Large primary values.** The number you need to read at a glance is 28–36 sp bold. Labels are 12–14 sp gray.
- **Minimum 48 × 48 px tap targets.** Bottom navigation tabs are 56 px tall.
- **Status color at a glance.** A colored 6 px left border or pill badge on every card encodes health: green = good, amber = degraded/manual, red = alert/stale.
- **No unnecessary confirmation dialogs**, except Emergency Stop, which shows a destructive-red sheet with a single large "STOP" button.

### Page wireframes

---

#### `/login`

```
┌───────────────────────────────┐
│                               │
│   sleepctl                    │  (center-aligned, large)
│                               │
│   ┌───────────────────────┐   │
│   │  username             │   │
│   └───────────────────────┘   │
│   ┌───────────────────────┐   │
│   │  password             │   │
│   └───────────────────────┘   │
│                               │
│   ┌───────────────────────┐   │
│   │      Sign in          │   │  (full-width blue button)
│   └───────────────────────┘   │
│                               │
└───────────────────────────────┘
```

Submits to `POST /auth/login`. On success, redirects to `/`. Token stored in the `session` HttpOnly cookie. A 401 shakes the password field.

---

#### `/` — Home / Status

```
┌───────────────────────────────┐
│  ●  RUNNING  ·  MAINTENANCE   │  (green dot, live SSE)
│  Deep sleep · 66.5°F target   │
│  Bed 67.2°F · Room 71.0°F     │
│  Confidence ████████░░ 82%    │
│                      [STALE]  │  (red banner, if stale)
├───────────────────────────────┤
│  LAST NIGHT  Jun 22           │
│  362 min  |  3 wake events ⚠  │
│  Deep 61m  REM 78m  Eff 83%  │
│  HRV 58 ms  Score 0.54       │
├───────────────────────────────┤
│  TONIGHT                      │
│  Wake: 06:30 · 390 min opp.  │
│  Mode: auto  Setpoint v7     │
│  Rec: slight_cool (conf 61%) │
├───────────────────────────────┤
│  ALERTS (1)                   │
│  ⚠ 3 wake events — target ≤2 │
│                       [Ack]   │
├───────────────────────────────┤
│  🏠  ◉  📊  🧠  ⚙          │  (bottom tab bar)
└───────────────────────────────┘
```

The top card re-renders silently on every SSE event. Tapping "STALE" opens a tooltip: "Daemon last reported >3 min ago. Control continues on its last command. Check /admin." The recommendation line links to `/learning`.

---

#### `/tonight` — Tonight Control

```
┌───────────────────────────────┐
│  TONIGHT CONTROL              │
│                               │
│  Mode   [auto] [manual] [view]│  (segmented control)
│                               │
│  Target temp                  │
│  ─────────────────────────── │
│  66.5°F        [-] [+]  [Set]│
│  (manual overrides are logged │
│   as revealed-preference data)│
│                               │
│  Wake time                    │
│  06:30   [Edit]               │
│  Smart-wake window: 30 min   │
│  (heat + vibration, no audio) │
│                               │
│  ┌───────────────────────┐    │
│  │    Emergency Stop     │    │  (red, full-width)
│  └───────────────────────┘    │
│  ┌───────────────────────┐    │
│  │   Return to Safe Default│  │  (amber, full-width)
│  └───────────────────────┘    │
│                               │
│  Current:  MAINTENANCE        │
│  Queued commands: 0           │
│                               │
│  🏠  ◉  📊  🧠  ⚙          │
└───────────────────────────────┘
```

Emergency Stop sends `POST /control/stop`. The button is inside a confirmation bottom sheet (red background, large "STOP" tap target) to prevent accidental taps. "Return to Safe Default" sends `POST /control/safe-default`, which resets the setpoint to defaults and resumes auto mode. Manual temperature changes call `POST /tonight/temp`; the response includes the queued command ID, and a small "Queued" badge appears until the next SSE confirms the daemon applied it.

**Short-sleep night indicator:** when `is_short_sleep_day = true`, a amber banner appears: "Short sleep night — controller in DAMAGE CONTROL mode (faster induction, less experimentation)."

---

#### `/data` — Data

```
┌───────────────────────────────┐
│  DATA  [Last 30 nights]       │
│                               │
│  Jun 22 │ 362 min │ 3 wake ⚠ │
│  Jun 21 │ 431 min │ 1 wake ✓ │
│  Jun 20 │ 398 min │ 2 wake ~  │
│  ...                          │
│                               │
│  ─── Jun 22 detail ───        │
│  Hypnogram (Recharts area):   │
│  ████ deep ▓▓▓ REM ░ light   │
│  with temperature overlay     │
│                               │
│  Interventions:               │
│  02:44  WAKE_RECOVERY -1.5°F  │
│  04:11  MAINTENANCE   hold    │
│                               │
│  Note: [Add note for Jun 22]  │
│                               │
│  🏠  ◉  📊  🧠  ⚙          │
└───────────────────────────────┘
```

Tapping a night row fetches `/nights/{date}` and `/nights/{date}/samples`. The hypnogram is a Recharts stacked area chart (sleep stage on Y, time on X) with a right-axis temperature line. Intervention timestamps are rendered as vertical lines on the chart. Notes are fetched with `GET /notes?date=YYYY-MM-DD` and added with `POST /notes`.

---

#### `/learning` — Learning

```
┌───────────────────────────────┐
│  LEARNING                     │
│                               │
│  Model confidence             │
│  ████████░░░░░░ 38%           │
│  (14 clean nights required;   │
│   currently 11)               │
│                               │
│  LOW CONFIDENCE — using safe  │
│  rule policy                  │  (amber badge)
│                               │
│  Current recommendation       │
│  slight_cool                  │
│  "wake_events above median;   │
│   cooling deep phase"         │
│  Predicted: wake_events -0.8  │
│             deep_min +4.2     │
│                               │
│  Setpoint (v7, source: ml)    │
│  neutral 70°F  deep 66°F      │
│  REM offset +1.5°F            │
│  wake ramp 74°F               │
│  bed weight 0.75              │
│                               │
│  Recent actions               │
│  Jun 21  slight_cool  r=0.71  │
│  Jun 19  no_change    r=0.58  │
│  ...                          │
│                               │
│  Top phenotype correlations   │
│  late_night_work → wake_evts  │
│  outdoor_temp_f  → efficiency │
│                               │
│  🏠  ◉  📊  🧠  ⚙          │
└───────────────────────────────┘
```

Data from `GET /ml/overview`. The confidence meter is a filled progress bar — when below `conf_min` (0.35) it is amber with the "LOW CONFIDENCE" badge. The "Predicted" values show the model's expected per-outcome changes for the recommended action. When the model is deferring to the rule policy, the action reads "rule-policy" and reason reads "deferring to safe rule policy (insufficient data or confidence)." Manual overrides accumulated toward revealed-preference anchoring are noted: "X manual overrides logged — setpoint is being anchored toward your median choice (Y°F)."

---

#### `/analytics` — Analytics

```
┌───────────────────────────────┐
│  ANALYTICS  [metric ▼] [30d]  │
│                               │
│  Wake events (30 nights)      │
│  ┌────────────────────────┐   │
│  │  Recharts line chart   │   │
│  │  trend + benchmark line│   │
│  └────────────────────────┘   │
│  7-day median: 2.1            │
│  Benchmark target: ≤2         │
│  Trend: improving ✓           │
│                               │
│  Intervention effectiveness   │
│  slight_cool   n=8  r̄=0.66   │
│  strong_cool   n=2  r̄=0.41   │
│  no_change     n=5  r̄=0.53   │
│                               │
│  🏠  ◉  📊  🧠  ⚙          │
└───────────────────────────────┘
```

Metric selector cycles: `wake_events`, `deep_min`, `rem_min`, `avg_hrv`, `total_sleep_min`, `sleep_efficiency`, `outcome_score`. Window selector: 14 / 30 / 60 nights. Benchmark reference lines are drawn from `AppConfig.benchmarks` (wake_events_max = 2, deep_min_ideal = 108, hrv_target_ms = 70, etc.). The effectiveness table renders `GET /analytics/effectiveness`.

---

#### `/settings` — Settings

```
┌───────────────────────────────┐
│  SETTINGS                     │
│                               │
│  Thermal targets              │
│  Neutral temp      70.0°F  ▶  │
│  Deep-sleep bias   66.0°F  ▶  │
│  Wake ramp         74.0°F  ▶  │
│  Max step          2.0°F   ▶  │
│                               │
│  Smart wake                   │
│  Wake window       30 min  ▶  │
│  Vibration power   50      ▶  │
│  (audio: always OFF)          │
│                               │
│  Benchmarks                   │
│  HRV target        70 ms   ▶  │
│  Max wake events   2       ▶  │
│                               │
│  [Save]                       │
│                               │
│  → Admin panel                │
│                               │
│  🏠  ◉  📊  🧠  ⚙          │
└───────────────────────────────┘
```

Settings are read from `GET /settings` (stored values overlaid on engine defaults). Edits call `PUT /settings`. Changes are logged in `settings_changes` for auditability. The vibration power slider (0–100) is labelled "0 = off; 50 = gentle (default); audio is always disabled." Thermal targets are validated client-side against the 55–110 °F device range before submission.

---

#### `/admin` — Admin

```
┌───────────────────────────────┐
│  ADMIN                        │
│                               │
│  Daemon         ● ALIVE       │
│  Last tick:  3 s ago          │
│  Pending commands: 0          │
│                               │
│  Data sources                 │
│  eightsleep_cloud  ✓ 2 min ago│
│  calendar          ✓ 5 min ago│
│  weather           ✓ 1 hr ago │
│                               │
│  Recent decisions (50)        │
│  03:17  MAINT deep  66.5°F   │
│         conf 0.82  hold      │
│  03:12  MAINT deep  66.5°F   │
│         conf 0.79  cooler    │
│  ...                          │
│                               │
│  🏠  ◉  📊  🧠  ⚙          │
└───────────────────────────────┘
```

Data from `GET /admin/health` (daemon liveness + data sources) and `GET /admin/logs` (raw decisions). This page is the first stop when something looks wrong.

---

## 8. ML Integration

The dashboard surfaces the action-value recommender (`sleepctl/ml/recommend.py`) as a first-class feature, not a debug panel.

### What the recommender does

Each nightly update (`loop/nightly.py`) runs the ML pipeline:

1. `build_feature_rows(repo)` — joins 3 dataset layers + setpoints into one `FeatureRow` per night.
2. `clean_rows(rows)` — drops nights flagged as confounded (illness, travel, alcohol, excessive manual overrides — anything that would corrupt reward attribution).
3. Gate: if fewer than 14 clean nights, return `None` → fall back to the rule-based tiered policy.
4. `SetpointModel.fit(clean)` — fits a per-outcome ridge regression (standardized, y-centered) for each of: `wake_events`, `deep_pct`, `rem_pct`, `avg_hrv`, `sleep_efficiency`, `outcome_score`. Confidence is derived from residual spread and data support.
5. `score_actions(model, current, ctx, cfg)` — evaluates each candidate action (`no_change`, `slight_cool`, `slight_warm`, `rem_warm_more`, `skin_more`, `skin_less`, `strong_cool`) by predicting the reward under the resulting setpoint given the recent context vector.
6. `select_action(scores, cfg)` — picks the smallest-magnitude action whose predicted improvement clears an uncertainty-aware margin (`base_margin / confidence`). Low-confidence → `no_change`.

### Dashboard surfaces

- **Confidence meter** (`/learning`): filled progress bar 0–100%. Below `conf_min` (0.35) → amber badge "LOW CONFIDENCE — using safe rule policy." The number of clean nights vs. the 14-night gate is shown ("11 / 14 nights").
- **Chosen action card**: action name, plain-English reason, confidence percentage, predicted per-outcome changes (e.g. "wake_events −0.8, deep_min +4.2"). If action is `no_change` and confidence is adequate, displays "Model satisfied — no change recommended."
- **Fallback badge**: when `source == "fallback"` or `low_confidence == true`, a gray pill reads "RULE POLICY." When the model is active, a blue pill reads "ML."
- **Action history table**: last 10 `ActionRecord` rows — date, action, source, confidence, observed reward. Lets the user verify the loop is closing.
- **Phenotype correlations**: top 6 `(feature, r, n)` pairs from `correlate_with_outcome` — shows what the model has learned, e.g. "late_night_work correlates with wake_events (r = −0.41, n = 23)."

### Manual overrides as revealed preference

When the user taps `[Set]` in Tonight with a manual temperature, the API:
1. Logs `ActionRecord(source="manual", params={"target_f": X})` immediately.
2. Enqueues `set_temp` for the daemon.

`ml/preference.py` checks the last 60 `actions` for `source="manual"` entries. Once there are at least 3, it computes the median manual target and nudges `neutral_f` and `deep_bias_f` toward it at `manual_preference_gain` (0.5 × gap, capped at `max_step_f` per update). The dashboard shows: "8 manual overrides — setpoint anchored toward 68°F (your revealed preference)." Nights with excessive manual overrides are excluded from automated reward attribution in `clean_rows`, so constant tweaking informs the setpoint without corrupting the reward signal.

### Short-sleep / damage-control interaction

When `context.is_short_sleep_day = true`, the controller switches to `NightObjective.DAMAGE_CONTROL`. In this mode, cool thermal intents are nudged toward neutral (less experimentation), induction is shortened, and the ML does not apply its recommendation (the night is flagged as confounded — a bad recovery night caused by schedule, not setpoint). The dashboard shows the amber "DAMAGE CONTROL" banner and suppresses the ML recommendation card.

---

## 9. Security and Auth

### Authentication model

Single-user, single-tenant. No signup flow, no OAuth, no magic links. One set of credentials is bootstrapped from environment variables on first run; the `users` table holds the PBKDF2 hash; the JWT authorizes all subsequent API calls.

### Password hashing

PBKDF2-SHA256, 200,000 iterations, 16-byte random salt, implemented in `security.py` using `hashlib.pbkdf2_hmac` and `os.urandom`. Stored format: `pbkdf2_sha256$200000$<salt_hex>$<hash_hex>`. Comparison uses `hmac.compare_digest` to prevent timing attacks.

### JWT implementation

HS256, stdlib-only. `create_token` builds a standard `{alg, typ}` header + `{sub, exp}` payload, base64url-encodes both, computes `HMAC-SHA256(secret, header.payload)`, and returns `header.payload.sig`. `decode_token` reverses this and checks expiry. No third-party library is involved (`jose` and `passlib` are explicitly excluded because the host's `cryptography` package is broken).

JWT secret: auto-generated with `secrets.token_hex(32)` if `JWT_SECRET` env var is not set. The deploy entrypoint generates and persists a secret in the Compose environment so it survives container restarts.

JWT TTL: 720 hours (30 days), configurable via `JWT_TTL_HOURS`. This phone-friendly default avoids the need to re-login after every sleep.

### Cookie vs. header

`POST /auth/login` sets a `session` HttpOnly, `SameSite=Lax` cookie. The API also accepts `Authorization: Bearer <token>`. The SSE endpoint (`/stream/status`) accepts `?token=<jwt>` because `EventSource` cannot send headers. Caddy's HTTPS ensures the cookie is always transmitted over TLS.

### Network exposure

The API is not exposed to the internet. Caddy runs on the local network with a self-generated root CA (managed by Caddy's built-in `tls internal`). The iPhone trusts this CA after a one-time profile install. Safari on LAN delivers the PWA with the HTTPS required for service workers. No external services, no cloud relay.

### What security does NOT cover

This is a single-user home system. There is no RBAC beyond the `role` column (currently always `owner`), no rate limiting, no audit trail beyond `settings_changes`, and no MFA. These are acceptable trade-offs for a private LAN-only deployment.

---

## 10. Pseudocode / File Structure

### Repository layout

```
SleepController/
├── sleepctl/                    # Python engine (installed as a package)
│   ├── adapters/                # PodSensorSource implementations
│   │   ├── eightsleep_cloud.py  # Tier 0: pyEight cloud intervals
│   │   ├── raw_capture.py       # Tier 1: redirected raw stream
│   │   ├── local_frank.py       # Tier 2: gated on-device stub
│   │   ├── simulator.py         # Offline deterministic simulator
│   │   ├── calendar.py          # Google Calendar → ContextRecord
│   │   └── weather.py           # OpenMeteo ambient fallback
│   ├── controller/
│   │   ├── controller.py        # SleepController.decide()
│   │   ├── state_machine.py     # 6-state FSM
│   │   ├── induction.py         # InductionRoutine
│   │   ├── maintenance.py       # MaintenanceRoutine
│   │   ├── smart_wake.py        # SmartWakeRoutine + WakeAlarmSpec
│   │   ├── thermal.py           # ThermalController (intent → °F → level)
│   │   ├── wake_detection.py    # WakeDetector (multi-signal voting)
│   │   └── calibration.py       # RAW_TO_FAHRENHEIT_MAP, to_level()
│   ├── learning/
│   │   ├── baselines.py         # BaselineEngine (7/14-day median+MAD)
│   │   ├── policy.py            # TieredPolicy (try/hold/escalate/revert)
│   │   ├── response.py          # ResponseEstimator (paired-night comparison)
│   │   └── setpoints.py         # apply_recommendation → new SetpointProfile
│   ├── loop/
│   │   ├── cycle.py             # ControlCycle (one tick: decide + log)
│   │   ├── live.py              # Live adapter management
│   │   ├── nightly.py           # NightlyUpdater (Learn phase + ML)
│   │   └── runtime.py           # Runtime (tick/replay)
│   ├── ml/
│   │   ├── actions.py           # ACTIONS, ActionScore, apply_action
│   │   ├── confounders.py       # clean_rows (drop confounded nights)
│   │   ├── dataset.py           # build_feature_rows → FeatureRow
│   │   ├── features.py          # Rolling engineered features
│   │   ├── linalg.py            # Ridge regression (pure Python)
│   │   ├── model.py             # SetpointModel.fit() + confidence()
│   │   ├── objective.py         # Outcome weighting
│   │   ├── phenotype.py         # correlate_with_outcome
│   │   ├── preference.py        # revealed_preference (manual override anchor)
│   │   ├── recommend.py         # recommend_action pipeline
│   │   ├── reward.py            # multi-objective outcome_score
│   │   └── select.py            # score_actions, select_action
│   ├── storage/
│   │   ├── schema.py            # Engine DDL (_DDL), connect(), init_db()
│   │   └── repository.py        # Repository (all read/write methods)
│   ├── models.py                # Dataclasses + enums (shared contract)
│   ├── config.py                # AppConfig, UserProfile, Benchmarks, Tunables, MLConfig
│   └── cli.py                   # CLI entry point
│
├── dashboard/
│   ├── api/
│   │   ├── app/
│   │   │   ├── main.py          # FastAPI app, all routes
│   │   │   ├── bridge.py        # enqueue_command, read_runtime_state
│   │   │   ├── db.py            # Dashboard DDL, get_repo()
│   │   │   ├── security.py      # PBKDF2, HS256 JWT, AuthDep
│   │   │   ├── services.py      # build_status, ml_overview, trends, alerts
│   │   │   └── config.py        # Settings (env-driven)
│   │   ├── tests/
│   │   │   ├── conftest.py
│   │   │   └── test_api.py
│   │   └── requirements.txt     # fastapi, uvicorn, PyYAML
│   ├── daemon/
│   │   └── run_daemon.py        # DashboardDaemon (owns device, drains commands)
│   └── web/                     # Next.js PWA (to be created)
│       ├── app/
│       │   ├── layout.tsx        # Root layout, bottom tab nav, SSE provider
│       │   ├── page.tsx          # Home / Status
│       │   ├── tonight/page.tsx
│       │   ├── data/page.tsx
│       │   ├── learning/page.tsx
│       │   ├── analytics/page.tsx
│       │   ├── settings/page.tsx
│       │   ├── admin/page.tsx
│       │   └── login/page.tsx
│       ├── components/
│       │   ├── StatusCard.tsx     # Live status tile (SSE-driven)
│       │   ├── ConfidenceMeter.tsx
│       │   ├── TempControl.tsx    # Slider + set button
│       │   ├── EmergencyStop.tsx  # Confirmation sheet
│       │   ├── NightRow.tsx
│       │   ├── Hypnogram.tsx      # Recharts area + temp overlay
│       │   ├── TrendChart.tsx     # Recharts line + benchmark line
│       │   ├── AlertBanner.tsx
│       │   └── StaleBanner.tsx
│       ├── lib/
│       │   ├── api.ts             # Typed fetch wrappers for all endpoints
│       │   └── sse.ts             # SSE hook with reconnect
│       ├── public/
│       │   ├── manifest.json
│       │   ├── sw.js              # Service worker (offline shell)
│       │   └── icons/             # 192, 512 px
│       ├── next.config.js
│       ├── tailwind.config.js
│       └── package.json
│
├── deploy/
│   ├── docker-compose.yml        # api + daemon + web + caddy
│   ├── Caddyfile                 # TLS internal, reverse proxy
│   ├── Dockerfile.api
│   ├── Dockerfile.daemon
│   ├── Dockerfile.web
│   └── generate-secrets.sh       # Writes .env with JWT_SECRET, DASHBOARD_PASSWORD
│
├── docs/
│   ├── DESIGN.md                 # Engine design (this system's DESIGN.md)
│   └── DASHBOARD.md              # This file
│
├── tests/                        # Engine unit tests
├── sleepctl.db                   # Shared SQLite (WAL mode)
└── pyproject.toml
```

### Key pseudocode — SSE client hook (`lib/sse.ts`)

```typescript
function useStatusStream() {
  const [status, setStatus] = useState<Status | null>(null);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    let es: EventSource;
    let stalTimer: ReturnType<typeof setTimeout>;

    function connect() {
      es = new EventSource(`/api/stream/status?token=${getToken()}`);
      es.onmessage = (e) => {
        clearTimeout(stalTimer);
        const data = JSON.parse(e.data) as Status;
        setStatus(data);
        setStale(data.stale);
        // If daemon marks stale itself, propagate immediately
        stalTimer = setTimeout(() => setStale(true), 15_000);
      };
      es.onerror = () => {
        es.close();
        setTimeout(connect, 5_000);  // exponential backoff omitted for brevity
      };
    }

    connect();
    return () => { es.close(); clearTimeout(stalTimer); };
  }, []);

  return { status, stale };
}
```

### Key pseudocode — Emergency Stop flow

```
User taps [Emergency Stop]
  → Confirmation bottom sheet opens (red background)
User taps [STOP]
  → POST /control/stop
  → API: bridge.enqueue_command(conn, "stop") → inserts commands row
  → Returns: {queued: "stop", command_id: 42}
  → UI shows "Queued — waiting for daemon"
Daemon next tick (≤5 s):
  → bridge.next_pending_command() → {type: "stop", ...}
  → self.paused = True; actuator.set_level(0)
  → bridge.mark_applied(conn, 42)
  → bridge.write_runtime_state(conn, {state: "IDLE", mode: "paused", ...})
SSE event arrives:
  → status.state = "IDLE", status.mode = "paused"
  → UI: green dot → gray, status card shows "PAUSED"
```

### Key pseudocode — manual temp override ML path

```
User sets 68°F via tonight/temp
  → POST /tonight/temp {target_f: 68.0}
  → API logs ActionRecord(source="manual", params={"target_f": 68.0})
  → API enqueues set_temp command
Nightly update (loop/nightly.py):
  → preference.py collects recent_actions(60) where source=="manual"
  → targets = [68.0, 67.5, 68.0, 69.0, 68.5]  (5 entries, ≥ min_count)
  → pref = median(targets) = 68.0
  → delta = gain * (pref - current_neutral)
           = 0.5 * (68.0 - 70.0) = -1.0
  → new_neutral = clamp(70.0 - 1.0, 62, 78) = 69.0
  → new SetpointProfile(neutral_f=69.0, source="manual_pref", version=8)
  → confounders.py: this night had 3 manual overrides → flagged as confounded
    (excluded from automated reward attribution)
```

---

## 11. Deployment Plan

### Minimal MVP (can be done today)

- [x] FastAPI backend (`main.py`, `bridge.py`, `db.py`, `security.py`, `services.py`)
- [x] Simulator daemon (`run_daemon.py` with `--simulate`)
- [ ] Next.js PWA scaffolded with the 8 pages above
- [ ] `StatusCard` + SSE hook wired to `/stream/status`
- [ ] `EmergencyStop` + `TempControl` components wired to control endpoints
- [ ] `/login` page with cookie auth
- [ ] Docker Compose: api + daemon + web + Caddy (`tls internal`)
- [ ] `generate-secrets.sh` generating `JWT_SECRET` and `DASHBOARD_PASSWORD`
- [ ] iPhone: trust Caddy local CA → Safari → Add to Home Screen

### Full v1

All MVP items, plus:

- [ ] Hypnogram chart (`Hypnogram.tsx`) with temperature overlay
- [ ] Trend charts with benchmark lines (`TrendChart.tsx`)
- [ ] ML recommendation card with confidence meter, predicted outcomes, LOW CONFIDENCE badge
- [ ] Revealed-preference display ("X manual overrides — anchored toward Y°F")
- [ ] Night-by-night data table with interventions
- [ ] Settings page wired to `GET/PUT /settings`
- [ ] Admin page with daemon health + decision log
- [ ] Alerts display + acknowledge
- [ ] Web Push notifications (via `push_subscriptions` table + service worker)
- [ ] Live Eight Sleep client (daemon with `--live` flag)
- [ ] Phenotype correlation table on `/learning`
- [ ] Short-sleep / DAMAGE CONTROL banner
- [ ] Note entry on night detail view

### Docker Compose layout

```yaml
# deploy/docker-compose.yml (outline)
services:
  api:
    build: { context: .., dockerfile: deploy/Dockerfile.api }
    environment:
      - SLEEPCTL_DB=/data/sleepctl.db
      - JWT_SECRET=${JWT_SECRET}
      - DASHBOARD_USER=${DASHBOARD_USER}
      - DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}
      - CORS_ORIGINS=https://sleep.local
    volumes:
      - db:/data
    expose: ["8000"]

  daemon:
    build: { context: .., dockerfile: deploy/Dockerfile.daemon }
    environment:
      - SLEEPCTL_DB=/data/sleepctl.db
    volumes:
      - db:/data
    command: ["python", "dashboard/daemon/run_daemon.py", "--poll-seconds", "5"]
    # add --live here once the Eight Sleep adapter is wired

  web:
    build: { context: ../dashboard/web, dockerfile: ../../deploy/Dockerfile.web }
    expose: ["3000"]

  caddy:
    image: caddy:2
    ports: ["443:443", "80:80"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config

volumes:
  db:
  caddy_data:
  caddy_config:
```

```
# deploy/Caddyfile
sleep.local {
  tls internal
  reverse_proxy /api/* api:8000
  reverse_proxy /* web:3000
}
```

### iPhone deployment checklist

1. **LAN setup.** Ensure the server machine has a static LAN IP (e.g. `192.168.1.50`). Add `192.168.1.50 sleep.local` to the local DNS resolver or `/etc/hosts` on the router (or use mDNS).

2. **Generate secrets.** Run `deploy/generate-secrets.sh` to write a `.env` file with `JWT_SECRET` (64 hex chars via `openssl rand -hex 32`) and `DASHBOARD_PASSWORD`. Keep this file out of git.

3. **Start containers.** `docker compose up -d`. Caddy auto-generates a local CA and a `sleep.local` TLS certificate on first start.

4. **Export Caddy root CA.** On the host:
   ```
   docker compose exec caddy caddy trust
   # or manually:
   docker cp sleepcontroller_caddy_data:/data/caddy/pki/authorities/local/root.crt .
   ```
   Email the `root.crt` file to yourself or host it at `http://192.168.1.50/ca.crt`.

5. **Trust CA on iPhone.** Open the `.crt` file in Safari (or Files). iOS prompts "Profile Downloaded" → Settings → General → VPN & Device Management → install the profile → Settings → General → About → Certificate Trust Settings → enable full trust for the Caddy root.

6. **Add to Home Screen.** Open `https://sleep.local` in Safari on iPhone. Wait for the page to load fully. Tap the Share button → "Add to Home Screen" → name it "sleepctl" → Add. The PWA icon appears on the Home Screen.

7. **Enable notifications (v1).** In Safari PWA, tap "Allow Notifications" when prompted. The service worker registers a push subscription and `POST /api/...` stores the endpoint in `push_subscriptions`. (Requires `web-push` library in the web container and a notification trigger in `services.py`.)

8. **Verify.** Open the app. The Home page should show "RUNNING" (green) with live SSE updates every 5 s. Navigate to /admin to confirm daemon is alive and pending commands = 0. Try Emergency Stop and verify the status card changes to "PAUSED" within 10 s.

---

## 12. Failure Modes and Safeguards

### Dashboard-layer failure modes

| Failure mode | Effect | Safeguard |
|---|---|---|
| API process crash | Dashboard goes offline | Control daemon is unaffected; device continues on its last commanded level. API restarts via Docker Compose `restart: unless-stopped`. |
| SSE stream drops | UI shows stale data | Client reconnects with 5 s exponential backoff. Stale timer fires after 15 s → gray banner "Reconnecting…" |
| `runtime_state` not updated for >180 s | Dashboard shows STALE banner (red) | `read_runtime_state` sets `stale=True` and `daemon_alive=False`. An alert of type `stale_data` / severity `critical` is auto-generated on the next `/alerts` poll. |
| Daemon crash | `runtime_state` goes stale | Device holds its last commanded temperature (Eight Sleep Pod retains the last level). STALE banner appears in the dashboard. User can restart the daemon container. |
| JWT secret lost (container restart without persisted `.env`) | All sessions invalidated | `generate-secrets.sh` is a one-time setup step; the secret is written to `.env` which is volume-mounted. Worst case: re-login. Control is not affected. |
| Two API requests race on `commands` table | Both writes succeed; daemon processes both | Daemon processes commands in insertion order. Duplicate commands are harmless (second `set_temp` overwrites the first within the same tick). |
| Manual temp command queued but daemon not running | Command stays `pending` indefinitely | Admin page shows "pending commands: N". User can see the command was not applied and restart the daemon. |

### Engine-layer failure modes (dashboard surfaces)

| Failure mode | Engine safeguard | Dashboard surface |
|---|---|---|
| Stale / delayed cloud sensor data | `SensorFrame.is_stale()` → controller holds last command, `reason = "data stale"` | Confidence shows low; decisions log shows "data stale; hold" |
| Wake-detection false positive | Wake recovery only stabilizes (never aggressive); auto-resumes when physiology settles | Intervention history shows `WAKE_RECOVERY hold`; no dramatic temperature jump |
| Single bad night skewing ML | Median+MAD baselines; `min_hold_nights = 3`; majority-rule revert | ML recommendation card shows "insufficient confidence" if baseline is disturbed |
| ML model below confidence threshold | `recommend_action` returns `None`; rule policy activates | LOW CONFIDENCE badge + "RULE POLICY" pill in Learning page |
| Illness/travel/confounded night | `clean_rows` excludes it from ML training | Learning page shows "X clean nights (N excluded as confounded)" |
| Device returns out-of-range level | `ThermalController.to_level()` clamps to [-100, 100] | `target_level` in runtime_state is always bounded; never shown OOB to user |
| Hot sleeper overcooling (temp too aggressive) | Slew ≤ 2°F/step; variability cap ≤ 3°F window; REM stays neutral | Settings page shows current limits; user can increase `neutral_temp_f` to pull the setpoint warm |
| Smart wake fires too early (deep sleep) | `SmartWakeRoutine.step()` checks stage: only fires on `LIGHT` or `AWAKE` within window | Tonight page shows "Smart wake: waiting for light sleep (window open)" |
| Smart wake audio | `WakeAlarmSpec.audio = False` always; `alarm_vibration_enabled` default is False in config (vibration only if explicitly enabled) | Settings shows "audio: always OFF"; vibration power = 0 disables all wake disturbance |
| Emergency Stop not applied | Commands table is the only path; daemon always drains pending before ticking | Command stays `pending` and is visible in Admin. Daemon crash + container restart will drain the stop command as the first action. |
| `--dry-run` mode active | `actuator.set_level()` is a no-op | Admin health shows "dry_run: true" in daemon extra state |

### Safety invariants that are never overridden

1. Temperature steps are always ≤ 2°F (`max_step_f`), enforced in `ThermalController.slew_limit()`.
2. Total thermal swing in a rolling window is always ≤ 3°F (`variability_cap_f`), enforced in `ThermalController.enforce_variability_cap()`.
3. Device level is always clamped to [-100, 100] (55–110°F), enforced in `to_level()`.
4. Audio is never used. `WakeAlarmSpec.audio` is hardcoded `False`.
5. The API never calls `pyEight` directly. The only path to the device is through the daemon's command queue.
6. The ML never acts with fewer than 14 clean nights (`min_nights`) or below 35% confidence (`conf_min`).
