#!/usr/bin/env bash
# One-shot installer for the Verity BLE bridge on a Raspberry Pi (Raspberry Pi OS / Debian).
#
#   sudo INGEST_URL='http://192.168.1.50:8000/hr/ingest?token=XXXX' \
#        VERITY_ADDRESS='24:AC:AC:16:96:1D' \
#        bash deploy/pi-bridge/install.sh
#
# INGEST_URL     the Windows box's API, reachable on the LAN, with BCG_INGEST_TOKEN as ?token=
# VERITY_ADDRESS the band's BLE address (scripts/verity_forwarder.py --scan prints it)
set -euo pipefail
: "${INGEST_URL:?set INGEST_URL}"
: "${VERITY_ADDRESS:?set VERITY_ADDRESS}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

apt-get update -qq
apt-get install -y -qq bluez python3-venv python3-pip git >/dev/null

id -u sleepctl >/dev/null 2>&1 || useradd --system --home /opt/sleepctl --shell /usr/sbin/nologin sleepctl
usermod -aG bluetooth sleepctl || true

mkdir -p /opt/sleepctl /etc/sleepctl
rsync -a --delete --exclude .git --exclude node_modules --exclude .run "$HERE/" /opt/sleepctl/
mkdir -p /opt/sleepctl/.run
chown -R sleepctl:sleepctl /opt/sleepctl

if [ ! -x /opt/sleepctl/.venv/bin/python ]; then
  sudo -u sleepctl python3 -m venv /opt/sleepctl/.venv
fi
sudo -u sleepctl /opt/sleepctl/.venv/bin/pip install -q --upgrade pip bleak

cat > /etc/sleepctl/bridge.env <<ENV
INGEST_URL=${INGEST_URL}
VERITY_ADDRESS=${VERITY_ADDRESS}
ENV
chmod 600 /etc/sleepctl/bridge.env

install -m 644 "$HERE/deploy/pi-bridge/verity-forwarder.service" /etc/systemd/system/
install -m 644 "$HERE/deploy/pi-bridge/bt-reset.service" /etc/systemd/system/
install -m 644 "$HERE/deploy/pi-bridge/bt-reset.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bluetooth
systemctl enable --now bt-reset.path
systemctl enable --now verity-forwarder.service

echo
echo "bridge installed. follow it with:  journalctl -u verity-bridge -f"
echo "on the Windows box set SLEEPCTL_VERITY=0 in deploy\\.env so two forwarders don't fight for the band."
