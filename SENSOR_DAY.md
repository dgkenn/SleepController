# Sensor day — one page

## The whole thing

Charge the armband. Then, on the controller PC:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\go-live.ps1
```

That is the entire procedure. It updates the checkout, re-runs itself on the new code, checks the
machine, brings the stack up, sets up the Verity, waits for real samples, and prints GO or NO-GO.

It is safe to re-run as many times as you like, and it never touches the Pod.

**Why one command instead of a list:** this box has been off for a while, which means it is running
old scripts. Running "the setup script" would run the *old* setup script — the one without the
ingest-token minting or the streaming check — so you could follow instructions perfectly and still
end up with a feed that never delivers. `go-live.ps1` pulls first and re-executes itself on the new
code specifically to close that trap.

## What you'll see

Five steps, each printing `[ok]` / `[warn]` / `[FAIL]`:

| Step | What it does | If it fails |
|---|---|---|
| 0 | Pull, re-exec if this script changed | Network — it continues on old code and says so |
| 1 | venv, `deploy\.env`, AC power, disk | Run `scripts\windows-setup.ps1` first |
| 2 | Scheduled Task + api/daemon/web up | Needs `windows-always-on.ps1` once, as Administrator |
| 3 | BLE library, scan, token, enable, **wait for samples** | It prints a ranked checklist |
| 3b | Per-channel sensor report (PATH + LIVE) | See "Which sensors" below |
| 4 | Preflight → **GO / NO-GO** | See below |

At step 3 it pauses and asks you to put the armband on and press Enter. Single-press the button
until the LED shows the Bluetooth/HR mode (blue). Wear it on the **upper forearm**, not the wrist —
optical HR is much better there, and it's what the accuracy research assumed.

## First: does the armband stream everything?

Before wiring it into the controller, prove the hardware end. One command, 60 seconds:

```powershell
.venv\Scripts\python.exe scripts\verity_stream_test.py --seconds 120
```

Wear it on your **upper forearm** and single-press until the LED shows Bluetooth/HR mode first —
a Verity sitting on a desk streams HR happily and tells you nothing about whether it works on a
body. Make sure the **Polar phone app is not connected**; it holds an exclusive link.

It starts all four channels, listens, and reports each one as STREAMING / SILENT / REFUSED with
sample counts and rates:

| Channel | What it's for | Expected |
|---|---|---|
| Heart rate (0x180D) | the authoritative cardiac signal | ~1/s within seconds |
| RR intervals (0x180D) | beat-to-beat -> HRV; irreplaceable training data | ~1/s |
| Accelerometer (PMD) | actigraphy **without** the phone | ~52/s |
| Pulse-to-pulse (PMD) | Polar's own beat intervals + error estimate | first batch ~25s in |

**Use `--seconds 120`.** PPI takes ~25s for its first batch, so a 60s run can end before it
arrives and look like a failure. If PPI is still silent past ~60s the armband is probably in SDK
mode — power-cycle it (hold the button until it switches off, then on).

Nothing is written to the database and no Pod command is sent; it is purely a listening test.
It ends with a **PASTE THIS BACK** block — counts and rates only, no identifiers — which is what
to send me.

## Which sensors, and what "working" means

Every sensor here is optional by design — the controller degrades rather than stops — which makes
a silent channel look identical to a broken one, and both look identical to one that was never set
up. `scripts\verify_sensors.py` separates the three:

- **PATH** — does the ingest/decode/fusion code work? Verified with synthetic data; needs no
  hardware. **All eight channels currently pass.**
- **LIVE** — is real data arriving right now? Read from the database, only meaningful on the box.
- **ROLE** — what the controller loses without it.

| Channel | Role | Expected on day one |
|---|---|---|
| Verity cardiac (HR/HRV) | onset, arousal, wake-risk, staging | OK once worn |
| Verity accelerometer | motion **without** the phone | OK once worn |
| iPhone accelerometer | sub-second motion when the phone's in bed | QUIET — never set up |
| Eight Sleep Pod frame | stage/presence *if* the membership is active | likely QUIET |
| Bed temperature | closed-loop feedback + arrival timing | blocked by the prime |
| Weather / ambient | feed-forward setpoint pre-compensation | QUIET until a location is set |
| Work calendar (ICS) | the wake deadline the night is planned around | QUIET — not connected |
| Sleep stager (derived) | turns HR into the stage steered on | OK — weights are bundled |
| Hue dawn light (output) | sunrise ramp + therapy lamp at wake | QUIET until a bridge is paired |

QUIET is not broken. Only the Verity channels and the bed temperature actually matter for tonight.

Run it any time on its own:

```powershell
.venv\Scripts\python.exe scripts\verify_sensors.py --db <your sleepctl.db>
```

## If Eight Sleep says alarms are subscription-only

The controller has never gone through the app UI — it talks to the Eight Sleep API directly with
your own credentials. So a paywall enforced in the *app* may not apply to the *API*, and it can
only ever MODIFY an existing alarm, which you already have from before the change. Whether the
server still accepts the write is an empirical question, so ask it:

```
GET /diag/alarm-probe?token=<DIAG_TOKEN>
```

It reads your existing alarm and PUTs it back byte-for-byte — a no-op that changes nothing about
the alarm while proving whether writes are accepted. Three possible answers:

- **`writable: true`** — nothing more to do. Vibration works as designed.
- **`writable: false` with 402/403** — the refusal is server-side, so no client can work around
  it. The controller detects this, stops retrying, and says so; see the fallback below.
- **no alarm slot** — see the next section.

**If vibration really is gone, you still wake up.** The thermal wake ramp and the Hue sunrise are
driven by the controller through the ordinary setpoint and light paths, which are not gated —
they're the same mechanisms it uses all night. So the wake degrades from *light + warmth +
vibration* to *light + warmth*. That is a real loss for someone who needs silence, which is why it
raises an alert and shows as `Wake alarm (vibration)` in `/diag` rather than failing quietly. With
vibration gone, **configuring the Hue bridge stops being optional** — it becomes your main cue.

## One thing to check in the Eight Sleep app

The Pod's alarm API can only **modify an existing alarm** — it cannot create one. If you have never
made a wake alarm in the Eight Sleep app, there is no slot to drive and the vibration alarm cannot
be programmed at all.

So, once: open the Eight Sleep app and create a single wake alarm. Any time; sleepctl then manages
its time, level and vibration silently from then on, and never enables audio.

If it's missing you'll see `wake alarm programming` in the `degraded` check with that remedy. It
retries every tick, so fixing it mid-night still works.

## How the wake actually goes

Verified tick by tick over a simulated window (`tests/test_wake_sequence.py` pins all of it):

```
06:30  deep   hold        vib   0            light 0.00   waiting for a light-sleep moment
06:40  deep   dawn        vib   0            light 0.00   holding through deep — dawn ramp on
06:46  deep   dawn        vib   0            light 0.30   holding through deep — dawn ramp on
06:48  light  gentle      vib  30  slow      light 0.50   light moment in-window — waking gently
06:50  light  escalate    vib  70  medium    light 0.50   waking in progress
06:52  light  fire        vib 100  continuous light 0.60  waking in progress
07:00  light  fire        vib 100  continuous light 1.00  deadline reached — guaranteed wake
```

Three things happening in order: the **light leads** (dawn ramp starts ~20 min out, while you're
still asleep), the alarm **waits through deep sleep** rather than firing on a timer, and the
**vibration builds** — gentle/slow, then stronger/medium, then full/continuous. Most mornings never
reach the loud end. If no light moment ever comes, the deadline still fires at full power: no
light-sleep moment must never mean no wake.

**The light needs Hue configured** (bridge IP, token, and at least one target bulb) in the
dashboard. Without it the wake works identically, just silently and in the dark — `verify_sensors`
reports the channel as QUIET rather than broken.

## Expected first-run outcome

Realistically you will get **NO-GO on the first run**, and that is fine — it tells you what's left.
The likely blockers, and what each means:

- **Water-loop / thermal capacity** — the stuck prime from before. The bed can't move heat, so the
  thermal layer is inert. This is the one that actually matters; everything else is downstream.
- **Control daemon / API** — the box was off. Step 2 starts them; re-run and they should clear.
- **Cardiac sensor** — clears the moment the armband streams. If it doesn't, `.run\verity.log` has
  the reason; a `401` means the ingest token (re-run go-live, it mints one).

A **GO_DEGRADED** verdict means the night goes ahead with something reduced. That's a real night —
sleep on it.

## The morning after

```powershell
sleepctl checkin
```

Twenty seconds, and it's the ground truth two learners train on. Without it they never move off the
literature priors, and "tailored to me" stays theoretical. This is the highest-value thing you do
after the sensor itself.

## Once the water loop is healthy

Two in-bed calibrations turn generic presets into your numbers. Both need a loop that can actually
move heat, which is why they come after the prime is fixed:

1. **Thermal self-test** — `POST /diag/action/self-test`, or the Admin page button. Measures the
   bed's real heat/cool rates and lags. This sizes the pre-cool lead, the onset cascade and the wake
   ramp; without it all three use generic presets.
2. **Comfort sweep** — from the dashboard, in bed. Establishes your neutral °F. Every thermal intent
   is an offset from it, so a wrong neutral shifts the entire night's policy and no amount of clever
   steering corrects a wrong anchor.

`GET /diag` will show `Personal calibration: 3 of 3 missing` until these are done. That is
informational, not a fault — the controller runs correctly on priors, they just aren't yours yet.

## If something looks wrong

```powershell
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
```

Paste the whole output. Read `watchdog.heartbeat` first: **days old** means the box was away;
**fresh** means the git push is failing and `health-publish.result` has the reason.

Remotely, from your phone: `GET /diag/preflight?token=<DIAG_TOKEN>&format=text` gives the same
verdict without touching the machine.

## What's deliberately OFF

Not oversights — each is waiting on evidence that only real nights can supply:

- **Thermal dose-response trial** — needs a verified water loop first. Running it on a Pod that
  can't move heat would faithfully measure a broken actuator and hand you a confident, wrong answer
  about your ideal temperature.
- **Post-wake cool snap** — opt-in; it changes what the bed does around your alarm.
- **REM-unblock steering** — off until the per-person A/B says it helps *you*.

## What happens automatically

- The forwarder is relaunched by the watchdog if it dies, and reconnects if the armband drops.
- Around 19:00–21:00 the controller runs its own preflight and **pushes your phone only if tonight
  is a NO-GO** — while there's still time to fix it. A degraded night isn't paged; alerting on a
  night that will happen anyway just teaches you to ignore alerts.
- Any subsystem that fails quietly is counted and surfaced as the `degraded` check, so "everything
  green but the feature isn't running" can't hide.
