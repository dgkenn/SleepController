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

### 1. Get a long-lived token from your dashboard

The phone authenticates with the **same login** as the dashboard (the token lasts 30 days).
From any machine that can reach your API, log in and copy the token:

```bash
curl -s -X POST https://YOUR-DASHBOARD/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"YOUR_DASHBOARD_PASSWORD"}'
# -> {"token":"eyJhbGciOi...","..."}
```

Copy the `token` value. (You can also grab it from Safari's dev tools cookie/`Authorization`
after logging into the web app.)

### 2. Configure Sensor Logger to stream to the endpoint

In **Sensor Logger**:
1. Enable only the **Accelerometer** (turn the rest off to save battery). A rate of
   **~50 Hz** is plenty.
2. Open **Settings → Data Streaming → HTTP Push** (a.k.a. "Push to server").
3. Set the URL to your ingest endpoint:
   ```
   https://YOUR-DASHBOARD/api/bcg/ingest
   ```
4. Add a custom **header**:
   ```
   Authorization: Bearer <the token from step 1>
   ```
5. Set the push interval to a few seconds (e.g. every 2–5 s).

Sensor Logger posts JSON in its native shape; the endpoint understands it directly. If your
app version lets you choose the body format, any of these work:

```jsonc
// Native Sensor Logger payload (list of samples) — handled automatically:
{ "payload": [ { "name": "accelerometer", "values": {"x":0.01,"y":0.00,"z":1.00} }, ... ] }

// Or the simple explicit form, if you build your own poster:
{ "fs": 50, "ax": [...], "ay": [...], "az": [...] }     // 3-axis batch in g
{ "fs": 50, "mag": [...] }                              // pre-collapsed magnitude
```

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
curl -s -X POST https://YOUR-DASHBOARD/api/bcg/ingest \
  -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
  -d '{"fs":50,"mag":[1,1,1,1]}'
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

### Piece 2 — starting/stopping the recording itself (pick one, all automatic, no tap)

Because the server can't start the app, tie Sensor Logger's start/stop to an automatic iOS
trigger. Sensor Logger exposes **Shortcuts actions** ("Start Recording" / "Stop Recording"), and
iOS **Personal Automations** can run them with no confirmation (toggle *Ask Before Running* off):

- **Charger (simplest, recommended).** You charge the phone at the bed anyway.
  - *Automation:* When **iPhone is connected to power** → Start Sensor Logger recording.
  - *Automation:* When **iPhone is disconnected from power** → Stop recording.
  - Plug in at bedtime → records all night; unplug in the morning → stops. Mid-night bathroom
    trips don't matter (Piece 1 gates them out server-side).

- **Sleep Focus (tracks your schedule).**
  - *Automation:* When **Sleep Focus turns on** → Start recording; **turns off** → Stop.
  - Lines up with your wind-down/wake schedule automatically.

- **Optional power-user: poll real bed presence.** The API exposes
  `GET /api/bcg/should-record` → `{"record": true|false}` driven by the Pod's actual presence.
  A Shortcuts automation can *Get Contents of URL* (with the `Authorization: Bearer <token>`
  header) and start/stop on the flag. This is the closest to literal "mattress senses me," but
  iOS only runs background automations on a loose schedule, so the charger/Sleep Focus triggers
  are more reliable for the start/stop itself.

> Net: set up a charger (or Sleep Focus) automation once, and from then on the phone records
> across the night while the controller uses it only when you're actually in bed.

## Nightly routine (with automation set up)

Nothing — plug in at bed, unplug in the morning. The recording starts/stops itself and the
controller fuses the movement only while you're in bed.

## How it flows through the system

```
iPhone accelerometer (≈50 Hz)
   │  Sensor Logger → HTTP push (Bearer token)
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
- Accelerometer batches are processed into movement/HR and **only the derived sample is kept**
  (a singleton row); the raw stream is not stored.
- The token is a normal dashboard session token — treat it like your password; rotate by simply
  logging in again (old tokens expire after 30 days).
