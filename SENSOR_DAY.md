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
| 4 | Preflight → **GO / NO-GO** | See below |

At step 3 it pauses and asks you to put the armband on and press Enter. Single-press the button
until the LED shows the Bluetooth/HR mode (blue). Wear it on the **upper forearm**, not the wrist —
optical HR is much better there, and it's what the accuracy research assumed.

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
