#!/usr/bin/env python3
"""Find, configure and test the wake-therapy smart plug -- one command.

WHY THIS EXISTS RATHER THAN A DOCUMENTED MODEL NUMBER
-----------------------------------------------------
The plug on this deployment is an **Amysen YX-WS01** (FCC ID ``2AOT8-WS01``, grantee Shenzhen
Yexiang Intelligent Technology). That model number is NOT a reliable protocol identifier: at
least five vendors (Amysen, Esicoo, Ecoey, Tuya, unbranded) have shipped plugs labelled YX-WS01,
and both the firmware and the CHIPSET changed across those runs -- early units were ESP-based and
Tuya-convertible, later ones use a TG7100C (a BL602 clone), and the newest ones are Matter
devices with Bluetooth onboarding under a *different* FCC ID (``2AOT8-YX-WS01``).

So this script does not guess. It probes the LAN and reports what is actually there, which is the
only thing that settles it. Guessing from the label is exactly how you end up with a lamp that
silently fails to fire at 05:30.

USAGE
-----
    python scripts/setup_wake_plug.py --scan          # what is on the network?
    python scripts/setup_wake_plug.py --test-on       # fire the configured plug now
    python scripts/setup_wake_plug.py --test-off

Scanning needs ``tinytuya`` (``pip install tinytuya``). Getting the ``local_key`` needs the free
Tuya IoT Platform account linked to your Smart Life app -- ``python -m tinytuya wizard`` walks
through it and writes ``devices.json``. That key is what makes control LOCAL: once you have it,
the plug is driven over your own LAN with no vendor cloud in the path, which matters for a device
whose entire job is to fire at a fixed moment every morning.

Nothing here writes to the plug unless you pass a --test flag.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _scan() -> int:
    try:
        import tinytuya
    except Exception:
        print("tinytuya is not installed. Run:  pip install tinytuya")
        return 2
    print("scanning the LAN for Tuya-protocol devices (~18s) ...")
    try:
        found = tinytuya.deviceScan(False, 20)
    except Exception as exc:
        print(f"scan failed: {exc!r}")
        return 1
    if not found:
        print("\nNo Tuya-protocol devices answered.")
        print("That is INFORMATIVE, not a dead end -- it means this unit is very likely one of")
        print("the newer Matter/BLE-mesh YX-WS01 builds rather than a Tuya one. In that case")
        print("pair it in Apple Home / Alexa / Google / SmartThings and drive it through the")
        print("'http' backend (any bridge that can expose an on-URL and an off-URL), or via")
        print("Home Assistant's Matter integration.")
        return 0
    print(f"\n{len(found)} device(s):\n")
    for ip, d in found.items():
        print(f"  ip={ip}")
        print(f"    gwId/device_id : {d.get('gwId')}")
        print(f"    version        : {d.get('version')}")
        print(f"    product/name   : {d.get('productKey') or d.get('name') or '?'}")
    print("\nThe device_id above plus the local_key from `python -m tinytuya wizard` are the two")
    print("values /wake/plug/config needs.")
    return 0


def _load_cfg(db_path: str) -> dict:
    from sleepctl.storage.repository import Repository
    repo = Repository(db_path)
    try:
        row = repo.conn.execute(
            "SELECT value FROM settings_kv WHERE key='wake_plug_config'").fetchone()
        return json.loads(row["value"]) if row else {}
    finally:
        repo.close()


def _test(db_path: str, on: bool) -> int:
    from sleepctl.adapters.smart_plug import switch
    cfg = _load_cfg(db_path)
    if not cfg or not cfg.get("config"):
        print("no wake-plug config stored yet -- PUT /wake/plug/config first")
        return 1
    ok = switch(cfg.get("backend") or "tuya", cfg.get("config") or {}, on)
    print(f"commanded {'ON' if on else 'OFF'} via backend={cfg.get('backend')}: "
          f"{'OK' if ok else 'FAILED'}")
    if not ok:
        print("If this is a Tuya unit, check the local_key and that the plug is on the same")
        print("2.4GHz LAN (these plugs are 2.4GHz only). If nothing answers a --scan either,")
        print("it is a Matter build -- use the 'http' backend instead.")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="sleepctl.db")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true", help="probe the LAN and report what is there")
    g.add_argument("--test-on", action="store_true", help="turn the configured plug ON now")
    g.add_argument("--test-off", action="store_true", help="turn the configured plug OFF now")
    args = p.parse_args(argv)

    if args.scan:
        return _scan()
    return _test(args.db, on=bool(args.test_on))


if __name__ == "__main__":
    sys.exit(main())
