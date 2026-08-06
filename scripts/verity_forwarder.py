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
     "acc": {"pim":.., "zcm":.., "mad":.., "std":.., "pmax":.., "n":.., "fs":52}}

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

#: Shared release state. Module-level because BOTH session paths (PMD and the generic HR
#: fallback) must be able to let go, and ``_run_once`` -- which owns the reconnect -- has to see
#: the backoff. The PMD path is the one actually used in production.
_RELEASE = {"run": 0, "until": 0.0}


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


async def _discover(BleakScanner, address_hint: str | None):
    if address_hint:
        return address_hint
    _log("scanning for a Polar/BLE heart-rate sensor (10s)...")
    devices = await BleakScanner.discover(timeout=10.0)
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
    last_hr: dict = {"v": None}
    last_flush = {"t": time.monotonic()}

    def _on_hr(_handle, data: bytearray) -> None:
        hr, rr = _parse_hr_measurement(data)
        if hr is not None:
            last_hr["v"] = hr
        if rr:
            batch_rr.extend(rr)

    async def _flusher(client) -> None:
        while client.is_connected:
            await asyncio.sleep(args.batch_seconds)
            hr = last_hr["v"]
            rr = batch_rr[:]
            batch_rr.clear()
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

    _log(f"subscribing to HR notifications; forwarding to {args.url}")
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
        return None
    return resp


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
    last_hr: dict = {"v": None}
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
                if len(acc_mags) > acc_cap:
                    del acc_mags[:len(acc_mags) - acc_cap]
            elif mtype == pmd.MEAS_PPI:
                _ts, samples = pmd.parse_ppi_frame(data)
                frames["ppi"] += 1
                for s in samples:
                    if s["hr"]:
                        last_hr["v"] = s["hr"]
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
            if resp is None:
                _log(f"PMD: {what} FAILED; continuing without it")
                if meas_type == pmd.MEAS_PPI:
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
        _log(f"PMD: streaming {' + '.join(sources)}; forwarding to {args.url}")
        if ppi_running:
            _log(f"PMD: PPI warm-up -- Polar documents ~{pmd.PPI_FIRST_SAMPLE_S:.0f}s to the first "
                 f"batch and HR updates only every ~{pmd.PPI_HR_UPDATE_S:.0f}s; silence until then "
                 "is normal")

        t0 = time.monotonic()
        warned: set = set()
        while client.is_connected:
            await asyncio.sleep(args.batch_seconds)
            hr = last_hr["v"]
            rr = batch_rr[:]
            batch_rr.clear()
            mags = acc_mags[:]
            acc_mags.clear()

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
    async with BleakClient(address) as client:
        _log("connected")
        if args.mode in ("pmd", "auto"):
            ok = False
            try:
                ok = await _pmd_session(client, args)
            except Exception as exc:
                _log(f"PMD session error ({exc})")
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
    while True:
        try:
            await _run_once(args, env)
        except Exception as exc:
            _log(f"session error ({exc}); reconnecting in {args.retry_seconds}s")
            await asyncio.sleep(args.retry_seconds)


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
