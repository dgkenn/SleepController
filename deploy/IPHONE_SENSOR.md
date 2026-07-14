# Use your iPhone as an in-bed motion sensor (zero device risk)

You already sleep with your phone in bed — so it can be a **second, independent sensor** that
gives the controller **sub-minute movement** (and a best-effort heartbeat) the Pod cloud can't.
The Pod's cloud vitals are floored at ~**60 s**; that's too coarse for catching the first stir
of an awakening. Your iPhone's accelerometer samples **tens of times per second**, so it sees
the toss-and-turn *as it happens* and fuses it onto the Pod frame the controller already uses.

**It never touches the Pod.** This is a completely separate sensor streaming to your own
dashboard — there is no way for it to harm or brick the bed. Worst case, the phone data is
ignored and the controller runs on Pod-cloud data exactly as before.

## What it actually buys you (honest)

| Signal | iPhone gives you | vs Pod cloud |
|---|---|---|
| **Movement / restlessness** | **Excellent**, sub-second | Pod is ~60 s binned |
| **Respiration** | Decent when still (chest/bed coupling) | ~60 s, session-level |
| **Heart rate** | *Best-effort only* — the phone is not on your body, so beat-to-beat is unreliable; treated as advisory | Pod BCG is the cardiac source of record |

So the win is **fast movement** — the single most useful early signal for the awakening
detector and the closed loop. We deliberately **do not** let the phone's shaky HR override the
Pod's; movement is what gets fused with confidence.

## Why an app (and not just a web page)

A web page **cannot read the accelerometer with the screen off** — iOS suspends it. To stream
all night you need a small **background sensor-logger app**. The cleanest, free option:

### Recommended: **Sensor Logger** (by Kelvin Choi, free on the App Store)

It records the accelerometer in the background and can **push batches to an HTTP endpoint** —
which is exactly the endpoint this dashboard exposes. No coding, no jailbreak.

## One-time setup (~5 minutes)

> **Note (Windows deploy):** the always-on box runs via `scripts/windows-watchdog.ps1` + `deploy\.env`,
> **not** docker-compose — so the compose "publish port 8000 / uncomment `ports:`" step does not
> apply here. The watchdog opens **port 3000** in Windows Firewall (Next.js), which proxies
> `/api/*` to the API; use the `:3000/api/...` Push URL below.

### 1. Auth: pick one of three options

You pass auth as a `?token=` on the Push URL (Sensor Logger's header-less HTTP push can't set an
`Authorization` header reliably, so the query param is what actually works). The three options:

- **(a) Static ingest token via `?token=` — recommended.** Set a non-expiring shared secret in
  `deploy/.env`:

  ```
  BCG_INGEST_TOKEN=<paste output of: python -c "import secrets; print(secrets.token_urlsafe(24))">
  ```

  Then put that same value on the Push URL as `?token=<BCG_INGEST_TOKEN>`. This keeps the phone
  endpoints **authenticated even over the public Tailscale funnel**, and it **never expires** —
  nothing to re-mint on the phone every month. Best default.

- **(b) 30-day dashboard JWT via `?token=`.** The phone authenticates with the same login as the
  dashboard; the token **expires after ~30 days** and must be re-minted:

  ```bash
  curl -s -X POST https://YOUR-DASHBOARD/api/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"YOUR_DASHBOARD_PASSWORD"}'
  # -> {"token":"eyJhbGciOi...","..."}
  ```

  Copy the `token` value and use it as `?token=<TOKEN>`.

- **(c) `BCG_INGEST_OPEN=1` — no token (LAN-trusted only).** Drops auth on **just the two phone
  endpoints** so a header-less device can stream with no token. Every other endpoint stays
  login-protected. Enable this ONLY on a trusted LAN — note it **also drops auth on the funnel's
  ingest path**, so anyone who can reach the funnel URL could POST to it. Prefer (a) if the API is
  exposed to the internet at all.

### 2. Configure Sensor Logger to stream to the endpoint

Sensor Logger's **Settings → HTTP Push** screen (the one with **Push URL** + **Auth Header**):

1. On the **Logger** page, enable only the **Accelerometer** (turn the rest off to save
   battery). The default rate is fine — the server auto-detects it.
2. **Enable HTTP Push.**
3. **Push URL** — point it at the ingest endpoint:
   - **Recommended (home WiFi, via the web proxy):**
     `http://192.168.1.163:3000/api/bcg/ingest?token=<BCG_INGEST_TOKEN>`
     — hit **port 3000** (Next.js), which **proxies `/api/*`** (including the POST body **and**
     the `?token=` query) through to the API. On the always-on **Windows** deployment the
     watchdog opens **port 3000** in Windows Firewall but **NOT 8000**, so this is the path that
     actually works out of the box. (Replace the IP with your server's LAN IP if different.)
   - Over the internet (HTTPS via the dashboard funnel): `https://YOUR-DASHBOARD/api/bcg/ingest?token=<BCG_INGEST_TOKEN>`
   - Direct to the API on port 8000 (older text suggested this):
     `http://YOUR-SERVER-IP:8000/bcg/ingest?token=<BCG_INGEST_TOKEN>` — note the **path is
     `/bcg/ingest`** with **no `/api`** prefix when hitting the API directly. **Requires opening
     the 8000 firewall port first** (the Windows watchdog does not); prefer the `:3000/api/...`
     path above.
4. **Auth** — put your token on the Push URL as `?token=<value>` per option (a)/(b) in step 1.
   If you chose option (c) `BCG_INGEST_OPEN=1`, no token is needed — **leave the Auth Header
   blank** and drop the `?token=` from the URL. (Sensor Logger's `Authorization`/`Bearer <token>`
   header field also works where present, but the `?token=` query is the reliable path.)
5. **Batch Period** — **1 s is perfect** (don't pay for 100/200 ms; the server keeps a rolling
   window, so 1-second batches are plenty). Leave **Skip Writing** off, **Send Images** off.
6. Hit **Test Push** — you want a `200` with `{"ok": true, "ingested": …, "fs_source": "detected"}`.

That's it — Sensor Logger POSTs its native JSON (`{messageId, sessionId, deviceId, payload:[…]}`);
the endpoint pulls the accelerometer entries out, **auto-detects the sample rate** from their
timestamps (no `fs` to set), and ignores any other sensors. Custom posters can also send
`{"fs":50,"ax":[…],"ay":[…],"az":[…]}` or `{"fs":50,"mag":[…]}`.

> The token is a normal 30-day dashboard session token — treat it like your password; rotate by
> logging in again to mint a fresh one. On a home-WiFi `http://…:8000` URL the token rides the
> local network only; over the internet always use the `https://` dashboard URL.

### 3. Start streaming and confirm it's working

Start the recording in Sensor Logger, then check the dashboard **Admin / Data Health** — you'll
see a **phone sensor** block:

```json
"phone_sensor": { "streaming": true, "fusing": true, "age_seconds": 3.1, "movement": 0.04 }
```

- `streaming: true` → batches are arriving.
- `fusing: true` → the sample is fresh enough (< 90 s) that the daemon is overlaying it onto the
  Pod frame in real time.

You can also confirm a single batch from the command line:

```bash
curl -s -X POST "https://YOUR-DASHBOARD/api/bcg/ingest?token=<token>&fs=50" \
  -H 'Content-Type: application/json' -d '{"mag":[1,1,1,1]}'
# -> {"ok": true, "ingested": 4, "buffered": 4, ...}
```

## Start/stop automatically (no remembering)

You asked: *can it just start when the mattress senses I'm in bed and stop when I get up?*

**The honest limit:** iOS sandboxes apps, so neither the mattress nor this server can reach into
your iPhone and launch a third-party app's recording. That part has to be triggered on the phone
itself. But the behavior splits into two pieces, and the important one is fully automatic:

### Piece 1 — using the phone is already presence-driven (automatic, server-side)

The daemon **only fuses the phone feed while the Pod senses you in bed.** The moment bed presence
drops (you got up), the phone data is ignored — and it re-engages the instant you're back in bed.
You don't configure anything: Admin → Phone Sensor shows `In bed — fused` / `Out of bed —
ignored`. So even if the phone records 24/7, the controller only *acts* on it when you're in bed.

### Piece 2 — the smart charger automation (set up once, then never touch it)

Because the server can't start the app, you trigger it on the phone with two iOS **Personal
Automations** that fire on plugging/unplugging the charger. Sensor Logger exposes start/stop
**deep links** so a Shortcut can drive it:

```
sensorlogger://start     # start a recording
sensorlogger://stop      # stop the recording
```

**"Smart" part:** a bare charger trigger would also start recording during a daytime top-up. So
the start automation includes a **time-of-day guard** — it only records during your sleep
window. Daytime charging does nothing.

#### Automation A — start recording when you plug in at night

1. **Shortcuts** app → **Automation** tab → **+** → **Create Personal Automation**.
2. Trigger: **Charger** → **Is Connected** → **Next**.
3. Add action **If** (search "If"). For the condition, tap to build:
   *If* **Time** *is between* **9:00 PM** and **9:00 AM** (set to your real sleep window).
   - To get "Current Time" as the If input, add a **Get Current Date** action above the If and
     compare its time, or use the **"Time of Day"** condition if your iOS version offers it.
4. Inside the **If** (the true branch), add **Open URLs** → set it to `sensorlogger://start`.
5. **Next** → turn **Ask Before Running** **OFF** (so it runs silently) → **Done**.

#### Automation B — stop recording when you unplug in the morning

1. New Personal Automation → Trigger: **Charger** → **Is Disconnected** → **Next**.
2. Action: **Open URLs** → `sensorlogger://stop`.
3. **Next** → **Ask Before Running OFF** → **Done**.

That's the whole thing: **plug in at bed → it records (only in your night window); unplug in the
morning → it stops.** Mid-night bathroom trips don't matter — the recording keeps running and the
server (Piece 1) simply ignores the phone while you're out of bed.

> **Even simpler alternative (no Shortcuts):** Sensor Logger has a built-in **Rule Engine**
> (gear icon → Rules) that can **start/stop on time of day** directly — e.g. start at 21:00,
> stop at 09:00. That covers the "only at night" part without the charger at all; use it if you
> don't always charge in bed. (Battery: streaming all night is fine on the charger; on battery,
> the time-bounded rule keeps it to your sleep window.)

> **Optional power-user — poll real bed presence.** `GET /api/bcg/should-record?token=<token>`
> returns `{"record": true|false}` from the Pod's actual presence. A periodic Shortcut can fetch
> it and start/stop on the flag — the closest to literal "mattress senses me" — but iOS runs
> background automations on a loose schedule, so the charger trigger is more reliable for the
> actual start/stop.

## Nightly routine (with the charger automation set up)

Nothing — plug in at bed, unplug in the morning. The recording starts/stops itself (only in your
night window) and the controller fuses the movement only while you're actually in bed.

## How it flows through the system

```
iPhone accelerometer (≈50 Hz)
   │  Sensor Logger → HTTP push (Auth Header: Bearer <token>, or ?token=)
   ▼
POST /api/bcg/ingest ──▶ BCGProcessor (rolling window)
   │   accel magnitude → detrend gravity → movement (+best-effort HR/HRV)
   ▼
bridge.live_sensor (singleton sample)  ◀── written by the API
   │
   ▼
daemon BridgeWearableSource.read_sample() ──▶ fuse_sample() onto the Pod frame
   │   (movement overrides the coarse Pod bin; HR only if the Pod's is missing)
   ▼
the same controller / awakening detector — no controller changes
```

The fusion is **age-gated**: a phone sample older than 90 s is ignored, so if you forget to
start the recording or the phone dies, the controller silently falls back to Pod-cloud data.

## Toggle / privacy

- The daemon reads the phone feed only when `SLEEPCTL_PHONE_SENSOR` is unset or truthy (default
  on). Set `SLEEPCTL_PHONE_SENSOR=0` to ignore it entirely.
- Accelerometer batches are processed into movement/HR. The derived samples are appended to a
  `sensor_samples` history table (retrievable via `GET /diag/sensor-history` for analysis and
  learning); independently, the live singleton row (`bridge.live_sensor`) is what the daemon
  reads for real-time fusion. The raw accelerometer stream itself is not stored.
- The token is a normal dashboard session token — treat it like your password; rotate by simply
  logging in again (old tokens expire after 30 days).
