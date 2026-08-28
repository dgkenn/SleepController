#!/usr/bin/env python3
"""Polar Verity Sense -> SleepController bridge (zero device risk to the Pod).

Reads a dedicated BLE Heart Rate sensor -- a **Polar Verity Sense** armband (or any standard
0x180D Heart Rate Service device, incl. a Polar H10 chest strap) -- and forwards its heart rate
plus beat-to-beat RR intervals to the dashboard's ``/hr/ingest`` endpoint. The API computes HRV
(RMSSD) from the RR intervals and MERGES this authoritative cardiac signal with the iPhone
accelerometer's movement (``/bcg/ingest``) into a single fused frame the controller consumes.

Two transports are supported (``--mode``):

  * ``hr``  -- the generic 0x180D Heart Rate Service (HR + RR). The long-standing production path.
  * ``pmd`` -- Polar's vendor **Polar Measurement Data** service, which additionally gives us the
    armband's OWN triaxial accelerometer (ACC) and pulse-to-pulse intervals (PPI, with a per-beat
    error estimate and blocker bit). ACC means actigraphy WITHOUT the user's iPhone, in the same
    modality/units as the PhysioNet training data.
  * ``auto`` (default) -- try PMD, degrading per stream: ACC refused -> PPI only; PPI refused ->
    ACC plus the generic HR service for HR/RR; both refused (or no PMD service) -> the generic HR
    path for that session. The log always states which streams actually started.

Two Verity quirks worth knowing when reading the log:

  * With PPI enabled the device only updates HR every ~5 s and the FIRST PPI batch takes ~25 s to
    arrive. That silence is normal; nothing here treats it as a failure or reconnects during the
    ``--pmd-grace-seconds`` window.
  * If PPI never arrives (or its start is refused), the armband may have been left in **SDK mode**
    by another app -- SDK mode disables the on-device HR/PPI algorithms. We never enable SDK mode
    and deliberately implement no opcode for it; we just detect the symptom and tell the user to
    power-cycle the armband.

In PMD mode the POST body gains an ``acc`` block of actigraphy counts computed exactly the way
``scripts/reduce_motion_activity.py`` computes the training counts::

    {"hr": 58.0, "rr": [948.0, ...], "source": "verity",
     "acc": {"pim":.., "zcm":.., "mad":.., "std":.., "pmax":.., "n":.., "fs":52,
             "gait":true, "cadence_hz":1.9, "gait_conc":0.39}}   # gait only when detected

The Verity is a SEPARATE device: nothing here ever touches, modifies, or risks the Eight Sleep
Pod. This is the physiology path that works even when the Pod's own sleep-tracking is unavailable
(e.g. no Eight Sleep membership).

Runs unattended with an auto-reconnect loop; the watchdog can launch it (set SLEEPCTL_VERITY=1 in
deploy\\.env). Run it by hand any time:

    python scripts/verity_forwarder.py                       # auto-discover a Polar sensor
    python scripts/verity_forwarder.py --scan                 # just list sensors, then exit
    python scripts/verity_forwarder.py --address AA:BB:...    # pin a specific device
    python scripts/verity_forwarder.py --url http://localhost:8000/hr/ingest --token <TOKEN>

Requires ``bleak`` (``pip install bleak``) and a Bluetooth adapter. On Windows the Verity must
first be paired/available to the OS; put the armband in HR broadcast mode (single press -> the
LED shows the Bluetooth/HR mode).
"""
from __future__ import annotations

import argparse
from collections import deque
import asyncio
import json
import re
import os
import sys
import time
import urllib.request
from pathlib import Path

try:  # running as a script puts scripts/ on sys.path; importing it as a module may not
    import polar_pmd as pmd
except ImportError:  # pragma: no cover - import-path fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import polar_pmd as pmd

# The repo root must be importable for the shared respiratory estimator -- the forwarder runs as
# a bare script from scripts/, so it is not on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sleepctl.controller import respiration  # noqa: E402

# Standard BLE Heart Rate Measurement characteristic (GATT 0x2A37).
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
# Standard BLE Battery Level characteristic (GATT 0x2A19), uint8 percent. The Verity exposes it,
# and NOT reading it cost a whole night: the band ran 25.5 h unattended and died flat at 00:01
# mid-sleep with nothing anywhere reporting how much charge was left.
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

#: Warn at/below this percent -- roughly a night's margin on a band that streams ~20 h full.
_BATTERY_WARN_PCT = 40
# Names we'll auto-match when scanning (case-insensitive substring).
_NAME_HINTS = ("polar", "verity", "sense", "h10", "oh1")


def _repo_root() -> Path:
    here = Path(__file__).resolve().parent.parent
    return here


def _load_env(root: Path) -> dict:
    """Parse deploy\\.env into a dict (same KEY=VALUE style as the PowerShell scripts). Missing
    file -> empty dict. Used only to pick up BCG_INGEST_TOKEN and a URL override; never printed."""
    out: dict[str, str] = {}
    env_path = root / "deploy" / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def _parse_hr_measurement(data: bytearray) -> tuple[int | None, list[float]]:
    """Decode a Heart Rate Measurement notification into (hr_bpm, [rr_ms, ...]) per the GATT spec.

    flags byte: bit0 = HR value format (0: uint8, 1: uint16); bit3 = Energy Expended present;
    bit4 = RR-Interval(s) present. RR intervals are uint16 little-endian in units of 1/1024 s.
    """
    if not data:
        return None, []
    flags = data[0]
    idx = 1
    hr: int | None = None
    if flags & 0x01:  # 16-bit HR
        if len(data) >= idx + 2:
            hr = int.from_bytes(data[idx:idx + 2], "little")
            idx += 2
    else:  # 8-bit HR
        if len(data) >= idx + 1:
            hr = data[idx]
            idx += 1
    if flags & 0x08:  # Energy Expended present (uint16) -> skip it
        idx += 2
    rr_ms: list[float] = []
    if flags & 0x10:  # RR intervals present
        while idx + 1 < len(data):
            raw = int.from_bytes(data[idx:idx + 2], "little")
            idx += 2
            rr_ms.append(raw * 1000.0 / 1024.0)  # 1/1024 s units -> milliseconds
    return hr, rr_ms


def _redact(url: str) -> str:
    """URL with any ``token=`` query value masked.

    The ingest URL carries BCG_INGEST_TOKEN as a query parameter, and logging it verbatim wrote
    the live token in plaintext into .run\verity.log -- a file nothing rotates, that the
    diagnostics bundle copies, and that is readable by anything running as this user.
    """
    return re.sub(r"(token=)[^&\s]+", r"<redacted>", url or "")


def _post(url: str, payload: dict, timeout: float = 5.0):
    """POST a batch and RETURN the parsed response.

    The response was previously read and thrown away, which cost a whole night: it carries the
    server's ``not_worn`` verdict, and without it this process happily held a BLE connection to a
    band sitting on its charger. See ``_NOT_WORN_RELEASE_BATCHES``.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local URL)
        raw = resp.read()
    # Counted AFTER the round-trip succeeds: this is the "did this session actually produce
    # physiology" signal the recovery ladder escalates on, so an attempt that threw must not
    # look like a productive one.
    _STATS["posts"] += 1
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


#: Consecutive not-worn batches before we RELEASE the band. At the default ~2 s batch cadence
#: this is about 10 minutes -- long enough that real motionless deep sleep never trips it (the
#: server's not-worn test additionally requires a physiologically implausible RMSSD, measured at
#: 0/5685 on real sleeping samples and 4851/4851 on a charger), short enough to let go promptly.
_NOT_WORN_RELEASE_BATCHES = 300

#: How long to stay disconnected after releasing. A Verity on its charger must be left ALONE to
#: charge; reconnecting straight away would resume the drain that caused the outage.
_NOT_WORN_BACKOFF_S = 900.0

#: Ceiling on the escalating retry backoff after repeated failed sessions (see ``_main_async``).
#: Five minutes still reconnects promptly once the band is worn again, without hammering one
#: that is charging or stuck in SDK mode.
_MAX_SESSION_BACKOFF_S = 300.0

#: Shared release state. Module-level because BOTH session paths (PMD and the generic HR
#: fallback) must be able to let go, and ``_run_once`` -- which owns the reconnect -- has to see
#: the backoff. The PMD path is the one actually used in production.
_RELEASE = {"run": 0, "until": 0.0}

#: --- redundancy layer 3: escalating RECOVERY across repeated barren sessions ----------------
#: ``fails`` in the reconnect loop only ever counted EXCEPTIONS, and a session that connects,
#: yields nothing and disconnects cleanly is not an exception -- it resets the counter. So the
#: single most common real failure (a band that accepts the link but streams nothing) looped at
#: the base retry interval forever with no escalation whatsoever. Count sessions that produced no
#: DATA instead, and escalate through qualitatively different recoveries rather than just waiting
#: longer: try the other transport, then stop trusting the cached address, then ask for the
#: Bluetooth stack itself to be reset.
_STATS = {"posts": 0, "last_seen_at": 0.0}

#: Last PMD control-point error code, so a start refused with "already in state" (6) can be
#: distinguished from a genuine refusal and recovered by stopping the stale stream first.
_PMD_LAST_ERROR: dict = {"code": None}

#: Backoff ceiling when the band WAS found this cycle but the connect failed. The escalating
#: ceiling (_MAX_SESSION_BACKOFF_S) exists to leave a band that is absent/charging alone; applying
#: it to a band that is sitting right there, advertising, and merely blocked by another central
#: means up to five minutes of not retrying after the blocker lets go. Observed 2026-08-27: ten
#: consecutive connect timeouts, each followed by a 300 s wait, while the band was visible in
#: every single scan.
_PRESENT_BACKOFF_S = 25.0

#: Consecutive barren sessions before each rung of the ladder.
_ALT_TRANSPORT_AFTER = 2     # the Verity exposes two independent streams -- try the other one
_REDISCOVER_AFTER = 3        # stop trusting a pinned/cached address; full rescan
_ADAPTER_RESET_AFTER = 5     # ask the watchdog to restart the Bluetooth stack

#: --- redundancy layer 1: a live LINK is not a live FEED --------------------------------------
#: BLE can hold a connection open long after notifications stop. Both session loops ran
#: ``while client.is_connected``, so a silent-but-connected band pinned the forwarder in a
#: session that would never produce another sample -- and because ``last_hr`` was never
#: invalidated, the flusher kept re-POSTing the SAME frozen heart rate every batch, indefinitely,
#: as though it were live. Measured 2026-08-26: 25 HR samples at 19:00 and nothing for the next
#: ten hours while the controller sat in a session it could never advance.
#:
#: Two independent guards, because they fail differently:
#:   * FRESHNESS -- a reading older than this is not sent at all. Stops a frozen value from
#:     being laundered into the pipeline as current physiology.
#:   * STALL -- no new notification for this long ends the session, which drops the link and
#:     forces a full rescan/reconnect. Recovers a wedged link instead of waiting for the OS.
#: Both are generous relative to real quiet periods: the Verity's own PPI warm-up is ~25 s and
#: it slows HR updates to ~5 s when PPI is enabled, so neither fires on normal behaviour.
_HR_MAX_AGE_S = 30.0
_STALL_TIMEOUT_S = 120.0


#: --- redundancy layer 2: process liveness, separate from data flow ---------------------------
#: The supervisor could only check that a verity_forwarder process EXISTED, which a wedged one
#: passes. This mirrors the daemon.heartbeat pattern the watchdog already trusts ("a file's mtime
#: is unambiguous"): the forwarder touches this every loop, so a stale file means WEDGED.
#:
#: Deliberately beats even while deliberately idle -- during the not-worn release backoff the
#: forwarder is doing exactly the right thing by holding off, and killing it then would restart a
#: fresh process that rescans immediately and reconnects to a band that is trying to charge
#: (the backoff lives in process memory and does not survive a restart). Liveness and data flow
#: are different questions: data freshness is already measured at the ingest side.
def _beat(root: Path) -> None:
    try:
        run_dir = root / ".run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "verity.heartbeat").write_text(
            time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="ascii")
    except Exception:
        pass


class _Freshness:
    """Monotonic 'when did real data last arrive' tracker shared by both session paths."""

    def __init__(self) -> None:
        self.t = time.monotonic()

    def note(self) -> None:
        self.t = time.monotonic()

    def age(self) -> float:
        return time.monotonic() - self.t


def _effective_mode(preferred: str, barren: int) -> str:
    """Which transport to try, given how many barren sessions we have had in a row.

    The Verity exposes TWO independent streams -- Polar's vendor PMD service (ACC + PPI) and the
    generic 0x180D Heart Rate service -- and they fail independently: PMD can be refused outright
    (a band left in SDK mode), while 0x180D keeps working, and a wedged PMD handshake can hang a
    session that plain HR would have sailed through. ``auto`` already degrades PMD->HR WITHIN one
    connection, but if the PMD handshake itself is what is wedging, every attempt wedges the same
    way. So after repeated barren sessions, alternate which stream we lead with.
    """
    if barren < _ALT_TRANSPORT_AFTER:
        return preferred
    # Alternate on each subsequent barren session so we never get stuck favouring the broken one.
    flip = ((barren - _ALT_TRANSPORT_AFTER) // _ALT_TRANSPORT_AFTER) % 2 == 0
    if preferred == "pmd":
        return "hr" if flip else "pmd"
    if preferred == "hr":
        return "pmd" if flip else "hr"
    return "hr" if flip else "pmd"      # "auto": lead with one service explicitly


def _request_adapter_reset(root: Path, barren: int) -> None:
    """Ask the watchdog to restart the Bluetooth stack (flag file, same protocol as
    update.request / restart.request).

    The forwarder cannot do this itself: restarting a system service needs rights this process
    does not have, and the watchdog already runs elevated from its Scheduled Task. Writing one
    flag file keeps the privileged action on the privileged side, and matches how every other
    escalated action in this system is requested.
    """
    try:
        run_dir = root / ".run"
        run_dir.mkdir(parents=True, exist_ok=True)
        flag = run_dir / "bt-reset.request"
        if flag.exists():
            return          # one pending request is enough; the watchdog rate-limits the rest
        flag.write_text(f"barren_sessions={barren}", encoding="ascii")
        _log(f"{barren} barren sessions -- requesting a Bluetooth adapter reset")
    except Exception:
        pass


def _note_worn_state(resp) -> bool:
    """Track consecutive not-worn verdicts; True means RELEASE the band now."""
    if resp is None:
        return False
    if not resp.get("not_worn"):
        _RELEASE["run"] = 0
        return False
    _RELEASE["run"] += 1
    if _RELEASE["run"] < _NOT_WORN_RELEASE_BATCHES:
        return False
    _log(f"not worn for {_RELEASE['run']} consecutive batches -- releasing the band so it can "
         f"idle/charge; reconnecting in {_NOT_WORN_BACKOFF_S / 60:.0f} min")
    _RELEASE["until"] = time.monotonic() + _NOT_WORN_BACKOFF_S
    _RELEASE["run"] = 0
    return True


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


# Repeated-identical-failure throttle. This process runs unattended all night at a ~2s POST
# cadence, so a PERSISTENT failure -- a missing ingest token 401ing every batch is the likely one
# -- writes ~14k identical lines a night into .run\verity.log, which nothing rotates. Left running
# that is tens of MB a month onto the disk that also holds the SQLite DB, and a full disk fails
# WRITES while deletes still succeed, so it presents as the controller mysteriously losing data
# rather than as a disk problem. Log the first occurrence, then back off geometrically, always
# reporting the true count so the reduced volume never hides the severity.
_repeat_state: dict = {"key": None, "count": 0, "next_at": 1}


def _log_repeating(key: str, msg: str) -> None:
    """Log ``msg`` for a condition identified by ``key``, throttled while it keeps repeating."""
    st = _repeat_state
    if st["key"] != key:
        if st["key"] is not None and st["count"] > st.get("last_reported", 0):
            _log(f"(previous condition '{st['key']}' ended after {st['count']} occurrences)")
        st.update({"key": key, "count": 0, "next_at": 1, "last_reported": 0})
    st["count"] += 1
    if st["count"] >= st["next_at"]:
        suffix = f" [x{st['count']}]" if st["count"] > 1 else ""
        _log(msg + suffix)
        st["last_reported"] = st["count"]
        st["next_at"] = max(2, st["count"] * 4)


def _reset_repeat_log() -> None:
    """Clear the throttle after a success, so the next failure is reported immediately."""
    if _repeat_state["key"] is not None:
        _repeat_state.update({"key": None, "count": 0, "next_at": 1, "last_reported": 0})


def _adv_uuids(device) -> list[str]:
    """Service UUIDs a scan result advertised (empty if the bleak backend doesn't expose them)."""
    try:
        return [u.lower() for u in (device.metadata.get("uuids") or [])]
    except Exception:
        return []



def _addr_file(root: Path) -> Path:
    return root / ".run" / "verity.address"


def _remember_address(addr: str) -> None:
    """Persist an address we have actually connected to, so a later scan failure is survivable."""
    try:
        f = _addr_file(_repo_root())
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(addr), encoding="ascii")
    except Exception:
        pass


def _recall_address() -> str | None:
    try:
        return (_addr_file(_repo_root()).read_text(encoding="ascii").strip() or None)
    except Exception:
        return None


async def _discover(BleakScanner, address_hint: str | None):
    if address_hint:
        return address_hint
    # 20s, not 10. Advertising intervals are long, Windows' scanner is bursty, and a band that a
    # phone keeps grabbing only surfaces in SOME windows -- a short scan turns an intermittently
    # visible device into a permanently invisible one.
    _log("scanning for a Polar/BLE heart-rate sensor (20s)...")
    devices = await BleakScanner.discover(timeout=20.0)
    for d in devices:
        name = (d.name or "").lower()
        if any(h in name for h in _NAME_HINTS):
            _log(f"found '{d.name}' at {d.address}")
            return d.address
    # Fall back to any device advertising the HR service, if the backend exposes it.
    for d in devices:
        if any("180d" in u for u in _adv_uuids(d)):
            _log(f"found HR-service device '{d.name}' at {d.address}")
            return d.address

    # Nothing matched. Say WHAT WAS THERE -- "no sensor found" alone cannot distinguish "the band
    # is off" from "the band is present but we failed to match it", and those need opposite fixes.
    seen = [f"{(d.name or '?')}@{d.address}" for d in devices]
    _log(f"no match among {len(devices)} advertising device(s): "
         + (", ".join(seen[:12]) if seen else "<none advertising at all>"))

    # LAST RESORT: try an address we have connected to before, even though it is not advertising.
    #
    # A BLE peripheral that is already connected to another central generally STOPS advertising,
    # so it is invisible to a scan while remaining perfectly healthy and connectable -- which is
    # exactly what a phone running Polar Flow does to it. On Windows a bonded device can often
    # still be opened directly by address through the OS's cached record, so a scan miss must not
    # be treated as "no device". Refusing to try is how a band that pairs fine with a phone looks
    # identical to a flat one.
    remembered = _recall_address()
    if remembered:
        _log(f"scan found nothing; trying the last known address {remembered} directly "
             f"(a band connected to another app stops advertising but stays connectable)")
        return remembered
    return None


async def _scan_report(seconds: float) -> None:
    """--scan: list candidate sensors and exit. Never connects, never streams."""
    from bleak import BleakScanner  # lazy: only needed at runtime

    _log(f"scanning for BLE devices ({seconds:.0f}s)...")
    devices = await BleakScanner.discover(timeout=seconds)

    matches = []
    for d in devices:
        hr_service = any("180d" in u for u in _adv_uuids(d))
        if hr_service or any(h in (d.name or "").lower() for h in _NAME_HINTS):
            matches.append((d, hr_service))

    if matches:
        _log(f"{len(matches)} candidate heart-rate sensor(s):")
        for d, hr_service in matches:
            print(f"    {d.address}    {d.name or '(unnamed)'}"
                  f"{'  [0x180D heart-rate service]' if hr_service else ''}", flush=True)
        _log("pin one with:  python scripts/verity_forwarder.py --address <ADDRESS>")
    else:
        _log("no Polar/heart-rate sensor found.")
        _log("hint: put the Verity in HR mode (single press -> blue LED) and keep it awake, "
             "on the arm and close to this machine, then scan again.")
        if devices:
            _log(f"all {len(devices)} BLE devices seen this scan:")
            for d in devices:
                print(f"    {d.address}    {d.name or '(unnamed)'}", flush=True)
        else:
            _log("no BLE devices at all were seen -- check the Bluetooth adapter is present, "
                 "enabled, and not claimed by another app.")
    _log("note: if the armband connects but never produces HR/PPI -- " + pmd.SDK_MODE_REMEDY)


def _scan_main(args) -> int:
    """--scan entry point. Always exits 0: this is a diagnostic step in the setup script, so a
    missing dependency or absent adapter must print advice, not a traceback or a hard failure."""
    try:
        import bleak  # noqa: F401
    except Exception:
        _log("'bleak' is not installed, so no scan is possible. Run:  pip install bleak")
        return 0
    try:
        asyncio.run(_scan_report(args.scan_seconds))
    except KeyboardInterrupt:
        _log("scan stopped")
    except Exception as exc:
        _log(f"scan failed ({exc}); is a Bluetooth adapter present and enabled?")
    return 0


async def _hr_session(client, args) -> None:
    """Generic 0x180D Heart Rate Service path (the long-standing production behaviour)."""
    # Coalesce notifications into small batches so we POST a few times a second, not per-beat.
    batch_rr: list[float] = []
    last_hr: dict = {"v": None, "t": 0.0}
    last_flush = {"t": time.monotonic()}
    fresh = _Freshness()

    def _on_hr(_handle, data: bytearray) -> None:
        hr, rr = _parse_hr_measurement(data)
        if hr is not None:
            last_hr["v"] = hr
            last_hr["t"] = time.monotonic()
            fresh.note()
        if rr:
            batch_rr.extend(rr)
            fresh.note()

    async def _flusher(client) -> None:
        while client.is_connected:
            await asyncio.sleep(args.batch_seconds)
            _beat(_repo_root())
            # Layer 1a: never forward a stale reading. Without this the last value seen was
            # re-sent every batch forever once notifications stopped.
            hr = last_hr["v"]
            hr_age = time.monotonic() - (last_hr.get("t") or 0.0)
            if hr is not None and hr_age > getattr(args, "hr_max_age", _HR_MAX_AGE_S):
                hr = None
            rr = batch_rr[:]
            batch_rr.clear()
            # Layer 1b: a live link with a dead feed must END the session so the reconnect loop
            # can rescan, rather than sitting here until something else notices.
            if fresh.age() > getattr(args, "stall_seconds", _STALL_TIMEOUT_S):
                _log(f"no sensor data for {fresh.age():.0f}s while connected -- "
                     f"dropping the link to force a reconnect")
                return
            if hr is None and not rr:
                continue
            payload = {"source": args.source}
            if hr is not None:
                payload["hr"] = float(hr)
            if rr:
                payload["rr"] = rr
            try:
                resp = _post(args.url, payload)
                last_flush["t"] = time.monotonic()
                _reset_repeat_log()
                if _note_worn_state(resp):
                    return          # ends the flusher -> disconnect -> backoff before rescan
            except Exception as exc:  # network blip -> drop this batch, keep streaming
                _log_repeating(f"post:{type(exc).__name__}",
                               f"POST failed ({exc}); dropping batch")

    _log(f"subscribing to HR notifications; forwarding to {_redact(args.url)}")
    await client.start_notify(HR_MEASUREMENT_UUID, _on_hr)
    try:
        await _flusher(client)
    finally:
        try:
            await client.stop_notify(HR_MEASUREMENT_UUID)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# Polar PMD path (vendor service: ACC + PPI)
# --------------------------------------------------------------------------------------
async def _pmd_service_present(client) -> bool:
    """True if the device advertises Polar's PMD service. Unknown -> True (let START decide)."""
    services = None
    try:
        services = client.services
    except Exception:
        services = None
    if services is None:
        try:
            services = await client.get_services()  # bleak < 0.21
        except Exception:
            return True
    try:
        uuids = {str(s.uuid).lower() for s in services}
    except Exception:
        return True
    if not uuids:
        return True
    return pmd.PMD_SERVICE_UUID in uuids


async def _pmd_command(client, responses: "asyncio.Queue", cmd: bytes, what: str,
                       timeout: float) -> dict | None:
    """Write a control-point command and wait for its indication. None -> failed (already logged)."""
    # Cleared per command: the stale-stream retry keys off this, and a code left over from an
    # EARLIER stream would make an unrelated later failure (a timeout, a write error) look like
    # "already running" and trigger a stop/start against a stream that was never started.
    _PMD_LAST_ERROR["code"] = None
    while not responses.empty():
        try:
            responses.get_nowait()
        except Exception:
            break
    try:
        await client.write_gatt_char(pmd.PMD_CONTROL_UUID, cmd, response=True)
    except Exception as exc:
        _log(f"PMD {what}: write failed ({exc})")
        return None
    try:
        raw = await asyncio.wait_for(responses.get(), timeout=timeout)
    except asyncio.TimeoutError:
        _log(f"PMD {what}: no control-point response within {timeout}s")
        return None
    try:
        resp = pmd.parse_control_response(raw)
    except Exception as exc:
        _log(f"PMD {what}: unparseable response ({exc})")
        return None
    if not resp["ok"]:
        _log(f"PMD {what}: device refused ({resp['error']}, code {resp['error_code']})")
        _PMD_LAST_ERROR["code"] = resp.get("error_code")
        # The band tells us outright when it is CHARGING (PMD error code 13). That is the most
        # precise "leave me alone" signal available -- better than inferring not-worn from the
        # data -- and it is exactly the state we must not hold a connection through, because
        # doing so is what kept the Verity awake on its charger until the battery died mid-night.
        if resp.get("error_code") == pmd.ERROR_DEVICE_IN_CHARGER:
            _RELEASE["until"] = time.monotonic() + _NOT_WORN_BACKOFF_S
            _log(f"device reports it is IN THE CHARGER -- releasing it for "
                 f"{_NOT_WORN_BACKOFF_S / 60:.0f} min so it can actually charge")
        return None
    return resp


async def _read_battery(client) -> "int | None":
    """Battery percent from the standard BLE Battery Service, or None if unreadable.

    Best-effort by design: a band that will not report battery must still be allowed to stream.
    """
    try:
        raw = await client.read_gatt_char(BATTERY_LEVEL_UUID)
        if raw:
            pct = int(raw[0])
            if 0 <= pct <= 100:
                return pct
    except Exception:
        pass
    return None


async def _report_battery(client, args) -> "int | None":
    """Read, log and forward the band's battery level once per session."""
    pct = await _read_battery(client)
    if pct is None:
        _log("battery: not reported by this device")
        return None
    if pct <= _BATTERY_WARN_PCT:
        _log(f"battery: {pct}% -- LOW. A full night needs roughly a full charge; the band died "
             f"mid-sleep on 2026-08-06 after 25.5 h of continuous streaming.")
    else:
        _log(f"battery: {pct}%")
    try:
        _post(args.url, {"source": args.source, "battery_pct": pct})
    except Exception:
        pass        # telemetry only; never let it stop the stream starting
    return pct


async def _pmd_session(client, args) -> bool:
    """Stream ACC + PPI over Polar's PMD service, degrading per stream.

    Each stream is started independently, so one refusal never costs us the other:
      * PPI ok, ACC refused  -> PPI only (cardiac as today, no on-device actigraphy).
      * ACC ok, PPI refused  -> ACC plus the generic 0x180D HR service for HR/RR if it exists.
      * both refused / no PMD service -> return False so the caller uses the generic HR path.

    Returns True if at least one PMD stream started (the coroutine then runs until disconnect).
    """
    if not await _pmd_service_present(client):
        _log("PMD service not present on this device")
        return False

    responses: asyncio.Queue = asyncio.Queue()

    batch_rr: list[float] = []
    acc_mags: list[float] = []
    last_hr: dict = {"v": None, "t": 0.0}
    fresh = _Freshness()
    stats = {"blocked": 0, "bad_frames": 0}
    frames = {"acc": 0, "ppi": 0}
    acc_cap = max(int(args.acc_rate * 300), 1000)  # ~5 min of samples; bounds memory if POSTs fail
    # SEPARATE rolling window for accelerometer-derived respiration. The per-batch `acc_mags`
    # above is cleared every --batch-seconds (~2 s = ~104 samples), which is nowhere near enough
    # to resolve a 0.15-0.40 Hz breathing rhythm -- that needs minutes. Breathing physically
    # moves the body, so a worn accelerometer carries the same rhythm as RSA does in the RR
    # intervals, but as an INDEPENDENT measurement that fails differently: RSA collapses under
    # sympathetic arousal, accelerometry collapses under gross movement. Two disagreeing sensors
    # is a far better signal than one confident one.
    resp_win = int(args.acc_rate * respiration.DEFAULT_WINDOW_S)
    acc_resp_buf: "deque[float]" = deque(maxlen=resp_win)
    # Short, separate buffer for the deliberate MARKER GESTURE. It must not share the respiration
    # window: a 3 s shake inside a multi-minute buffer is diluted below every threshold that
    # would detect it.
    marker_buf: "deque[float]" = deque(maxlen=int(args.acc_rate * 4.0))
    marker_last = {"t": 0.0}

    def _on_control(_handle, data: bytearray) -> None:
        try:
            responses.put_nowait(bytes(data))
        except Exception:
            pass

    def _on_data(_handle, data: bytearray) -> None:
        try:
            mtype = pmd.frame_measurement_type(data)
            if mtype == pmd.MEAS_ACC:
                _ts, _ft, samples = pmd.parse_acc_frame(data)
                frames["acc"] += 1
                _mags = pmd.acc_magnitudes_g(samples)
                acc_mags.extend(_mags)
                acc_resp_buf.extend(_mags)
                marker_buf.extend(_mags)
                if len(acc_mags) > acc_cap:
                    del acc_mags[:len(acc_mags) - acc_cap]
            elif mtype == pmd.MEAS_PPI:
                _ts, samples = pmd.parse_ppi_frame(data)
                frames["ppi"] += 1
                for s in samples:
                    if s["hr"]:
                        last_hr["v"] = s["hr"]
                        last_hr["t"] = time.monotonic()
                        fresh.note()
                    if s["ok"]:
                        batch_rr.append(float(s["ppi_ms"]))
                    else:
                        stats["blocked"] += 1
        except Exception as exc:  # malformed frame -> log sparsely, never break the stream
            stats["bad_frames"] += 1
            if stats["bad_frames"] <= 5 or stats["bad_frames"] % 100 == 0:
                _log(f"PMD: dropping malformed frame ({exc})")

    def _on_hr(_handle, data: bytearray) -> None:  # generic 0x180D, only used if PPI is refused
        try:
            hr, rr = _parse_hr_measurement(data)
            if hr is not None:
                last_hr["v"] = hr
                last_hr["t"] = time.monotonic()
                fresh.note()
            if rr:
                batch_rr.extend(rr)
        except Exception as exc:
            stats["bad_frames"] += 1
            if stats["bad_frames"] <= 5:
                _log(f"HR: dropping malformed notification ({exc})")

    await client.start_notify(pmd.PMD_CONTROL_UUID, _on_control)
    started: list[int] = []
    data_notify = False
    hr_notify = False
    try:
        await client.start_notify(pmd.PMD_DATA_UUID, _on_data)
        data_notify = True

        # PPI takes no settings (02 03). ACC is configurable; defaults 52 Hz / 16-bit / 8 G.
        # CHANNELS is REQUIRED: real Verity Sense firmware refuses an ACC start that omits it
        # with "invalid number of channels" (error code 11) -- verified live on hardware. The
        # standalone scripts/verity_stream_test.py always sent it and streamed ACC fine, which
        # is what exposed that this forwarder (the production path) did not.
        acc_settings = {
            pmd.SETTING_RANGE: args.acc_range,
            pmd.SETTING_SAMPLE_RATE: args.acc_rate,
            pmd.SETTING_RESOLUTION: args.acc_resolution,
            pmd.SETTING_CHANNELS: 3,
        }
        wanted = [
            (pmd.MEAS_PPI, "start PPI", pmd.build_start_command(pmd.MEAS_PPI, None)),
            (pmd.MEAS_ACC, f"start ACC @{args.acc_rate}Hz/{args.acc_resolution}bit/{args.acc_range}G",
             pmd.build_start_command(pmd.MEAS_ACC, acc_settings)),
        ]
        for meas_type, what, cmd in wanted:
            resp = await _pmd_command(client, responses, cmd, what, args.control_timeout)

            # "already in state" (code 6) means the band still believes this stream is RUNNING
            # from a previous session -- which is what an unclean disconnect leaves behind. The
            # forwarder is killed and relaunched whenever its code changes or the watchdog
            # restarts, so the streams are not always stopped on the way out, and the very next
            # session is then refused ACC and PPI and silently degrades to HR-only. Observed
            # 2026-08-28 22:12: a deploy restarted the forwarder mid-stream and the reconnect
            # lost the accelerometer -- the single best wake signal (6/6 vs 2/6) -- for the rest
            # of the night, with nothing but one log line to say so.
            #
            # This is recoverable and worth recovering: STOP the stale stream, then start it
            # again. Only ever attempted once, and only for this specific code, so a genuinely
            # refused stream still degrades instead of looping.
            if resp is None and _PMD_LAST_ERROR.get("code") == pmd.ERROR_ALREADY_IN_STATE:
                _log(f"PMD: {what} refused as already-running (stale state from an unclean "
                     f"disconnect) -- stopping it and retrying once")
                await _pmd_command(client, responses, pmd.build_stop_command(meas_type),
                                   f"stop {what}", args.control_timeout)
                resp = await _pmd_command(client, responses, cmd, what, args.control_timeout)

            if resp is None:
                _log(f"PMD: {what} FAILED; continuing without it")
                # The SDK-mode hint is only meaningful for an UNEXPLAINED PPI refusal. Printing it
                # after an "already in state" (code 6) refusal actively misleads: it tells the user
                # to power-cycle the armband when the real cause is a stale stream from our own
                # unclean disconnect, which the retry above already handles. A remedy aimed at the
                # wrong cause is worse than none -- it sends someone to do the one thing that
                # cannot help.
                if (meas_type == pmd.MEAS_PPI
                        and _PMD_LAST_ERROR.get("code") != pmd.ERROR_ALREADY_IN_STATE):
                    # A refused PPI start is one of the two SDK-mode symptoms; say so up front.
                    _log("PMD: " + pmd.SDK_MODE_REMEDY)
                continue
            started.append(meas_type)
            _log(f"PMD: {what} ok")

        if not started:
            return False  # caller falls back entirely to the generic HR service

        ppi_running = pmd.MEAS_PPI in started
        sources = []
        if pmd.MEAS_ACC in started:
            sources.append(f"ACC@{args.acc_rate}Hz")
        if ppi_running:
            sources.append("PPI")
        else:
            # No PPI -> try the generic HR service so we still have a cardiac signal.
            try:
                await client.start_notify(HR_MEASUREMENT_UUID, _on_hr)
                hr_notify = True
                sources.append("HR/RR (generic 0x180D)")
            except Exception as exc:
                _log(f"PMD: generic HR service unavailable too ({exc}); ACC only, no heart rate")
        _log(f"PMD: streaming {' + '.join(sources)}; forwarding to {_redact(args.url)}")
        if ppi_running:
            _log(f"PMD: PPI warm-up -- Polar documents ~{pmd.PPI_FIRST_SAMPLE_S:.0f}s to the first "
                 f"batch and HR updates only every ~{pmd.PPI_HR_UPDATE_S:.0f}s; silence until then "
                 "is normal")

        t0 = time.monotonic()
        warned: set = set()
        while client.is_connected:
            await asyncio.sleep(args.batch_seconds)
            _beat(_repo_root())
            hr = last_hr["v"]
            # Layer 1a: a reading older than hr_max_age is not current physiology -- do not
            # forward it. Otherwise a frozen value is re-POSTed every batch as though live.
            if hr is not None and (time.monotonic() - (last_hr.get("t") or 0.0)) > getattr(args, "hr_max_age", _HR_MAX_AGE_S):
                hr = None
            rr = batch_rr[:]
            batch_rr.clear()
            mags = acc_mags[:]
            acc_mags.clear()
            if mags:
                fresh.note()
            # Layer 1b: end a connected-but-silent session so the reconnect loop can rescan.
            # Suppressed during the documented PPI warm-up, which is legitimately quiet.
            if (fresh.age() > getattr(args, "stall_seconds", _STALL_TIMEOUT_S)
                    and (time.monotonic() - t0) > args.pmd_grace_seconds):
                _log(f"PMD: no sensor data for {fresh.age():.0f}s while connected -- "
                     f"dropping the link to force a reconnect")
                return False

            payload: dict = {"source": args.source}
            if hr is not None:
                payload["hr"] = float(hr)
            if rr:
                payload["rr"] = rr
            if len(mags) >= pmd.MIN_EPOCH_SAMPLES:
                counts = pmd.actigraphy_counts(mags)
                counts["fs"] = args.acc_rate
                # Accelerometer-derived respiration over the ROLLING window (not this 2 s
                # batch). None whenever it is not confidently measurable -- the same quality
                # gates the RR path uses, so a movement burst or a flat spectrum yields nothing
                # rather than a fabricated rate.
                # DELIBERATE MARKER GESTURE -- the user shaking their arm to declare "I am awake
                # right now". Every other anchor we have is INFERRED; this one is declared, which
                # is what the validation layer has never had. Debounced so one shake produces one
                # marker rather than one per batch.
                if len(marker_buf) >= int(pmd.MARKER_MIN_BURST_S * args.acc_rate):
                    try:
                        mk = pmd.marker_gesture(list(marker_buf), float(args.acc_rate))
                        if mk.get("marker") and (time.monotonic() - marker_last["t"]) > 20.0:
                            marker_last["t"] = time.monotonic()
                            counts["marker"] = True
                            counts["marker_hz"] = mk.get("freq_hz")
                            marker_buf.clear()
                            _log(f"MARKER gesture detected ({mk.get('freq_hz')} Hz) -- "
                                 f"logging an awake anchor")
                    except Exception:
                        pass

                # GAIT, over the rolling window. Every other motion feature we send is an
                # AMPLITUDE measure, and amplitude cannot separate a big postural turn in bed
                # from walking across the room -- measured, a turn registers LARGER than a walk.
                # Cadence separates them, and because nothing in the wake detector reads
                # periodicity, it is usable as INDEPENDENT evidence of being awake and up, which
                # is exactly what the validation layer has been missing.
                if len(acc_resp_buf) >= int(pmd.GAIT_MIN_WINDOW_S * args.acc_rate):
                    try:
                        gait = pmd.locomotion_features(list(acc_resp_buf), float(args.acc_rate))
                        if gait.get("gait"):
                            counts["gait"] = True
                            counts["cadence_hz"] = gait.get("cadence_hz")
                            counts["gait_conc"] = gait.get("concentration")
                    except Exception:
                        pass
                if len(acc_resp_buf) >= resp_win // 2:
                    try:
                        est = respiration.estimate_uniform(list(acc_resp_buf),
                                                           float(args.acc_rate))
                        if est is not None:
                            counts["resp_brpm"] = round(est.breaths_per_min, 2)
                            counts["resp_conc"] = round(est.concentration, 3)
                    except Exception:
                        pass    # telemetry extra must never break the forwarder
                payload["acc"] = counts
            if len(payload) > 1:  # more than the source tag -> something worth sending
                try:
                    resp = _post(args.url, payload)
                    _reset_repeat_log()
                    # Let go of a band that is not on a body -- see _note_worn_state. Holding the
                    # link keeps the Verity streaming, so one left on its charger never charges.
                    if _note_worn_state(resp):
                        return True
                except Exception as exc:  # network blip -> drop this batch, keep streaming
                    _log_repeating(f"post:{type(exc).__name__}",
                                   f"POST failed ({exc}); dropping batch")

            # Quiet PPI is NOT a failure during the documented warm-up: never reconnect on it,
            # and warn at most once per state so the log doesn't fill up while we wait.
            if ppi_running:
                elapsed = time.monotonic() - t0
                state = pmd.warmup_state(elapsed, frames["ppi"], args.pmd_grace_seconds)
                if state not in warned:
                    warned.add(state)
                    if state == "stalled":
                        _log(f"PMD: no PPI yet after {elapsed:.0f}s; still waiting (warm-up is "
                             f"~{pmd.PPI_FIRST_SAMPLE_S:.0f}s)")
                    elif state == "sdk_mode_suspect":
                        _log("PMD: " + pmd.SDK_MODE_HINT.format(seconds=elapsed))
                    elif state == "streaming" and "stalled" in warned:
                        _log(f"PMD: PPI data arrived after {elapsed:.0f}s")
        return True
    finally:
        if hr_notify:
            try:
                await client.stop_notify(HR_MEASUREMENT_UUID)
            except Exception:
                pass
        for meas_type in reversed(started):
            try:
                await client.write_gatt_char(pmd.PMD_CONTROL_UUID,
                                             pmd.build_stop_command(meas_type), response=True)
            except Exception:
                pass
        if data_notify:
            try:
                await client.stop_notify(pmd.PMD_DATA_UUID)
            except Exception:
                pass
        try:
            await client.stop_notify(pmd.PMD_CONTROL_UUID)
        except Exception:
            pass
        if stats["blocked"] or stats["bad_frames"]:
            _log(f"PMD: {stats['blocked']} blocked/implausible PPI, "
                 f"{stats['bad_frames']} malformed frames this session")


async def _run_once(args, env) -> None:
    from bleak import BleakClient, BleakScanner  # lazy: only needed at runtime

    # Honour a release backoff BEFORE scanning. Reconnecting immediately would put the band
    # straight back into streaming and undo the whole point of letting go.
    remaining = _RELEASE["until"] - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(min(remaining, 60.0))
        return

    address = await _discover(BleakScanner, args.address)
    if not address:
        _log("no Polar/HR sensor found this scan; will retry")
        await asyncio.sleep(args.retry_seconds)
        return

    _log(f"connecting to {address} ...")
    # Distinguish "the band is not here" from "the band is here but the connect is being
    # refused/blocked". They deserve opposite backoffs: leave an absent (charging) band alone,
    # but retry a present one promptly -- whatever is holding it can let go at any moment.
    _STATS["last_seen_at"] = time.monotonic()
    # Do NOT trust Windows' cached GATT services. The Verity exposes different services per
    # mode, and WinRT caches the service list per device -- so after a power-cycle into a
    # different mode the cache can still describe the OLD one. Observed 2026-08-06: a band that
    # had been streaming full PMD (ACC + PPI) minutes earlier came back as "PMD service not
    # present on this device", silently costing the accelerometer -- which is what the
    # actigraphy wake signal runs on (6/6 against labelled awakenings, vs 2/6 for HR-only).
    # Forcing rediscovery costs one connection round-trip and makes the mode we actually get
    # match the mode the band is actually in.
    # EXPLICIT connect timeout. Bleak's default varies by backend and the WinRT path was taking
    # ~31s to give up, which then triggered the escalating session backoff -- so one blocked
    # connect cost five minutes of not even trying again. Fail fast, retry soon.
    async with BleakClient(address, timeout=float(getattr(args, "connect_timeout", 20.0)),
                           winrt={"use_cached_services": False}) as client:
        _log("connected")
        # Only remembered once a connection actually OPENED, so we never cache a bad guess.
        # This is what makes the scan-miss fallback above work at all.
        _remember_address(address)
        await _report_battery(client, args)
        if args.mode in ("pmd", "auto"):
            ok = False
            try:
                ok = await _pmd_session(client, args)
            except Exception as exc:
                _log(f"PMD session error ({type(exc).__name__}: {exc or '<no message>'})")
            if ok:
                _log("disconnected")
                return
            if args.mode == "pmd":
                _log(f"PMD unavailable on this device; retrying in {args.retry_seconds}s")
                await asyncio.sleep(args.retry_seconds)
                return
            _log("falling back to the generic HR service")
        await _hr_session(client, args)
    _log("disconnected")


async def _main_async(args, env) -> None:
    # ESCALATING BACKOFF on repeated failed sessions. The not-worn release only fires on POSTed
    # batches, so it cannot help when the band yields NO data at all -- which is exactly what a
    # Verity sitting on its charger does: it accepts the connection, refuses the PMD ACC stream,
    # and does not even expose the 0x180D HR characteristic. Retrying that every 10 s is a
    # connect storm against a device we are specifically trying to leave alone to charge, and it
    # keeps waking the radio. Observed 2026-08-06 while the band was charging after the battery
    # death this same loop caused.
    fails = 0
    barren = 0                 # consecutive sessions that produced NO data (see _STATS)
    preferred_mode = args.mode
    pinned_address = args.address
    while True:
        try:
            _beat(_repo_root())
            # Escalate through qualitatively different recoveries, not just a longer wait.
            args.mode = _effective_mode(preferred_mode, barren)
            if barren >= _REDISCOVER_AFTER:
                # Stop trusting a pinned/cached address. A stale address (band re-paired, or the
                # OS handing back a cached entry for a device now in a different mode) makes
                # every attempt fail identically no matter how long we wait between them.
                if args.address:
                    _log(f"{barren} barren sessions -- ignoring the pinned address and rescanning")
                args.address = None
            else:
                args.address = pinned_address
            if barren >= _ADAPTER_RESET_AFTER:
                _request_adapter_reset(_repo_root(), barren)

            before = _STATS["posts"]
            await _run_once(args, env)
            if _STATS["posts"] > before:
                if barren:
                    _log(f"recovered after {barren} barren session(s)")
                barren = 0
            else:
                barren += 1
                _log(f"session produced no data (barren streak: {barren}, "
                     f"next transport: {_effective_mode(preferred_mode, barren)})")
            fails = 0
        except Exception as exc:
            fails += 1
            barren += 1
            delay = min(args.retry_seconds * (2 ** min(fails - 1, 5)), _MAX_SESSION_BACKOFF_S)
            # If we saw the band this cycle, it is present and something transient is in the way.
            if (time.monotonic() - float(_STATS.get("last_seen_at") or 0.0)) < 120.0:
                delay = min(delay, _PRESENT_BACKOFF_S)
            # ALWAYS include the exception TYPE. asyncio.TimeoutError (and several bleak
            # errors) have an empty str(), so the old "session error ()" was literally
            # information-free -- it hid a connect timeout for 26 hours while looking like a
            # logged failure. A diagnostic that cannot distinguish a timeout from a refusal is
            # worse than none, because it looks like it is telling you something.
            _log(f"session error ({type(exc).__name__}: {exc or '<no message>'}); "
                 f"reconnecting in {delay:.0f}s (consecutive failures: {fails})")
            await asyncio.sleep(delay)


def main(argv=None) -> int:
    root = _repo_root()
    env = _load_env(root)
    token = os.environ.get("BCG_INGEST_TOKEN") or env.get("BCG_INGEST_TOKEN", "")

    p = argparse.ArgumentParser(description="Forward a Polar Verity Sense (BLE HR) to /hr/ingest")
    p.add_argument("--address", default=os.environ.get("SLEEPCTL_VERITY_ADDRESS"),
                   help="BLE MAC/address of the sensor (skip auto-discovery)")
    p.add_argument("--url", default=None, help="ingest URL (default localhost API + token)")
    p.add_argument("--token", default=token, help="BCG_INGEST_TOKEN (defaults from env/deploy\\.env)")
    p.add_argument("--scan", action="store_true",
                   help="list nearby Polar/heart-rate sensors and exit (no connection, no stream)")
    p.add_argument("--scan-seconds", type=float, default=10.0, help="--scan duration")
    p.add_argument("--source", default="verity", help="source tag stored with the samples")
    p.add_argument("--batch-seconds", type=float, default=2.0, help="POST cadence")
    p.add_argument("--retry-seconds", type=float, default=10.0, help="reconnect backoff")
    p.add_argument("--connect-timeout", type=float, default=20.0,
                   help="seconds to wait for the BLE connect before giving up and retrying")
    p.add_argument("--hr-max-age", type=float, default=_HR_MAX_AGE_S,
                   help="do not forward a heart rate older than this many seconds (a frozen "
                        "reading must never be laundered into the pipeline as live physiology)")
    p.add_argument("--stall-seconds", type=float, default=_STALL_TIMEOUT_S,
                   help="end the session if no sensor data arrives for this long while the BLE "
                        "link is still up, forcing a full rescan/reconnect")
    p.add_argument("--mode", choices=("hr", "pmd", "auto"), default="auto",
                   help="hr: generic 0x180D only; pmd: Polar PMD (ACC+PPI) only; "
                        "auto: try PMD, fall back to the HR service (default)")
    p.add_argument("--acc-rate", type=int, default=52, help="PMD accelerometer sample rate (Hz)")
    p.add_argument("--acc-range", type=int, default=8, help="PMD accelerometer range (G)")
    p.add_argument("--acc-resolution", type=int, default=16, help="PMD accelerometer resolution (bits)")
    p.add_argument("--control-timeout", type=float, default=5.0,
                   help="seconds to wait for a PMD control-point response")
    p.add_argument("--pmd-grace-seconds", type=float, default=pmd.PMD_STARTUP_GRACE_S,
                   help="quiet period allowed after starting PPI before warning "
                        "(Polar: ~25s to the first PPI batch)")
    args = p.parse_args(argv)

    if args.scan:
        return _scan_main(args)

    if not args.url:
        base = os.environ.get("SLEEPCTL_HR_URL", "http://localhost:8000/hr/ingest")
        args.url = base + (f"?token={args.token}" if args.token else "")

    try:
        import bleak  # noqa: F401
    except Exception:
        _log("ERROR: 'bleak' is not installed. Run:  pip install bleak")
        return 2

    _log(f"Polar Verity forwarder starting (source={args.source}, mode={args.mode})")
    try:
        asyncio.run(_main_async(args, env))
    except KeyboardInterrupt:
        _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
