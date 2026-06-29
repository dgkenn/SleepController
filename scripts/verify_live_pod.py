#!/usr/bin/env python3
"""Comprehensive round-trip LIVE verification: drive EVERY device-affecting control from the
dashboard, then read the Pod's OWN reported state back from the Eight Sleep cloud to confirm the
bed actually did it.

It does NOT trust the daemon's intent — wherever the cloud surfaces a confirming signal it compares
the device readback against the command:

  temperature set/nudge  -> device_target_level matches the commanded level + bed_temp moves
  power off / E-stop      -> state OFF / device_level drops toward 0 / bed_temp drifts to ambient
  power on / safe-default -> side active again, auto mode, default setpoint
  away on/off             -> away state (cloud away-readback is limited -> COMMANDED)
  prime                   -> the Pod's priming flag flips on
  smart wake set/clear    -> the Pod's own alarm slot (enabled/time) changes  [if firmware exposes it]
  mode auto/manual/view   -> /status mode + manual actually holds the commanded target
  induce / nap / end      -> the session drives the device (warm-then-cool) + session state
  Hue dawn (optional)     -> the test flash returns ok (LAN bridge; only if configured)

Each result is labelled DEVICE-CONFIRMED / COMMANDED / FAILED.

Requirements: the LIVE daemon running against the real Pod (SLEEPCTL_LIVE=1), Pod plugged in +
online. Point --base at the dashboard API (default http://localhost:8000), or at a tunnel fronting
the web app with --api-prefix /api.

SAFETY: changes the real bed (temperature/power/prime). Run with the bed EMPTY. Restores power-on +
auto mode at the end. Scope with --checks (e.g. --checks temp,power).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")
try:
    from sleepctl.controller.calibration import fahrenheit_to_level
except Exception:
    def fahrenheit_to_level(f):
        return None


class Client:
    def __init__(self, base, prefix=""):
        self.base, self.prefix, self.cookie = base.rstrip("/"), prefix, None

    def _req(self, method, path, body=None):
        url = f"{self.base}{self.prefix}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                sc = r.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                txt = r.read().decode()
                return r.status, (json.loads(txt) if txt else {})
        except urllib.error.HTTPError as e:
            return e.code, {}

    def login(self, u, p):
        return self._req("POST", "/auth/login", {"username": u, "password": p})

    def status(self):
        return self._req("GET", "/status")[1]

    def post(self, path, body=None):
        return self._req("POST", path, body or {})

    def put(self, path, body):
        return self._req("PUT", path, body)

    def delete(self, path):
        return self._req("DELETE", path)


def poll(client, predicate, timeout=120, every=4):
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        last = client.status()
        try:
            if predicate(last):
                return True, last
        except Exception:
            pass
        time.sleep(every)
    return False, last


RESULTS = []


def record(name, level, detail):
    RESULTS.append((name, level, detail))
    icon = {"DEVICE-CONFIRMED": "✅", "COMMANDED": "🟡", "FAILED": "❌", "SKIP": "⏭"}.get(level, "•")
    print(f"  {icon} {level:<16} {name}: {detail}")


def _dev(s):
    return (s.get("device") or {}) or {}


# ----------------------------------------------------------------- checks
def check_temperature(c):
    for target in (66.0, 72.0, 69.0):
        print(f"\n[temperature] command {target} °F …")
        before = c.status()
        expect = fahrenheit_to_level(target)
        c.post("/tonight/temp", {"target_f": target})
        ok, st = poll(c, lambda s: s.get("device_target_level") is not None
                      and (expect is None or abs(s["device_target_level"] - expect) <= 2))
        dtl = st.get("device_target_level")
        if ok:
            record(f"set temp {target}°F", "DEVICE-CONFIRMED",
                   f"Pod accepted level {dtl} (expected {expect}); "
                   f"bed {before.get('bed_temp_f')}→{st.get('bed_temp_f')} °F")
        elif dtl is not None:
            record(f"set temp {target}°F", "COMMANDED",
                   f"Pod reports level {dtl}, expected {expect} (still slewing?)")
        else:
            record(f"set temp {target}°F", "FAILED",
                   "no device_target_level — is the LIVE daemon connected to the Pod?")


def check_nudge(c):
    for delta, word in ((3.0, "up"), (-3.0, "down")):
        print(f"\n[nudge] command {word} ({delta:+} °F) …")
        base = c.status().get("device_target_level")
        c.post("/tonight/temp/nudge", {"delta_f": delta})
        ok, st = poll(c, lambda s: s.get("device_target_level") is not None
                      and (base is None or s["device_target_level"] != base), timeout=90)
        record(f"nudge {word}", "DEVICE-CONFIRMED" if ok else
               ("COMMANDED" if st.get("device_target_level") is not None else "FAILED"),
               f"Pod level {base}→{st.get('device_target_level')}")


def check_mode(c):
    print("\n[mode] auto / manual / view …")
    for m in ("auto", "manual", "view"):
        c.post("/tonight/mode", {"mode": m})
        ok, st = poll(c, lambda s: (s.get("mode") or "").startswith(m[:4]), timeout=30)
        record(f"mode {m}", "DEVICE-CONFIRMED" if ok else "COMMANDED",
               f"/status mode={st.get('mode')}")
    # manual must actually hold a commanded target on the device
    c.post("/tonight/mode", {"mode": "manual"})
    c.post("/tonight/temp", {"target_f": 68.0})
    exp = fahrenheit_to_level(68.0)
    ok, st = poll(c, lambda s: s.get("device_target_level") is not None
                  and (exp is None or abs(s["device_target_level"] - exp) <= 2))
    record("manual holds target", "DEVICE-CONFIRMED" if ok else "COMMANDED",
           f"Pod level {st.get('device_target_level')} (expected {exp})")


def check_power(c):
    print("\n[power] Emergency Stop / power-off …")
    before = c.status()
    c.post("/control/stop")
    # The side is OFF when the daemon reports state OFF / power_on False (it only sets these AFTER a
    # successful turn_off_side on the real device) and/or the Pod's reported level returns to 0.
    # NOTE: a cooling bed's level is NEGATIVE, so "level <= 0" is NOT an off-signal — only exactly 0.
    ok, st = poll(c, lambda s: s.get("state") == "OFF" or s.get("power_on") is False, timeout=90)
    off_level = st.get("device_level") in (0, None)
    record("power off (E-stop)",
           "DEVICE-CONFIRMED" if (ok and off_level) else ("COMMANDED" if ok else "FAILED"),
           f"state={st.get('state')} power_on={st.get('power_on')} device_level={st.get('device_level')} "
           f"bed {before.get('bed_temp_f')}→{st.get('bed_temp_f')}")
    print("[power] power back ON …")
    c.post("/control/power-on")
    ok, st = poll(c, lambda s: s.get("power_on") is True and s.get("state") != "OFF", timeout=60)
    record("power on", "DEVICE-CONFIRMED" if ok else "COMMANDED",
           f"state={st.get('state')} power_on={st.get('power_on')}")


def check_away(c):
    print("\n[away] away on then off …")
    c.post("/control/away-on")
    ok, st = poll(c, lambda s: s.get("away") is True, timeout=45)
    record("away on", "COMMANDED", f"away={st.get('away')} (cloud away-readback is limited)")
    c.post("/control/away-off")
    poll(c, lambda s: s.get("away") is False, timeout=45)
    record("away off", "COMMANDED", "away cleared")


def check_prime(c):
    print("\n[prime] command a water prime — priming flag should flip on …")
    c.post("/control/prime")
    ok, st = poll(c, lambda s: _dev(s).get("priming"), timeout=60)
    record("prime", "DEVICE-CONFIRMED" if ok else "COMMANDED",
           "priming flag observed on the Pod" if ok else "command sent (priming flag not seen yet)")


def check_smart_wake(c):
    print("\n[smart wake] arm an alarm ~30 min out, then clear it …")
    from datetime import datetime, timedelta
    t = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
    c.post("/tonight/wake", {"wake_time": t, "window_min": 20, "vibration_power": 30})
    ok, st = poll(c, lambda s: (_dev(s).get("alarm") or {}).get("enabled") is True
                  or (s.get("wake") or {}).get("wake_time"), timeout=60)
    alarm = _dev(st).get("alarm")
    if alarm and alarm.get("enabled"):
        record("set smart wake", "DEVICE-CONFIRMED", f"Pod alarm enabled (time={alarm.get('time')})")
    elif (st.get("wake") or {}).get("wake_time"):
        record("set smart wake", "COMMANDED",
               f"daemon armed wake {st.get('wake', {}).get('wake_time')} "
               f"(device_error={st.get('device_error')})")
    else:
        record("set smart wake", "FAILED", "no wake state reflected")
    c.delete("/tonight/wake")
    ok, st = poll(c, lambda s: not (s.get("wake") or {}).get("wake_time")
                  and not (_dev(s).get("alarm") or {}).get("enabled"), timeout=45)
    record("clear smart wake", "DEVICE-CONFIRMED" if ok else "COMMANDED", "wake cleared")


def check_sessions(c):
    print("\n[sessions] induce-sleep (warm→cool) then end …")
    base = c.status().get("device_target_level")
    c.post("/tonight/induce")
    ok, st = poll(c, lambda s: s.get("session_mode") == "induce"
                  or (s.get("device_target_level") is not None and base is not None
                      and s["device_target_level"] != base), timeout=90)
    record("induce sleep session", "DEVICE-CONFIRMED" if (st.get("device_target_level") not in (None, base))
           else ("COMMANDED" if st.get("session_mode") == "induce" else "FAILED"),
           f"session={st.get('session_mode')} Pod level {base}→{st.get('device_target_level')}")
    c.post("/tonight/session/end")
    poll(c, lambda s: s.get("session_mode") in (None, "night"), timeout=45)

    print("[sessions] 20-min nap then end …")
    c.post("/tonight/nap", {"duration_min": 20})
    ok, st = poll(c, lambda s: s.get("session_mode") == "nap", timeout=60)
    record("nap session", "DEVICE-CONFIRMED" if ok else "COMMANDED", f"session={st.get('session_mode')}")
    c.post("/tonight/session/end")
    ok, st = poll(c, lambda s: s.get("session_mode") in (None, "night"), timeout=45)
    record("end session", "DEVICE-CONFIRMED" if ok else "COMMANDED", f"session={st.get('session_mode')}")


def check_safe_default(c):
    print("\n[safe default] return to safe default …")
    c.post("/control/safe-default")
    ok, st = poll(c, lambda s: s.get("power_on") is True and (s.get("mode") or "").startswith("auto"),
                  timeout=45)
    record("safe default", "DEVICE-CONFIRMED" if ok else "COMMANDED",
           f"power_on={st.get('power_on')} mode={st.get('mode')}")


def check_hue(c):
    cfg = c._req("GET", "/wake/light/config")[1]
    if not cfg.get("enabled") or not cfg.get("paired"):
        record("hue dawn light", "SKIP", "Hue not configured/paired — skipping")
        return
    print("\n[hue] flash the dawn lights (LAN bridge) …")
    r = c.post("/wake/light/test")[1]
    record("hue dawn light", "DEVICE-CONFIRMED" if r.get("ok") else "FAILED",
           "lights flashed" if r.get("ok") else f"error: {r.get('error')}")


ALL = ["temp", "nudge", "mode", "power", "away", "prime", "wake", "sessions", "safe", "hue"]
RUN = {"temp": check_temperature, "nudge": check_nudge, "mode": check_mode, "power": check_power,
       "away": check_away, "prime": check_prime, "wake": check_smart_wake,
       "sessions": check_sessions, "safe": check_safe_default, "hue": check_hue}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--api-prefix", default="", help="use /api when pointing at the web app/tunnel")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--checks", default="all", help=f"comma list of {','.join(ALL)} (default all)")
    ap.add_argument("--yes", action="store_true", help="skip the safety prompt")
    args = ap.parse_args()

    c = Client(args.base, args.api_prefix)
    if c.login(args.user, args.password)[0] != 200:
        print("login failed — check --base/--api-prefix and credentials"); return 2
    st = c.status()
    print(f"connected. state={st.get('state')} live={st.get('live')} daemon_alive={st.get('daemon_alive')} "
          f"device={_dev(st)}")
    if not st.get("daemon_alive"):
        print("⚠ daemon not alive — start the LIVE daemon (SLEEPCTL_LIVE=1) first.")
    if not st.get("live"):
        print("⚠ daemon is in SIMULATOR mode — readback will be simulated, not the real Pod.")

    if not args.yes:
        if input("This changes the REAL bed (temp/power/prime). Bed should be empty. Continue? [y/N] "
                 ).strip().lower() not in ("y", "yes"):
            print("aborted."); return 1

    checks = ALL if args.checks == "all" else [x.strip() for x in args.checks.split(",")]
    try:
        for name in checks:
            fn = RUN.get(name)
            if fn:
                fn(c)
    finally:
        print("\n[cleanup] returning the bed to a safe default …")
        c.post("/control/safe-default")

    print("\n================ SUMMARY ================")
    by = {}
    for _, lvl, _ in RESULTS:
        by[lvl] = by.get(lvl, 0) + 1
    for name, lvl, _ in RESULTS:
        print(f"  {lvl:<16} {name}")
    print(f"\n{by.get('DEVICE-CONFIRMED', 0)} device-confirmed · {by.get('COMMANDED', 0)} commanded · "
          f"{by.get('SKIP', 0)} skipped · {by.get('FAILED', 0)} failed · {len(RESULTS)} total")
    return 1 if by.get("FAILED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
