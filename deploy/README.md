# sleepctl dashboard — self-hosted deployment

A single `make up` starts the full stack on any always-on Linux box with Docker.
No API keys, no sign-ups, no external services required.

---

## Prerequisites

| Requirement | Check |
|---|---|
| Docker + Docker Compose plugin | `docker compose version` |
| Python 3.x on the host (for `make up` password gen) | `python3 --version` |
| Ports 80 and 443 free | `ss -tlnp \| grep -E '80\|443'` |

---

## iPhone deployment checklist

### 1. Start the stack

```bash
cd deploy/
make up
```

On **first run** `make up` will:
- Copy `.env.example` → `.env`
- Auto-generate a random `DASHBOARD_PASSWORD` (printed to the terminal — **save it**)
- Build Docker images and start four containers: `api`, `daemon`, `web`, `caddy`
- The API container auto-generates a `JWT_SECRET` and writes it to the shared
  `sleepdata` volume (`/data/.jwt_secret`) so it survives restarts

### 2. Find your host's LAN IP

```bash
hostname -I | awk '{print $1}'
```

Example output: `192.168.1.42`

### 3. Open in Safari on iPhone

Navigate to `https://192.168.1.42/` (use **Safari** — other browsers don't support
full PWA install on iOS).

### 4. Accept the self-signed certificate

Caddy uses its built-in local CA (`tls internal`) to issue HTTPS certificates.
No public domain or ACME account is needed.

**One-time tap flow:**
1. Safari shows "This Connection Is Not Private"
2. Tap **Show Details** → **visit this website** → **Visit Website**
3. The dashboard loads over HTTPS

#### Optional: install Caddy's root CA for a green lock (no warning ever again)

```bash
# On the host — extract the root cert Caddy generated:
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

Then AirDrop or email `caddy-root.crt` to your iPhone:
1. Tap the file → **Allow** → **Close**
2. Go to **Settings → General → VPN & Device Management**
3. Tap the "Caddy Local Authority" profile → **Install** → enter passcode
4. Go to **Settings → General → About → Certificate Trust Settings**
5. Toggle **Caddy Local Authority** to full trust → **Continue**

Safari now shows a green lock for your LAN IP.

### 5. Log in

Use the credentials from `deploy/.env`:
- **Username:** value of `DASHBOARD_USER` (default `admin`)
- **Password:** the auto-generated value of `DASHBOARD_PASSWORD`

### 6. Install as a PWA

1. Tap the **Share** icon (box with arrow pointing up) in Safari's toolbar
2. Scroll down and tap **Add to Home Screen**
3. Edit the name if desired → tap **Add**

The sleepctl dashboard now appears on your iPhone home screen as a full-screen
app (no browser chrome, offline-capable via service worker).

### 7. Enable notifications (optional)

When prompted by the app, tap **Allow** to enable push/local notifications for
sleep-window alerts.

---

## Seed demo data

If you don't have a real Eight Sleep Pod the daemon runs in **simulator mode**
automatically. Populate 21 nights of realistic demo data:

```bash
make seed
```

Refresh the dashboard to see trends, analytics, and sleep scores.

---

## Daily operations

| Command | Effect |
|---|---|
| `make up` | Start / rebuild stack (idempotent) |
| `make down` | Stop containers (data is preserved in volumes) |
| `make logs` | Tail live logs from all services |
| `make ps` | Show container status |
| `make seed` | Populate demo sleep data |

---

## Stack architecture

```
iPhone Safari
     │  HTTPS :443
     ▼
  [Caddy]  ← tls internal (local CA, no domain needed)
     │
     ├─ /api/*        → api:8000   (FastAPI, PBKDF2+JWT auth, SSE)
     ├─ /api/stream/* → api:8000   (SSE, flush_interval -1)
     └─ /*            → web:3000   (Next.js PWA)

  [daemon]            ← same image as api; shares sleepdata volume
                        (writes runtime_state; reads commands from api)

Volumes:
  sleepdata   — SQLite DB + JWT secret file + daemon state
  caddy_data  — Caddy TLS certs + local CA
```

---

## Security notes

- Caddy's local CA is **not trusted by default** — the cert warning is expected
  and harmless on a LAN-only setup.
- `JWT_SECRET` is auto-generated (64 hex chars) on first start and stored only
  inside the `sleepdata` Docker volume — it never leaves the host.
- The dashboard uses single-user auth with PBKDF2-SHA256 (200 000 iterations)
  password hashing — no external auth provider.
- All traffic between the iPhone and host is encrypted (TLS 1.3 via Caddy).

---

## Remote access (optional, free)

For access away from home WiFi you need one extra tool — both are free but
require an account:

**Tailscale** (easiest, end-to-end encrypted):
1. Install Tailscale on the host and on your iPhone (free tier covers both)
2. The host gets a stable Tailscale IP (e.g. `100.x.x.x`) — use that instead
   of the LAN IP; the certificate warning goes away if you set up a Tailscale
   HTTPS cert

**Cloudflare Tunnel** (no port-forwarding, works behind CGNAT):
1. Install `cloudflared` on the host
2. `cloudflared tunnel --url http://localhost:80` (Cloudflare provides a public
   `https://*.trycloudflare.com` URL for free with no account)

Neither option is required for the local-WiFi + PWA use case described above.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make up` says port 80/443 in use | Stop the conflicting service or change Caddy's ports in `docker-compose.yml` |
| iPhone can't reach the host | Ensure host firewall allows TCP 443 inbound; check `ufw status` |
| "502 Bad Gateway" from Caddy | `make logs` — likely the API is still starting; wait 15 s and reload |
| Password forgotten | `grep DASHBOARD_PASSWORD deploy/.env` |
| JWT errors after restart | The secret persists in the `sleepdata` volume — only regenerates if the volume is deleted |
| Daemon shows "simulator mode" | Normal without a Pod — real device needs Eight Sleep credentials (see main README) |
