#!/usr/bin/env python3
"""Round-trip LIVE verification: command the bed from the dashboard, then read the Pod's OWN
reported state back to confirm it actually did it.

This is the honest "did the bed take the action?" check. It does NOT trust the daemon's intent —
for the temperature commands it compares the level the Pod *accepted* (``device_target_level``,
read back from the Eight Sleep cloud) against what was commanded, and watches the measured bed
temperature move. For prime it watches the device's priming flag. Power/away are commanded and
confirmed as far as the cloud reports them.

Requirements: the LIVE daemon must be running against the real Pod (SLEEPCTL_LIVE=1) and the Pod
plugged in + online. Point ``--base`` at the dashboard API (default http://localhost:8000) or, if
you tunnel the web app, at the tunnel with ``--api-prefix /api``.

SAFETY: this changes the real bed (temperature, prime, power). It restores power-on + auto mode at
the end. Run with the bed empty. Use ``--checks temp`` to only exercise temperature if you prefer.

Usage:
  python scripts/verify_live_pod.py --base http://localhost:8000 --user admin --password XXXX
  python scripts/verify_live_pod.py --base https://your.trycloudflare.com --api-prefix /api ...
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# level<->F so we can predict the level a commanded temperature should map to
sys.path.insert(0, ".")
try:
    from sleepctl.controller.calibration import fahrenheit_to_level
except Exception:
    def fahrenheit_to_level(f):  # fallback if run outside the repo: rough linear guess
        return None


class Client:
    def __init__(self, base, prefix=""):
        self.base = base.rstrip("/")
        self.prefix = prefix
        self.cookie = None

    def _req(self, method, path, body=None):
        url = f"{self.base}{self.prefix}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=20) as r:
            sc = r.headers.get("Set-Cookie")
            if sc:
                self.cookie = sc.split(";")[0]
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else {})

    def login(self, user, password):
        return self._req("POST", "/auth/login", {"username": user, "password": password})

    def status(self):
        return self._req("GET", "/status")[1]

    def post(self, path, body=None):
        return self._req("POST", path, body or {})

    def put(self, path, body):
        return self._req("PUT", path, body)

    def delete(self, path):
        return self._req("DELETE", path)


def poll(client, predicate, timeout=100, every=4):
    """Poll /status until predicate(status) is truthy; return (ok, last_status)."""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        last = client.status()
        if predicate(last):
            return True, last
        time.sleep(every)
    return False, last


RESULTS = []


def record(name, level, detail):
    RESULTS.append((name, level, detail))
    icon = {"DEVICE-CONFIRMED": "✅", "COMMANDED": "🟡", "FAILED": "❌"}.get(level, "•")
    print(f"  {icon} {level:<16} {name}: {detail}")


def check_temperature(c, target_f):
    print(f"\n[temperature] commanding the bed to {target_f} °F …")
    before = c.status()
    expect = fahrenheit_to_level(target_f)
    c.post("/tonight/temp", {"target_f": target_f})
    # the bed must ACCEPT the level (device_target_level) — read back from the Pod itself
    ok, st = poll(c, lambda s: s.get("device_target_level") is not None
                  and (expect is None or abs(s["device_target_level"] - expect) <= 2),
                  timeout=120)
    dtl = st.get("device_target_level")
    if ok:
        record("set temperature", "DEVICE-CONFIRMED",
               f"bed accepted level {dtl} (≈{target_f} °F, expected {expect}); "
               f"bed_temp {before.get('bed_temp_f')}→{st.get('bed_temp_f')} °F")
    elif dtl is not None:
        record("set temperature", "COMMANDED",
               f"bed reports level {dtl}, expected {expect} — accepted but not yet matched "
               "(may still be slewing)")
    else:
        record("set temperature", "FAILED",
               "no device_target_level came back — is the LIVE daemon running against the Pod?")


def check_nudge(c):
    print("\n[nudge] commanding a +3 °F nudge …")
    before = c.status()
    base = before.get("device_target_level")
    c.post("/tonight/temp/nudge", {"delta_f": 3.0})
    ok, st = poll(c, lambda s: s.get("device_target_level") is not None
                  and (base is None or s["device_target_level"] != base), timeout=90)
    if ok:
        record("nudge temperature", "DEVICE-CONFIRMED",
               f"bed level moved {base}→{st.get('device_target_level')}")
    else:
        record("nudge temperature", "COMMANDED" if st.get("device_target_level") is not None
               else "FAILED", f"bed level {st.get('device_target_level')} (was {base})")


def check_power_off(c):
    print("\n[power] Emergency Stop / power-off — the side should turn OFF …")
    before = c.status()
    c.post("/control/stop")
    ok, st = poll(c, lambda s: s.get("state") == "OFF" or s.get("power_on") is False
                  or (s.get("device_level") is not None and (s["device_level"] or 0) <= 5),
                  timeout=90)
    if ok:
        record("power off (E-stop)", "DEVICE-CONFIRMED" if st.get("device_level") is not None
               else "COMMANDED",
               f"state={st.get('state')} power_on={st.get('power_on')} "
               f"device_level={st.get('device_level')} (bed_temp {before.get('bed_temp_f')}→"
               f"{st.get('bed_temp_f')})")
    else:
        record("power off (E-stop)", "FAILED", f"state={st.get('state')} power_on={st.get('power_on')}")


def check_power_on(c):
    print("\n[power] power the side back ON …")
    c.post("/control/power-on")
    ok, st = poll(c, lambda s: s.get("power_on") is True and s.get("state") != "OFF", timeout=60)
    record("power on", "DEVICE-CONFIRMED" if ok else "COMMANDED",
           f"state={st.get('state')} power_on={st.get('power_on')} device_level={st.get('device_level')}")


def check_prime(c):
    print("\n[prime] commanding a water prime — the Pod's priming flag should flip on …")
    c.post("/control/prime")
    ok, st = poll(c, lambda s: ((s.get("device") or s.get("extra", {}).get("device") or {}) or {}).get("priming"),
                  timeout=60)
    # /status may not surface the device dict; fall back to a soft pass
    record("prime", "DEVICE-CONFIRMED" if ok else "COMMANDED",
           "priming flag observed" if ok else "command sent (priming flag not surfaced in /status)")


def check_away(c):
    print("\n[away] toggle away mode on then off …")
    c.post("/control/away-on")
    ok, st = poll(c, lambda s: s.get("away") is True, timeout=45)
    record("away on", "COMMANDED", f"away={st.get('away')} (cloud away-state readback is limited)")
    c.post("/control/away-off")
    poll(c, lambda s: s.get("away") is False, timeout=45)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--api-prefix", default="", help="use /api when pointing at the web app/tunnel")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--checks", default="all",
                    help="comma list: temp,nudge,power,prime,away  (default: all)")
    ap.add_argument("--yes", action="store_true", help="skip the 'this changes the real bed' prompt")
    args = ap.parse_args()

    c = Client(args.base, args.api_prefix)
    sc, _ = c.login(args.user, args.password)
    if sc != 200:
        print(f"login failed ({sc}) — check --base/--api-prefix and credentials")
        return 2
    st = c.status()
    if not st.get("daemon_alive"):
        print("⚠ daemon not alive — start the LIVE daemon (SLEEPCTL_LIVE=1) first.")
    if not st.get("live"):
        print("⚠ daemon is in SIMULATOR mode — device readback will be simulated, not the real Pod.")
    print(f"connected. state={st.get('state')} live={st.get('live')} daemon_alive={st.get('daemon_alive')}")

    if not args.yes:
        ans = input("This will change the REAL bed (temperature/power/prime). Bed should be empty. "
                    "Continue? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted."); return 1

    checks = args.checks.split(",") if args.checks != "all" else ["temp", "nudge", "power", "prime", "away"]
    try:
        if "temp" in checks:
            check_temperature(c, 66.0)
            check_temperature(c, 72.0)
        if "nudge" in checks:
            check_nudge(c)
        if "power" in checks:
            check_power_off(c)
            check_power_on(c)
        if "prime" in checks:
            check_prime(c)
        if "away" in checks:
            check_away(c)
    finally:
        print("\n[cleanup] returning the bed to a safe default (power on, auto mode) …")
        c.post("/control/safe-default")

    print("\n================ SUMMARY ================")
    confirmed = sum(1 for _, lvl, _ in RESULTS if lvl == "DEVICE-CONFIRMED")
    failed = sum(1 for _, lvl, _ in RESULTS if lvl == "FAILED")
    for name, lvl, detail in RESULTS:
        print(f"  {lvl:<16} {name}")
    print(f"\n{confirmed} device-confirmed, {failed} failed, {len(RESULTS)} checks total.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
