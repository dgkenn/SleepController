#!/usr/bin/env bash
# Zero-signup public HTTPS URL for your iPhone via a Cloudflare *quick tunnel*.
# No Cloudflare account, no domain, no port-forwarding. The URL changes each run.
# Usage: ./tunnel.sh            (after `make up`, points at the Caddy proxy on :443/:80)
set -euo pipefail
PORT="${1:-80}"
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Installing cloudflared..."
  ARCH=$(uname -m); case "$ARCH" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; *) A=amd64;; esac
  curl -L -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${A}" 
  chmod +x /usr/local/bin/cloudflared
fi
echo "Opening a public tunnel to http://localhost:${PORT} ..."
exec cloudflared tunnel --url "http://localhost:${PORT}"
