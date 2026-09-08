#!/usr/bin/env bash
# Last rung of the forwarder's recovery ladder, Linux edition.
#
# The forwarder cannot restart the Bluetooth stack itself, so after repeated barren sessions it
# drops .run/bt-reset.request and the privileged side acts. On Windows that side is the watchdog
# restarting bthserv; here it is this script, fired by bt-reset.path the moment the flag appears.
#
# What actually clears the failure modes seen in production:
#   * a stale BOND (the band was paired to a phone, or to this adapter in a previous mode):
#     `bluetoothctl remove <addr>` -- the thing WinRT never let us do cleanly;
#   * a wedged adapter: power-cycle it via bluetoothctl.
# Rate-limited to once per 10 minutes so a persistent fault cannot loop the adapter forever.
set -u
FLAG=/opt/sleepctl/.run/bt-reset.request
STAMP=/run/sleepctl-bt-reset.last
ADDR="${VERITY_ADDRESS:-}"
[ -f "$FLAG" ] || exit 0
reason=$(cat "$FLAG" 2>/dev/null || true)
rm -f "$FLAG"
now=$(date +%s)
if [ -f "$STAMP" ] && [ $((now - $(cat "$STAMP"))) -lt 600 ]; then
  logger -t verity-bridge "bt-reset requested ($reason) but one ran <10 min ago -- skipping"
  exit 0
fi
echo "$now" > "$STAMP"
logger -t verity-bridge "bt-reset: $reason"
if [ -n "$ADDR" ]; then
  bluetoothctl remove "$ADDR" >/dev/null 2>&1 || true
fi
bluetoothctl power off >/dev/null 2>&1 || true
sleep 2
bluetoothctl power on  >/dev/null 2>&1 || true
systemctl restart verity-forwarder.service || true
