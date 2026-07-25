#!/usr/bin/env python3
"""Polar Measurement Data (PMD) protocol codec -- pure stdlib, no BLE dependency.

The Polar Verity Sense (and H10/OH1) expose a **vendor** GATT service, ``PMD``, alongside the
generic 0x180D Heart Rate Service. PMD carries the raw sensor streams the generic service hides:

  * **ACC**  -- the armband's OWN triaxial accelerometer. This lets us compute actigraphy without
    the user's iPhone, and in the SAME modality/units (triaxial accel -> magnitude in g) as the
    PhysioNet ``sleep-accel`` training data, removing the unit-scale mismatch that blocks the
    HR+motion model.
  * **PPI**  -- pulse-to-pulse intervals WITH a per-interval error estimate and a "blocker" bit,
    which is a cleaner HRV source than the RR intervals of the generic HR service.

This module is deliberately I/O free: it only builds command bytes and parses frames, so it is
fully unit-testable without hardware (see ``tests/test_polar_pmd.py``). ``verity_forwarder.py``
owns all of the bleak/BLE plumbing.

**SDK MODE: we never enable it, and this module intentionally defines no opcode for it.** Polar's
docs are explicit that SDK mode disables every on-device algorithm -- "any computed data such as
heart rate, PP intervals, RR intervals, etc. is not available anymore" -- in exchange for raw PPG
and higher ACC rates we do not need. Our entire cardiac path is the device-computed PPI/HR, so SDK
mode would silently destroy exactly the signal we came for. The one real hazard is a device left
in SDK mode by *another* app: PPI then starts "successfully" and simply never delivers a sample.
:func:`warmup_state` detects that symptom (no PPI well past the documented ~25 s warm-up) so the
forwarder can print :data:`SDK_MODE_HINT` -- power-cycle the armband -- instead of looking like a
generic connection fault.

Frame layouts implemented here (see the module tests for worked examples):

  Control point START  : [0x02][meas_type] then per-setting TLVs [type][count:uint8][value:uint16 LE]
  Control point STOP   : [0x03][meas_type]
  Control point RESPONSE: [0xF0][opcode][meas_type][error] [more] [setting TLVs...]
  Data characteristic  : [meas_type][timestamp:uint64 LE ns][frame_type][payload...]
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

__all__ = [
    "PMD_SERVICE_UUID", "PMD_CONTROL_UUID", "PMD_DATA_UUID",
    "OP_GET_SETTINGS", "OP_START_MEASUREMENT", "OP_STOP_MEASUREMENT",
    "MEAS_ECG", "MEAS_PPG", "MEAS_ACC", "MEAS_PPI", "MEAS_GYRO", "MEAS_MAG",
    "SETTING_SAMPLE_RATE", "SETTING_RESOLUTION", "SETTING_RANGE", "SETTING_CHANNELS",
    "CONTROL_RESPONSE_HEADER", "ERROR_NAMES", "PmdParseError",
    "build_start_command", "build_stop_command", "parse_control_response",
    "frame_measurement_type", "parse_acc_frame", "parse_ppi_frame",
    "acc_magnitudes_g", "actigraphy_counts", "warmup_state",
    "ZCM_THRESHOLD_G", "MIN_EPOCH_SAMPLES", "PPI_MIN_MS", "PPI_MAX_MS",
    "DEFAULT_ACC_SETTINGS", "PPI_FIRST_SAMPLE_S", "PPI_HR_UPDATE_S", "PMD_STARTUP_GRACE_S",
    "SDK_MODE_SUSPECT_S", "SDK_MODE_HINT", "SDK_MODE_REMEDY",
]

# --------------------------------------------------------------------------------------
# UUIDs (Polar vendor "PMD" service)
# --------------------------------------------------------------------------------------
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"  # write + indicate/read
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"     # notify

# --------------------------------------------------------------------------------------
# Control point opcodes / measurement types / setting types
# --------------------------------------------------------------------------------------
OP_GET_SETTINGS = 0x01
OP_START_MEASUREMENT = 0x02
OP_STOP_MEASUREMENT = 0x03

MEAS_ECG = 0x00
MEAS_PPG = 0x01
MEAS_ACC = 0x02
MEAS_PPI = 0x03
MEAS_GYRO = 0x05
MEAS_MAG = 0x06

# NOTE (deliberate omission): there is NO SDK-mode opcode here, and none may be added. Polar's SDK
# mode shuts down the on-device algorithms -- HR, PP intervals and RR intervals all stop existing,
# and PPI is documented as unavailable in SDK mode -- which is precisely the data this bridge
# exists to collect. It also shuts down the sensors until each stream is explicitly requested, and
# it survives until the device is powered off or explicitly told to stop. Higher raw PPG/ACC rates
# are NOT worth losing PPI/HR: do not "helpfully" turn SDK mode on. We also refuse to guess its
# control-point bytes -- an unverified write to the control point is not something we will ship.

MEAS_NAMES = {
    MEAS_ECG: "ecg", MEAS_PPG: "ppg", MEAS_ACC: "acc",
    MEAS_PPI: "ppi", MEAS_GYRO: "gyro", MEAS_MAG: "mag",
}

SETTING_SAMPLE_RATE = 0x00
SETTING_RESOLUTION = 0x01
SETTING_RANGE = 0x02
SETTING_CHANNELS = 0x04

SETTING_NAMES = {
    SETTING_SAMPLE_RATE: "sample_rate",
    SETTING_RESOLUTION: "resolution",
    SETTING_RANGE: "range",
    SETTING_CHANNELS: "channels",
}
_SETTING_IDS = {v: k for k, v in SETTING_NAMES.items()}
# Aliases people (and CLI flags) actually type.
_SETTING_IDS.update({"rate": SETTING_SAMPLE_RATE, "hz": SETTING_SAMPLE_RATE,
                     "bits": SETTING_RESOLUTION, "g": SETTING_RANGE})

CONTROL_RESPONSE_HEADER = 0xF0

ERROR_NAMES = {
    0: "success",
    1: "invalid op code",
    2: "invalid measurement type",
    3: "not supported",
    4: "invalid length",
    5: "invalid parameter",
    6: "already in state",
    7: "invalid resolution",
    8: "invalid sample rate",
    9: "invalid range",
    10: "invalid MTU",
    11: "invalid number of channels",
    12: "invalid state",
    13: "device in charger",
}

# Frame types on the DATA characteristic. 0x00/0x01 are uncompressed; 0x02 is delta-compressed.
FRAME_TYPE_UNCOMPRESSED = (0x00, 0x01)
FRAME_TYPE_DELTA = 0x02

# Default ACC stream configuration: 52 Hz, 16-bit, +/-8 G.
# Ordered RANGE -> SAMPLE_RATE -> RESOLUTION to match Polar's own verified byte sequence.
DEFAULT_ACC_SETTINGS = {
    SETTING_RANGE: 8,
    SETTING_SAMPLE_RATE: 52,
    SETTING_RESOLUTION: 16,
}

# Order the TLVs are emitted in for a START command. The verified Polar example emits RANGE first
# for ACC; other setting types follow in ascending id. (Firmware is believed to be order
# insensitive, but we match the known-good byte sequence exactly.)
_ACC_TLV_ORDER = (SETTING_RANGE, SETTING_SAMPLE_RATE, SETTING_RESOLUTION, SETTING_CHANNELS)
_DEFAULT_TLV_ORDER = (SETTING_SAMPLE_RATE, SETTING_RESOLUTION, SETTING_RANGE, SETTING_CHANNELS)

# Actigraphy constants -- MUST match scripts/reduce_motion_activity.py so live counts are directly
# comparable to the counts the model was trained on.
ZCM_THRESHOLD_G = 0.01
MIN_EPOCH_SAMPLES = 5

# Plausibility window for a pulse-to-pulse interval (240 bpm .. 24 bpm).
PPI_MIN_MS = 250
PPI_MAX_MS = 2500

# PPI warm-up, per Polar's official Verity Sense documentation: with PPI enabled the heart rate
# only updates every ~5 s and the FIRST PPI batch takes ~25 s to arrive. Silence during this
# window is normal, NOT a failure -- callers must not reconnect or warn inside the grace period.
PPI_FIRST_SAMPLE_S = 25.0
PPI_HR_UPDATE_S = 5.0
PMD_STARTUP_GRACE_S = 40.0

# Well past the warm-up with still no PPI: the most likely cause is a device left in SDK MODE by
# another app (SDK mode disables the HR/PPI algorithms, so the stream starts and stays empty).
SDK_MODE_SUSPECT_S = 60.0
SDK_MODE_REMEDY = (
    "The Verity may be in SDK MODE, which disables the device's HR/PPI algorithms. Power-cycle "
    "the armband (hold the button until it switches off, then back on) to exit SDK mode, then "
    "restart this forwarder."
)
SDK_MODE_HINT = "No PPI data after {seconds:.0f}s. " + SDK_MODE_REMEDY


class PmdParseError(ValueError):
    """A PMD frame was malformed / truncated. Callers should log and drop the frame."""


# --------------------------------------------------------------------------------------
# Control point: command building
# --------------------------------------------------------------------------------------
def _setting_id(key) -> int:
    if isinstance(key, int):
        return key
    try:
        return _SETTING_IDS[str(key).strip().lower()]
    except KeyError:
        raise PmdParseError(f"unknown PMD setting name: {key!r}") from None


def build_start_command(meas_type: int, settings: dict | None = None) -> bytes:
    """Build a START MEASUREMENT control-point command.

    ``settings`` maps setting type (id 0x00/0x01/0x02/0x04 or the names ``sample_rate``,
    ``resolution``, ``range``, ``channels``) to a uint16 value (or a sequence of uint16 values).
    ``None``/empty means "no settings", which is what PPI requires.

    Each TLV is ``[setting_type][count:uint8][value:uint16 LE]*count``; e.g. ACC at 25 Hz / 16-bit
    / 2 G is ``02 02 | 02 01 02 00 | 00 01 19 00 | 01 01 10 00``.
    """
    out = bytearray([OP_START_MEASUREMENT, meas_type & 0xFF])
    if not settings:
        return bytes(out)

    normalized: dict[int, list[int]] = {}
    for key, value in settings.items():
        sid = _setting_id(key)
        if isinstance(value, (list, tuple)):
            values = [int(v) for v in value]
        else:
            values = [int(value)]
        for v in values:
            if not 0 <= v <= 0xFFFF:
                raise PmdParseError(f"PMD setting {SETTING_NAMES.get(sid, sid)} out of range: {v}")
        normalized[sid] = values

    order = _ACC_TLV_ORDER if meas_type == MEAS_ACC else _DEFAULT_TLV_ORDER
    ordered = [s for s in order if s in normalized]
    ordered += sorted(s for s in normalized if s not in ordered)

    for sid in ordered:
        values = normalized[sid]
        out.append(sid & 0xFF)
        out.append(len(values) & 0xFF)
        for v in values:
            out += int(v).to_bytes(2, "little")
    return bytes(out)


def build_stop_command(meas_type: int) -> bytes:
    """Build a STOP MEASUREMENT control-point command (e.g. PPI -> ``03 03``)."""
    return bytes([OP_STOP_MEASUREMENT, meas_type & 0xFF])


def build_get_settings_command(meas_type: int) -> bytes:
    """Build a GET SETTINGS control-point command (``01 <meas_type>``)."""
    return bytes([OP_GET_SETTINGS, meas_type & 0xFF])


# --------------------------------------------------------------------------------------
# Control point: response parsing
# --------------------------------------------------------------------------------------
def _try_parse_settings(buf: bytes) -> tuple[dict, bool]:
    """Parse a run of ``[type][count:uint8][uint16 LE]*count`` TLVs.

    Returns ``(settings, exact)`` where ``exact`` is True only when the buffer was consumed
    completely by well-formed TLVs with known setting ids.
    """
    settings: dict = {}
    i = 0
    n = len(buf)
    while i < n:
        if i + 2 > n:
            return settings, False
        sid = buf[i]
        count = buf[i + 1]
        i += 2
        if sid not in SETTING_NAMES or count == 0 or i + 2 * count > n:
            return settings, False
        values = [int.from_bytes(buf[i + 2 * k:i + 2 * k + 2], "little") for k in range(count)]
        i += 2 * count
        settings[sid] = values
    return settings, True


def parse_control_response(data: bytes | bytearray) -> dict:
    """Parse a PMD control-point response/indication.

    Layout: ``[0xF0][opcode][meas_type][error][more][setting TLVs...]``.

    Returns a dict with ``opcode``, ``measurement_type``, ``error_code``, ``error``, ``ok``,
    ``more``, ``settings`` (setting id -> list of values) and ``settings_by_name``.
    """
    data = bytes(data)
    if len(data) < 4:
        raise PmdParseError(f"control response too short ({len(data)} bytes)")
    if data[0] != CONTROL_RESPONSE_HEADER:
        raise PmdParseError(f"unexpected control response header 0x{data[0]:02X}")

    opcode = data[1]
    meas_type = data[2]
    error = data[3]

    more = False
    settings: dict = {}
    rest = data[4:]
    if rest:
        # Polar's SDK puts a "more frames follow" flag at index 4 and the TLVs at index 5. Some
        # documentation shows the TLVs starting straight after the error byte, so accept both:
        # prefer the layout whose TLV run consumes its buffer exactly.
        tail_settings, tail_exact = _try_parse_settings(rest[1:])
        head_settings, head_exact = _try_parse_settings(rest)
        if tail_exact and (tail_settings or not head_exact or not head_settings):
            more = bool(rest[0])
            settings = tail_settings
        elif head_exact:
            settings = head_settings
        else:
            more = bool(rest[0])
            settings = tail_settings

    return {
        "opcode": opcode,
        "measurement_type": meas_type,
        "measurement": MEAS_NAMES.get(meas_type, f"0x{meas_type:02X}"),
        "error_code": error,
        "error": ERROR_NAMES.get(error, f"unknown error {error}"),
        "ok": error == 0,
        "more": more,
        "settings": settings,
        "settings_by_name": {SETTING_NAMES[k]: v for k, v in settings.items() if k in SETTING_NAMES},
    }


# --------------------------------------------------------------------------------------
# Data characteristic: common header
# --------------------------------------------------------------------------------------
def frame_measurement_type(data: bytes | bytearray) -> int:
    """Measurement type of a data-characteristic frame (byte 0)."""
    if not data:
        raise PmdParseError("empty PMD data frame")
    return data[0]


def _split_frame(data: bytes | bytearray, expect_type: int) -> tuple[int, int, bytes]:
    data = bytes(data)
    if len(data) < 10:
        raise PmdParseError(f"PMD data frame too short ({len(data)} bytes)")
    meas_type = data[0]
    if meas_type != expect_type:
        raise PmdParseError(
            f"expected measurement type 0x{expect_type:02X}, got 0x{meas_type:02X}")
    timestamp_ns = int.from_bytes(data[1:9], "little")
    frame_type = data[9]
    return timestamp_ns, frame_type, data[10:]


def _read_bits(buf: bytes, bit_offset: int, width: int) -> int:
    """Read ``width`` bits LSB-first starting at ``bit_offset`` (bits packed continuously)."""
    value = 0
    for i in range(width):
        idx = bit_offset + i
        value |= ((buf[idx >> 3] >> (idx & 7)) & 1) << i
    return value


def _to_signed(value: int, width: int) -> int:
    if width and value >= (1 << (width - 1)):
        value -= 1 << width
    return value


def _clamp_i16(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


# --------------------------------------------------------------------------------------
# ACC
# --------------------------------------------------------------------------------------
def parse_acc_frame(data: bytes | bytearray) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Parse an ACC data frame -> ``(timestamp_ns, frame_type, [(x, y, z), ...])`` in **milliG**.

    Handles both the uncompressed layout (6 bytes/sample: int16 LE x, y, z) and the
    delta-compressed layout (an int16 reference sample followed by blocks of
    ``[delta_bit_width][sample_count]`` + bit-packed signed deltas, 3 axes per sample, LSB-first
    and continuous across byte boundaries; each block restarts on a byte boundary).
    """
    timestamp_ns, frame_type, payload = _split_frame(data, MEAS_ACC)

    if frame_type in FRAME_TYPE_UNCOMPRESSED:
        if len(payload) % 6:
            raise PmdParseError(
                f"uncompressed ACC payload not a multiple of 6 bytes ({len(payload)})")
        samples = []
        for off in range(0, len(payload), 6):
            samples.append(tuple(
                int.from_bytes(payload[off + 2 * a:off + 2 * a + 2], "little", signed=True)
                for a in range(3)
            ))
        return timestamp_ns, frame_type, samples

    # Bit 7 set is how newer Polar firmware flags a compressed frame; 0x02 is the documented
    # delta frame type. Treat both as delta-compressed.
    if frame_type == FRAME_TYPE_DELTA or frame_type & 0x80:
        return timestamp_ns, frame_type, _parse_delta_payload(payload, channels=3)

    raise PmdParseError(f"unsupported ACC frame type 0x{frame_type:02X}")


def _parse_delta_payload(payload: bytes, channels: int = 3) -> list[tuple[int, ...]]:
    ref_bytes = 2 * channels
    if len(payload) < ref_bytes:
        raise PmdParseError(f"delta ACC payload too short ({len(payload)} bytes)")
    current = [int.from_bytes(payload[2 * c:2 * c + 2], "little", signed=True)
               for c in range(channels)]
    samples: list[tuple[int, ...]] = [tuple(current)]

    off = ref_bytes
    n = len(payload)
    while off < n:
        if off + 2 > n:
            # A single trailing pad byte is tolerated; anything else is malformed.
            if payload[off:] == b"\x00":
                break
            raise PmdParseError("truncated delta block header")
        bit_width = payload[off]
        count = payload[off + 1]
        off += 2
        if count == 0:
            if bit_width == 0:
                break  # zero-filled padding at the end of the frame
            continue
        if bit_width == 0:
            # All deltas are zero -> the sample simply repeats.
            samples.extend([tuple(current)] * count)
            continue
        total_bits = bit_width * channels * count
        nbytes = (total_bits + 7) // 8
        if off + nbytes > n:
            raise PmdParseError(
                f"truncated delta block (need {nbytes} bytes, have {n - off})")
        block = payload[off:off + nbytes]
        bit = 0
        for _ in range(count):
            for c in range(channels):
                delta = _to_signed(_read_bits(block, bit, bit_width), bit_width)
                bit += bit_width
                current[c] = _clamp_i16(current[c] + delta)
            samples.append(tuple(current))
        off += nbytes
    return samples


# --------------------------------------------------------------------------------------
# PPI
# --------------------------------------------------------------------------------------
def parse_ppi_frame(data: bytes | bytearray) -> tuple[int, list[dict]]:
    """Parse a PPI data frame -> ``(timestamp_ns, [sample, ...])``.

    Each 6-byte sample is ``[hr:uint8][ppi_ms:uint16 LE][error_ms:uint16 LE][flags]`` where flags
    bit0 = blocker (interval unreliable), bit1 = skin contact detected, bit2 = skin contact
    supported. Every sample dict also carries ``ok``: False when the blocker bit is set or the
    interval is outside the plausible 250-2500 ms window.
    """
    timestamp_ns, _frame_type, payload = _split_frame(data, MEAS_PPI)
    if len(payload) % 6:
        raise PmdParseError(f"PPI payload not a multiple of 6 bytes ({len(payload)})")

    samples: list[dict] = []
    for off in range(0, len(payload), 6):
        hr = payload[off]
        ppi_ms = int.from_bytes(payload[off + 1:off + 3], "little")
        error_ms = int.from_bytes(payload[off + 3:off + 5], "little")
        flags = payload[off + 5]
        blocker = bool(flags & 0x01)
        samples.append({
            "hr": hr,
            "ppi_ms": ppi_ms,
            "error_ms": error_ms,
            "blocker": blocker,
            "skin_contact": bool(flags & 0x02),
            "skin_contact_supported": bool(flags & 0x04),
            "ok": (not blocker) and PPI_MIN_MS <= ppi_ms <= PPI_MAX_MS,
        })
    return timestamp_ns, samples


def warmup_state(elapsed_s: float, frames_seen: int,
                 grace_s: float = PMD_STARTUP_GRACE_S,
                 sdk_suspect_s: float = SDK_MODE_SUSPECT_S) -> str:
    """Classify a quiet PMD stream: ``streaming`` / ``warming_up`` / ``stalled`` / ``sdk_mode_suspect``.

    PPI legitimately produces nothing for ~25 s after START, so a stream that has delivered no
    frames is only ``stalled`` once the grace period (>= that warm-up, with margin) has elapsed,
    and only ``sdk_mode_suspect`` past :data:`SDK_MODE_SUSPECT_S`. Nothing may reconnect or warn
    while the state is ``warming_up``.
    """
    if frames_seen > 0:
        return "streaming"
    if elapsed_s >= max(sdk_suspect_s, grace_s):
        return "sdk_mode_suspect"
    if elapsed_s < max(grace_s, PPI_FIRST_SAMPLE_S):
        return "warming_up"
    return "stalled"


def usable_ppi(samples: Iterable[dict]) -> list[float]:
    """Intervals (ms) from PPI samples that are neither blocked nor implausible."""
    return [float(s["ppi_ms"]) for s in samples if s.get("ok")]


# --------------------------------------------------------------------------------------
# Actigraphy
# --------------------------------------------------------------------------------------
def acc_magnitudes_g(samples_milli_g: Iterable[Sequence[int]]) -> list[float]:
    """Triaxial samples in milliG -> vector magnitudes in **g** (the training-data unit)."""
    out = []
    for x, y, z in samples_milli_g:
        gx = x / 1000.0
        gy = y / 1000.0
        gz = z / 1000.0
        out.append(math.sqrt(gx * gx + gy * gy + gz * gz))
    return out


def actigraphy_counts(samples_g, zcm_threshold: float = ZCM_THRESHOLD_G,
                      round_values: bool = True) -> dict:
    """Actigraphy counts over a batch of accelerometer samples, in g.

    ``samples_g`` is either a sequence of magnitudes (floats, in g) or a sequence of ``(x, y, z)``
    triples already in g. Definitions are copied verbatim from
    ``scripts/reduce_motion_activity.py`` (gravity removed by subtracting the batch's own mean
    magnitude) so live counts are directly comparable to the training counts:

      pim  = sum |mag - mean|        zcm  = transitions of (|dev| > threshold)
      mad  = pim / n                 std  = population std of the deviations
      pmax = max |dev|               n    = sample count

    Returns ``{"pim", "zcm", "mad", "std", "pmax", "n"}``. Note ACC frames arrive in milliG --
    convert with :func:`acc_magnitudes_g` first.
    """
    mags: list[float] = []
    for s in samples_g:
        if isinstance(s, (list, tuple)):
            x, y, z = s
            mags.append(math.sqrt(x * x + y * y + z * z))
        else:
            mags.append(float(s))

    n = len(mags)
    if n == 0:
        return {"pim": 0.0, "zcm": 0, "mad": 0.0, "std": 0.0, "pmax": 0.0, "n": 0}

    mean = sum(mags) / n
    devs = [m - mean for m in mags]
    abs_devs = [abs(d) for d in devs]
    pim = sum(abs_devs)
    mad = pim / n
    var = sum(d * d for d in devs) / n
    std = math.sqrt(var)
    pmax = max(abs_devs)
    zcm = 0
    above = abs_devs[0] > zcm_threshold
    for a in abs_devs[1:]:
        now_above = a > zcm_threshold
        if now_above != above:
            zcm += 1
            above = now_above

    if round_values:
        # Same rounding reduce_motion_activity.py writes to disk, so the two are byte-comparable.
        return {"pim": round(pim, 5), "zcm": zcm, "mad": round(mad, 6),
                "std": round(std, 6), "pmax": round(pmax, 5), "n": n}
    return {"pim": pim, "zcm": zcm, "mad": mad, "std": std, "pmax": pmax, "n": n}
