# Test the dashboard from your iPhone — GitHub Codespaces

This is the **zero-install, no-new-signup** way to try the sleepctl dashboard on your iPhone.
You already have a GitHub account, and Codespaces' free tier covers testing.

> **Simulator mode.** The control daemon runs against a simulator here, so every control moves
> *simulated* state — your real Eight Sleep Pod is never touched. This is for trying the full
> app experience. Wiring real Pod control is a separate step.

> **Free-tier reality check.** Codespaces free tier is ~60 core-hours/month and **auto-idles
> after ~30 minutes** of inactivity. That's perfect for *testing*, but it is **not a 24/7 host**
> for running the controller every night. For a permanent always-on deployment later, use the
> Docker setup in [`README.md`](./README.md) on an always-on machine (an always-free Oracle
> Cloud VM or a cheap Raspberry Pi work well).

---

## Steps

### 1. Create a Codespace
On GitHub: open **`dgkenn/SleepController`** → **`Code ▾`** → **Codespaces** tab → **Create
codespace on `main`**.

Wait ~1–2 min while it builds — the devcontainer automatically installs the Python and Node
dependencies (`pip install -e .`, the API requirements, and the web `npm` packages).

### 2. Start the dashboard
In the Codespace terminal:

```bash
./scripts/codespace-up.sh
```

This seeds 21 nights of demo data, starts the API + control daemon + web app, and **prints your
login password** (save it).

### 3. Make port 3000 reachable
Open the **PORTS** tab (next to the terminal) → find port **3000** → right-click →
**Port Visibility → Public**.

> Public means no GitHub login is needed to open the URL — the dashboard's own username/password
> still protects it. (If you prefer, leave it Private and just sign into GitHub in iPhone Safari.)

**Get the full URL.** Every Codespaces forwarded address has this exact shape:

```
https://<your-codespace-name>-3000.app.github.dev
```

i.e. your codespace's name, then `-3000` (the port), then `.app.github.dev`. For example:

```
https://glorious-dollop-5gp9q7r4x3-3000.app.github.dev
```

The PORTS panel **truncates the display** (e.g. `https://glorious-dollop-5gp…`) — the link itself
is complete. To copy the whole thing, hover over the **Forwarded Address** and click the
**copy / clipboard icon** that appears (or click the address to open it in a browser tab and copy
the full URL from the address bar). Then **text or email that link to yourself** so you can open it
on your iPhone without retyping it.

### 4. Open it on your iPhone
In **Safari**, open your full forwarded URL —
`https://<your-codespace-name>-3000.app.github.dev` — and log in:
- **Username:** `admin`
- **Password:** the value printed by the script

### 5. Install as an app (PWA)
Tap **Share** (box-with-arrow) → **Add to Home Screen** → **Add**. It now runs full-screen from
your home screen.

### 6. Try it out
- **Home** — live status, realtime temperature, last-night Perfect Sleep score, wake-up survey
- **Tonight** — mode, temperature, power/away, Smart Wake (night type + window + vibration), the
  wake-aware Sleep Plan
- **Data / Analytics** — seeded trends, charts, history
- **Learning** — ML status + the Sleep Maintenance card (wake-risk windows, lead-times, prevention)

---

## Stop / restart
```bash
# stop the services
pkill -f 'uvicorn app.main'; pkill -f run_daemon.py; pkill -f 'next dev'
# restart
./scripts/codespace-up.sh
```
When you're done, **Stop** the Codespace from the GitHub Codespaces page so it doesn't burn
free-tier hours. Your data persists in the Codespace until you delete it.

## Troubleshooting
| Symptom | Fix |
|---|---|
| URL shows a GitHub sign-in | Port 3000 is Private — set it to **Public**, or sign into GitHub in Safari |
| Login fails | Re-read the password from the script output, or `grep DASHBOARD_PASSWORD` isn't used — it's printed only; just re-run the script to see it |
| Page won't load | `cat .run/web.log` / `.run/api.log` — wait ~15 s after start and reload |
| "Polling" / Stale badge | Normal in simulator mode between daemon ticks |
