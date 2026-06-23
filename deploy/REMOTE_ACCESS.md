# Accessing the dashboard from your iPhone

You do **not** need the App Store (this is an installable PWA) and you do **not** need to pay.

## On the same WiFi (simplest, no signup)
1. On your always-on machine: `cd deploy && make up`
2. Find its LAN IP: `hostname -I | awk '{print $1}'` (e.g. 192.168.1.42)
3. iPhone Safari → `https://192.168.1.42/` → accept the local cert → log in
4. Share → **Add to Home Screen** → launches full-screen like an app

## From anywhere, free, NO signup — Cloudflare quick tunnel
After `make up`, run:
```bash
cd deploy && ./tunnel.sh          # installs cloudflared if needed, prints a public URL
```
It prints a `https://<random>.trycloudflare.com` URL — open that on your iPhone from any
network and Add to Home Screen. No account, no domain, no card. (The URL rotates each run; for
a stable URL use the free Tailscale or a Cloudflare named tunnel — both need a free account.)

## Keeping it perpetually on
The stack restarts automatically (`restart: unless-stopped`). Keep the host powered on (a
Raspberry Pi, an old laptop, a mini-PC, or any machine you leave running). Without a real
Eight Sleep Pod the control daemon runs in **simulator mode**, so the dashboard is fully
usable for testing before your Pod arrives.
