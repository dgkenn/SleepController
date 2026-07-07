# Going live with a real Eight Sleep Pod 2

This guide takes you from the simulator to **controlling your actual Pod** — both from the
`sleepctl` engine (CLI) and from the iPhone dashboard. It is deliberately **staged and
read-only first**: you validate sensing and *intended* actions before anything touches the bed.

> ⚠️ This is a comfort/automation tool, **not a medical device**. It is conservative by design:
> every command is slew-limited (≤ a couple °F per step), variability-capped, and clamped to the
> Pod's real **55–110 °F** range. Start in **dry-run** and only enable live writes once you're
> happy with what it would do.

---

## What drives the Pod

The real device is controlled through the community **`pyEight`** library
([lukas-clarke/pyEight](https://github.com/lukas-clarke/pyEight), the OAuth2 fork the Home
Assistant integration uses). The Docker image vendors it automatically; for a bare-metal run you
install it yourself (below).

Credentials are your **normal Eight Sleep app login** (email + password). Some newer accounts
also need an OAuth2 **client id / secret** — see [Troubleshooting](#troubleshooting).

---

## Part A — validate with the engine CLI (no dashboard, fully staged)

Do this first; it's the safest way to confirm the controller likes your real data.

### 0. Install (bare metal only — skip if you only use Docker)
```bash
pip install -e ".[eightsleep]"
git clone https://github.com/lukas-clarke/pyEight.git
export PYTHONPATH="$PWD/pyEight:$PYTHONPATH"
```

### 1. Authenticate
```bash
python -m sleepctl.cli auth            # stores creds 0600 at ~/.config/sleepctl/credentials.json
python -m sleepctl.cli auth --test     # connects and confirms login + network
```
(or set `EIGHTSLEEP_EMAIL` / `EIGHTSLEEP_PASSWORD` / `EIGHTSLEEP_SIDE` in the environment.)

### 2. Calibrate (read-only probe)
```bash
python -m sleepctl.cli calibrate
```
Confirms cooling is available, which biometric fields your Pod reports (HR / HRV / respiration /
stage / bed & room temp / presence), which commands exist, and the °F↔level mapping.
**Sends no commands.**

### 3. Dry-run a full night (read-only)
```bash
python -m sleepctl.cli run --dry-run --wake 07:00
```
Reads your real physiology all night and **logs the decisions it *would* make** — but writes
nothing to the bed. (Physiology availability depends on an active Autopilot subscription; if
your account has none, stages/HR/HRV will be empty and the controller will use alternative
data sources like the phone-BCG sensor for movement.) Review the printed ticks: do the stages,
targets and transitions look sane?

### 4. Go live (engine)
```bash
python -m sleepctl.cli run --wake 07:00
```
Now it actuates. The slew/variability/55–110 °F guards bound every command; `Ctrl-C` stops it
and closes the client cleanly.

---

## Part B — control the Pod from the iPhone dashboard

Once Part A looks good, point the dashboard's control daemon at the real device. The API, the
web app, and **all your existing controls** (temperature, power, away, prime, smart wake,
Emergency Stop) then drive the actual bed. Live status shows bed/room temps and available
physiology (HR/HRV/stage require an active Autopilot subscription; without it, movement data
from phone-BCG or Pod presence is used instead).

### 1. Put your credentials in `deploy/.env`
```dotenv
SLEEPCTL_LIVE=1
SLEEPCTL_DRY_RUN=1                 # <- keep this for the first night (read-only)
EIGHTSLEEP_EMAIL=you@example.com
EIGHTSLEEP_PASSWORD=your-password
EIGHTSLEEP_SIDE=right             # left | right
EIGHTSLEEP_TIMEZONE=America/New_York
# EIGHTSLEEP_CLIENT_ID=...        # only if plain login fails (see Troubleshooting)
# EIGHTSLEEP_CLIENT_SECRET=...
```

### 2. Start the stack
```bash
cd deploy/
make up
```
The `daemon` container now connects to your Pod. With `SLEEPCTL_DRY_RUN=1` it reads real data
and updates the dashboard, but **sends no commands** — the perfect first-night sanity check.
Watch it: `make logs` (look for `dashboard LIVE daemon started (dry_run=True)`).

### 3. Confirm on your iPhone
Open the dashboard (your LAN URL / tunnel). Home should show **real** bed/room temps. Sleep
stage, HR/HRV will appear if you have an active Autopilot subscription; otherwise those fields
will be empty and movement/HR come from phone-BCG if available. The **Admin** page shows the
daemon as *Live (dry-run)*.

### 4. Hand it the controls
When you're satisfied, set `SLEEPCTL_DRY_RUN=0` in `.env` and `make up` again. Now:
- **Tonight → temperature / mode** drives the real bed in realtime.
- **Power / Away / Prime** call the device directly.
- **Smart Wake** programs the Pod's alarm (heat + gentle vibration, audio off).
- **Emergency Stop** hard-offs your side immediately.

The controller's learning loop runs each night exactly as in the simulator, now on real
outcomes.

---

## Safety model (always on)

| Guard | Effect |
|---|---|
| `--dry-run` / `SLEEPCTL_DRY_RUN=1` | Reads + decides, sends **zero** device commands |
| Slew limit (`max_step_f`) | Never moves more than ~1–2 °F per control step |
| Variability cap | Bounds total thermal swing within a window |
| 55–110 °F clamp | Commands can never exceed the device's real range |
| Stale-data hold | If Pod data is old/low-confidence, the controller holds |
| Emergency Stop | Always available; turns the side off regardless of mode |
| API ↔ daemon decoupling | A UI/API crash can never disturb the running control loop |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `live mode requires Eight Sleep credentials` in daemon logs | Set `EIGHTSLEEP_EMAIL` / `EIGHTSLEEP_PASSWORD` in `.env`; it falls back to simulator otherwise |
| `pyeight` import error / "live Pod mode unavailable" | The image couldn't vendor pyEight at build (network). Rebuild with internet: `make build`, or bare-metal `git clone` + `PYTHONPATH` as in Part A |
| Login fails with plain email/password | Newer accounts need OAuth2 `client_id`/`client_secret`. The lukas-clarke `pyEight` / HA `eight_sleep` docs publish the known values and how to capture them; set `EIGHTSLEEP_CLIENT_ID` / `EIGHTSLEEP_CLIENT_SECRET` in `.env` |
| Bed presence looks wrong | Pod 2 presence can be unreliable; the controller already treats it conservatively and holds on low-confidence/stale data |
| Want to go back to simulator | Set `SLEEPCTL_LIVE=0` (or remove it) and `make up` |
