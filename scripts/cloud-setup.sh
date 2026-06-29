#!/usr/bin/env bash
# SleepController -- one-shot deploy on a fresh Ubuntu cloud VM (Oracle Always-Free, etc.).
# Installs Docker, clones the repo, generates secrets, brings up the stack + a Cloudflare tunnel,
# and prints the HTTPS URL + login. Everything auto-restarts (Docker restart policies) so it
# survives reboots. Run it once over SSH:
#
#   curl -fsSL https://raw.githubusercontent.com/dgkenn/SleepController/main/scripts/cloud-setup.sh | bash
#
# Eight Sleep credentials are entered into deploy/.env afterward (the script prints how).
set -euo pipefail
ROOT="$HOME/SleepController"

echo "==> Installing Docker (if missing)..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
DC="docker compose"
docker compose version >/dev/null 2>&1 || DC="sudo docker compose"

echo "==> Cloning / updating the repo..."
if [ -d "$ROOT/.git" ]; then git -C "$ROOT" pull --ff-only; else git clone https://github.com/dgkenn/SleepController.git "$ROOT"; fi
cd "$ROOT"

echo "==> Generating secrets (deploy/.env)..."
ENVF="$ROOT/deploy/.env"
if [ ! -f "$ENVF" ]; then
  rand() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
  PW="$(rand 4)"
  cat > "$ENVF" <<EOF
SLEEPCTL_DB=/data/sleepctl.db
JWT_SECRET=$(rand 32)
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=$PW
BCG_INGEST_OPEN=0
SLEEPCTL_LIVE=1
SLEEPCTL_DRY_RUN=1
# --- Eight Sleep login (fill these in, then re-run the up command below) ---
# EIGHTSLEEP_EMAIL=you@example.com
# EIGHTSLEEP_PASSWORD=your-password
# EIGHTSLEEP_TIMEZONE=America/New_York
# EIGHTSLEEP_SIDE=right
EOF
  echo "    Dashboard login:  admin  /  $PW   (saved in deploy/.env)"
else
  echo "    deploy/.env exists -- leaving it as is."
fi

echo "==> Building + starting the stack (this takes a few minutes the first time)..."
cd "$ROOT/deploy"
$DC -f docker-compose.yml -f docker-compose.cloud.yml up -d --build api daemon web cloudflared

echo "==> Waiting for the Cloudflare tunnel URL..."
URL=""
for _ in $(seq 1 30); do
  URL="$($DC -f docker-compose.yml -f docker-compose.cloud.yml logs cloudflared 2>/dev/null \
        | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [ -n "$URL" ] && break
  sleep 2
done

echo ""
echo "============================================================"
if [ -n "$URL" ]; then
  echo "  Dashboard (HTTPS, reachable anywhere):  $URL"
else
  echo "  Tunnel URL not found yet -- check:  cd deploy && $DC -f docker-compose.yml -f docker-compose.cloud.yml logs cloudflared"
fi
echo "  Login:  admin  /  (see DASHBOARD_PASSWORD in deploy/.env)"
echo ""
echo "  NEXT: add your Eight Sleep login to deploy/.env, then re-run:"
echo "    cd ~/SleepController/deploy"
echo "    $DC -f docker-compose.yml -f docker-compose.cloud.yml up -d daemon"
echo "============================================================"
