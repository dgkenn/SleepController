# Run SleepController on your Windows PC (home server) + live Pod bring-up

This is the home-server setup for a Windows 11 PC (e.g. the box at `192.168.1.99`), with the Pod
controlled live. It's staged so the **first live action is read-only** and safe — and because the
Pod is currently on the **bucket rig** (a thermal load, no person, no mattress), even full live
actuation is risk-free for this validation.

> Everything runs natively (Python + Node) — no Docker, no reboot. All commands are **PowerShell**.

---

## Step 0 — Install the prerequisites (one time)

Open **PowerShell** (Start → type "PowerShell" → Enter) and run these. Windows 11's `winget`
fetches them:

```powershell
winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
winget install -e --id Git.Git           --accept-package-agreements --accept-source-agreements
winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
```

**Then CLOSE PowerShell and open a NEW one** (so it picks up the newly-installed tools). Verify:

```powershell
python --version    # 3.11.x
git --version
node --version
```

If any says "not recognized," reopen PowerShell once more (or reboot) and re-check.

---

## Step 1 — Set up the project (one time)

```powershell
iwr -useb https://raw.githubusercontent.com/dgkenn/SleepController/main/scripts/windows-setup.ps1 | iex
```

That clones the repo to `C:\Users\<you>\SleepController`, vendors `pyEight`, creates a Python
venv, installs everything, installs the web app, and generates your dashboard login (printed at
the end — **write it down**). If you'd rather not pipe-to-run, clone first and run the script
locally:

```powershell
git clone https://github.com/dgkenn/SleepController.git $HOME\SleepController
cd $HOME\SleepController
powershell -ExecutionPolicy Bypass -File scripts\windows-setup.ps1
```

---

## Step 2 — Connect to your Pod (no manual login)

The daemon logs in automatically from `deploy\.env` — **put your Eight Sleep credentials there once**
and you never type a password again:

```ini
# deploy\.env  (this file is gitignored — it stays only on this PC, never committed)
EIGHTSLEEP_EMAIL=you@example.com
EIGHTSLEEP_PASSWORD=your-eight-sleep-password
EIGHTSLEEP_SIDE=right
EIGHTSLEEP_TIMEZONE=America/New_York
```

> ⚠️ Without `EIGHTSLEEP_EMAIL`/`_PASSWORD` in `deploy\.env`, live mode **silently falls back to the
> simulator** — the dashboard shows `live: false` and the bed never moves. This is the #1 "it didn't
> work" cause. (`windows-setup.ps1` adds these keys as blanks for you to fill.)
> If plain email/password fails, newer accounts need an OAuth2 client id/secret — see LIVE_POD.md.

### Optional read-only check — sends zero commands to the bed:

```powershell
cd $HOME\SleepController
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$HOME\SleepController;$HOME\SleepController\pyEight"
# load the creds from deploy\.env into this shell, then probe:
Get-Content deploy\.env | ForEach-Object { if ($_ -match '^\s*([^#=]+)=(.*)$') { Set-Item ("env:"+$matches[1].Trim()) $matches[2].Trim() } }
python -m sleepctl.cli calibrate
```

This confirms the real Pod is reachable, that **cooling** is supported, which fields come back
(HR / HRV / breath / stage / temps), the available commands, and the °F↔level mapping. **Nothing
is sent to the bed.** This single command validates the entire live data path against your
hardware — the biggest unknown in the whole project.

---

## Step 3 — Drive the Pod live (safe on the bucket)

A read-only dry-run night (real sensing, logged decisions, **no device writes**):

```powershell
python -m sleepctl.cli run --dry-run --wake 07:00 --max-ticks 30
```

Then real actuation — because the Pod is on the bucket (no person), this just heats/cools the
bucket water, so it's a safe live test of the control loop:

```powershell
python -m sleepctl.cli run --wake 07:00 --max-ticks 60
```

Watch it command levels; the controller's slew/variability/55–110 °F guards bound every move.
`Ctrl+C` stops it anytime.

---

## Step 4 — Start the home server (the iPhone dashboard)

```powershell
cd $HOME\SleepController
powershell -ExecutionPolicy Bypass -File scripts\windows-dashboard.ps1
```

This launches the API, the control daemon (live, **dry-run** for the first night), and the web
app. It prints your LAN URL. On your **iPhone** (same WiFi), open:

```
http://192.168.1.99:3000
```

Log in with `admin` + the password from Step 1, then **Share → Add to Home Screen** to install the
PWA. Drop dry-run once you're happy (set `SLEEPCTL_DRY_RUN=0` in `deploy\.env` and relaunch) — and
once the Pod is back on a real mattress, do the same staged dry-run-first there.

`windows-dashboard.ps1` is the **manual** launcher (start once, runs until you reboot or it
crashes). For an **always-on laptop**, use Step 5 instead.

---

## Step 5 — Make it ALWAYS-ON (laptop: survives reboot, crash, and a closed lid)

Run **once** in an **Administrator** PowerShell (right-click PowerShell → *Run as administrator*):

```powershell
cd $HOME\SleepController
git pull                       # get the always-on scripts
powershell -ExecutionPolicy Bypass -File scripts\windows-always-on.ps1
```

That does two things:

1. **Keeps the laptop awake on AC** — disables sleep/hibernate and sets *lid-close = do nothing*
   while plugged in, so you can shut the lid and walk away. (Keep it on the charger; the display
   may still turn off, which is fine — it doesn't stop the controller.)
2. **Registers a `SleepController` Scheduled Task** that launches a **watchdog**
   (`windows-watchdog.ps1`) at every boot/logon and **restarts it if it ever dies**. The watchdog
   in turn supervises the API + daemon + web and restarts any one that crashes. It uses the
   **production** web server (`next start`) for 24/7 stability.

Verify / manage it:

```powershell
Get-Content $HOME\SleepController\.run\watchdog.log -Wait      # watch it come up
Get-Content $HOME\SleepController\.run\watchdog.heartbeat      # last-alive timestamp
Get-ScheduledTask SleepController                              # task state
Stop-ScheduledTask SleepController ; Get-Process python,node | Stop-Process -Force   # stop
Unregister-ScheduledTask SleepController -Confirm:$false       # remove always-on
```

Now reboot once to prove it: the dashboard should come back on its own, no login required.

> Remote access (off your WiFi): you already have **Tailscale**/**cloudflared** — see
> [REMOTE_ACCESS.md](REMOTE_ACCESS.md). The always-on task makes the tunnel target always reachable.

> **Mac/Linux laptop instead?** Same idea, different tools: a `launchd` plist (macOS) or `systemd`
> service (Linux) running the equivalent watchdog, plus `caffeinate`/`pmset disablesleep` (macOS)
> or `systemd-inhibit` + `logind.conf HandleLidSwitch=ignore` (Linux). Tell me and I'll add it.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python` / `git` / `npm` "not recognized" | Reopen PowerShell (or reboot) after Step 0 so PATH updates. |
| `pyeight` import error | `git clone https://github.com/lukas-clarke/pyEight.git` into the repo folder and set `$env:PYTHONPATH = "$HOME\SleepController\pyEight"`. |
| Login fails (plain email/password) | Set `EIGHTSLEEP_CLIENT_ID` / `EIGHTSLEEP_CLIENT_SECRET` (see LIVE_POD.md). |
| iPhone can't reach `:3000` / `:8000` | Same WiFi? Allow the ports through Windows Firewall when prompted (Private network). |
| Script blocked | Run with `powershell -ExecutionPolicy Bypass -File <script>`. |
