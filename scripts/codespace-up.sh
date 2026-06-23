#!/usr/bin/env bash
# Start the sleepctl dashboard inside a GitHub Codespace (or any machine with Python+Node).
# Runs the API, the control daemon (simulator mode), and the web PWA as plain processes,
# seeds demo data on first run, and prints the login password. No Docker needed here — for a
# real always-on home deployment use deploy/ (docker compose) instead.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ---- config (override by exporting before running) -------------------------------------
export SLEEPCTL_DB="${SLEEPCTL_DB:-$ROOT/sleepctl.db}"
export JWT_SECRET="${JWT_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export DASHBOARD_USER="${DASHBOARD_USER:-admin}"
export DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_hex(4))')}"
export PYTHONPATH="$ROOT:$ROOT/dashboard/api"

# ---- init + seed demo data (only if the DB has no nights yet) ---------------------------
python3 - <<'PY'
import os
from app.db import connect, get_repo
from app.security import ensure_bootstrap_user
from app.seed import seed
connect()
ensure_bootstrap_user()
repo = get_repo()
try:
    has_data = len(repo.recent_nights(1)) > 0
finally:
    repo.close()
if not has_data:
    seed(21)
    print("[codespace-up] seeded 21 demo nights")
else:
    print("[codespace-up] existing data found; skipping seed")
PY

# ---- start services --------------------------------------------------------------------
# setsid fully detaches each service into its own session so they keep running after this
# launcher script returns (and survive a SIGTERM to the launcher's process group).
mkdir -p "$ROOT/.run"
echo "[codespace-up] starting API on :8000 ..."
setsid bash -c "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir dashboard/api" \
  > "$ROOT/.run/api.log" 2>&1 < /dev/null &

echo "[codespace-up] starting control daemon (simulator) ..."
setsid bash -c "exec python3 dashboard/daemon/run_daemon.py" \
  > "$ROOT/.run/daemon.log" 2>&1 < /dev/null &

echo "[codespace-up] starting web on :3000 ..."
setsid bash -c "cd dashboard/web && exec env API_URL=http://localhost:8000 PORT=3000 npm run dev" \
  > "$ROOT/.run/web.log" 2>&1 < /dev/null &

# ---- wait for the API to answer (best-effort) ------------------------------------------
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break
  sleep 1
done

cat <<EOF

────────────────────────────────────────────────────────────────────────
  sleepctl dashboard is starting.

  1. Open the PORTS tab, set port 3000 -> Port Visibility: Public
  2. Open the forwarded https://…-3000.app.github.dev URL in iPhone Safari
  3. Log in:
        username: $DASHBOARD_USER
        password: $DASHBOARD_PASSWORD
  4. Share -> Add to Home Screen to install the PWA

  Logs: .run/api.log  .run/daemon.log  .run/web.log
  Stop: pkill -f 'uvicorn app.main' ; pkill -f run_daemon.py ; pkill -f 'next dev'
────────────────────────────────────────────────────────────────────────
EOF
