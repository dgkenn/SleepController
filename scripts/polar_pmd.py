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

  Control point START  : [0x02][meas_type] then per-setting TLVs [type][count:uint8][value LE]
                          (value is uint16 for sample rate/resolution/range, uint8 for channels)
  Control point STOP   : [0x03][meas_type]
  Control point RESPONSE: [0xF0][opcode][meas_type][error][more][setting TLVs...]
  Data characteristic  : [meas_type][timestamp:uint64 LE ns][frame_type][payload...]
                          (frame_type bit 7 = compressed; timestamp is the LAST sample's time)
"""
from __future__ import annotations

import math
import struct
from typing import Iterable, Sequence

__all__ = [
    "PMD_SERVICE_UUID", "PMD_CONTROL_UUID", "PMD_DATA_UUID",
    "OP_GET_SETTINGS", "OP_START_MEASUREMENT", "OP_STOP_MEASUREMENT",
    "MEAS_ECG", "MEAS_PPG", "MEAS_ACC", "MEAS_PPI", "MEAS_GYRO", "MEAS_MAG",
    "SETTING_SAMPLE_RATE", "SETTING_RESOLUTION", "SETTING_RANGE", "SETTING_CHANNELS",
    "SETTING_FACTOR",
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
# setting type 0x03 ("RANGE_MILLIUNIT") exists in Polar's official PMD PDF (Table 5) and in
# zHElEARN/polar-python's constants, but the PDF itself labels it "NOT USED" and neither
# reference implementation ever sends or expects it for ECG/PPG/ACC/PPI -- deliberately omitted.
SETTING_CHANNELS = 0x04
SETTING_FACTOR = 0x05

SETTING_NAMES = {
    SETTING_SAMPLE_RATE: "sample_rate",
    SETTING_RESOLUTION: "resolution",
    SETTING_RANGE: "range",
    SETTING_CHANNELS: "channels",
    SETTING_FACTOR: "factor",
}
_SETTING_IDS = {v: k for k, v in SETTING_NAMES.items()}
# Aliases people (and CLI flags) actually type.
_SETTING_IDS.update({"rate": SETTING_SAMPLE_RATE, "hz": SETTING_SAMPLE_RATE,
                     "bits": SETTING_RESOLUTION, "g": SETTING_RANGE})

# Wire width of a setting's value(s), in bytes. Confirmed by THREE independent sources agreeing
# exactly: Polar's own official PMD PDF (Table 5: "4 | Number of channels | 1 | uint8" and
# "5 | Conversion factor | 4 | IEEE 754 ..."), bleakheart's start_streaming
# (``wlen=2 if s!='CHANNELS' else 1``), and polar-python's ``PmdSettingType.field_size``
# (CHANNELS=1, FACTOR=4, everything else=2). A captured real Verity Sense ACC settings response
# in the PDF (sec 5.2) even shows the raw bytes: "...04 01 03" -- setting type CHANNELS, array
# length 1, and the single value byte 0x03 (3 channels) with NO second, padding byte.
#
# BUG FIXED: this module used to encode (and expect to decode) every setting's value(s) as
# uint16 unconditionally. For CHANNELS that writes one extra, unexpected byte into every START
# command that configures channel count, and misaligns the parse of every subsequent TLV (and,
# for FACTOR, four bytes short) in any GET-SETTINGS/START response that includes one of these
# two setting types -- silently corrupting or losing every setting that comes after it in the
# same response. See test_channels_setting_uses_one_byte_not_two.
_SETTING_FIELD_SIZE = {
    SETTING_SAMPLE_RATE: 2,
    SETTING_RESOLUTION: 2,
    SETTING_RANGE: 2,
    SETTING_CHANNELS: 1,
    SETTING_FACTOR: 4,
}

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

# Frame types on the DATA characteristic. Confirmed against Polar's official PMD PDF
# (polarofficial/polar-ble-sdk, technical_documentation/online_measurement.pdf, sec 4.2.9
# "Parse meta data") AND two independent, real-hardware-tested community codecs
# (fsmeraldi/bleakheart PmdDataFrame handling; zHElEARN/polar-python PmdDataFrame.from_bytes):
# compression is signalled SOLELY by bit 7 (0x80) of the frame-type byte; the low 7 bits are
# an independent "raw data layout" id (0/1/2 = 8/16/24-bit uncompressed samples for ACC, per
# the PDF's Tables 7-9) that is NOT itself a "this frame is delta-compressed" flag. A captured
# real Verity Sense delta ACC frame in the PDF (sec 5.3) has frame-type byte 0x80.
#
# BUG FIXED: this module used to treat a literal frame-type byte of 0x02 as "always delta",
# which collided with the *uncompressed* 24-bit ACC layout (Table 9) -- a real 0x02 frame with
# bit 7 clear would have been silently run through the delta decoder instead of being rejected
# as an unsupported (not implemented here) raw layout. See
# test_acc_frame_type_0x02_without_compression_bit_is_not_delta.
FRAME_TYPE_UNCOMPRESSED = (0x00, 0x01)
FRAME_TYPE_COMPRESSED_BIT = 0x80

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
    ``resolution``, ``range``, ``channels``) to a value (or a sequence of values). ``None``/empty
    means "no settings", which is what PPI requires.

    Each TLV is ``[setting_type][count:uint8][value]*count`` where ``value`` is little-endian and
    ``count`` bytes wide per :data:`_SETTING_FIELD_SIZE` -- uint16 for sample rate / resolution /
    range, but uint8 for channels (confirmed against a real captured Verity Sense byte sequence;
    see the comment on :data:`_SETTING_FIELD_SIZE`). E.g. ACC at 25 Hz / 16-bit / 2 G is
    ``02 02 | 02 01 02 00 | 00 01 19 00 | 01 01 10 00``.

    Setting TLVs may be emitted in any order: Polar's own official PMD PDF documents ACC START
    requests in two different orders for two different devices (RANGE-first for the H10, sec
    5.3; SAMPLE_RATE-first for the Verity Sense, sec 5.3) that both work, confirming firmware is
    order-insensitive. This module still reproduces the RANGE-first byte sequence for ACC (the
    one we have byte-for-byte confirmation of) purely to match a known-good example exactly.
    """
    out = bytearray([OP_START_MEASUREMENT, meas_type & 0xFF])
    if not settings:
        return bytes(out)

    normalized: dict[int, list[int]] = {}
    for key, value in settings.items():
        sid = _setting_id(key)
        width = _SETTING_FIELD_SIZE.get(sid, 2)
        if isinstance(value, (list, tuple)):
            values = [int(v) for v in value]
        else:
            values = [int(value)]
        max_value = (1 << (8 * width)) - 1
        for v in values:
            if not 0 <= v <= max_value:
                raise PmdParseError(f"PMD setting {SETTING_NAMES.get(sid, sid)} out of range: {v}")
        normalized[sid] = values

    order = _ACC_TLV_ORDER if meas_type == MEAS_ACC else _DEFAULT_TLV_ORDER
    ordered = [s for s in order if s in normalized]
    ordered += sorted(s for s in normalized if s not in ordered)

    for sid in ordered:
        values = normalized[sid]
        width = _SETTING_FIELD_SIZE.get(sid, 2)
        out.append(sid & 0xFF)
        out.append(len(values) & 0xFF)
        for v in values:
            out += int(v).to_bytes(width, "little")
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
    """Parse a run of ``[type][count:uint8][value]*count`` TLVs (value width per setting type;
    see :data:`_SETTING_FIELD_SIZE` -- uint16 for most settings, uint8 for channels, an IEEE-754
    float32 for the conversion factor).

    Returns ``(settings, exact)`` where ``exact`` is True only when the buffer was consumed
    completely by well-formed TLVs with known setting ids. ``settings[SETTING_FACTOR]`` holds
    ``float`` values; every other setting holds ``int`` values.
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
        width = _SETTING_FIELD_SIZE.get(sid, 2)
        if sid not in SETTING_NAMES or count == 0 or i + width * count > n:
            return settings, False
        if sid == SETTING_FACTOR:
            values = [struct.unpack("<f", buf[i + width * k:i + width * (k + 1)])[0]
                      for k in range(count)]
        else:
            values = [int.from_bytes(buf[i + width * k:i + width * (k + 1)], "little")
                      for k in range(count)]
        i += width * count
        settings[sid] = values
    return settings, True


def parse_control_response(data: bytes | bytearray) -> dict:
    """Parse a PMD control-point response/indication.

    Layout: ``[0xF0][opcode][meas_type][error][more][setting TLVs...]``. The "more frames
    follow" flag is ALWAYS byte 4 and the TLV run ALWAYS starts at byte 5 -- confirmed by two
    independent, real-hardware-tested implementations agreeing exactly:
    bleakheart's ``available_settings`` (``if data[4]!=0x00: raise ...; offset=5``) and
    polar-python's ``MeasurementSettings.from_bytes`` (``more_frames = data[4] != 0; index = 5``).
    There is no alternate layout to disambiguate, so (unlike an earlier version of this
    function) we no longer guess between two possible offsets.

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
        more = bool(rest[0])
        settings, _exact = _try_parse_settings(rest[1:])

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
    """Read ``width`` bits LSB-first starting at ``bit_offset`` (bits packed continuously).

    CROSS-REFERENCE DISAGREEMENT (unresolved, flagged per the audit that added this comment):
    this LSB-first-within-byte, LSB-first-into-value bit order matches
    ``zHElEARN/polar-python``'s ``parsers/compression.py`` (``parse_delta_frame``), which is the
    function that library actually uses to decode ACC delta frames on both the Polar H10 and
    Verity Sense, i.e. the reference closest to this exact code path -- so this module's
    pre-existing choice is left unchanged. However ``fsmeraldi/bleakheart``'s
    ``_decode_ppg_data`` (real Verity-Sense-under-Windows hardware, though only exercised for
    PPG, never ACC, in that library) builds its delta bit-string MSB-first per byte
    (``f'{byte:08b}'``) and interprets each chunk MSB-first, i.e. the OPPOSITE convention.
    Polar's own official PMD PDF (sec 4.3.3) describes the accumulation as repeated
    "shift left, OR in the next bit", which is consistent with either convention depending on
    what "transformed into binary format" means for the source bytes, so it does not break the
    tie. Net effect: for any bit width that is NOT a multiple of 8, this module and
    bleakheart's PPG decoder would decode DIFFERENT sample values from the same bytes. This
    matters here only if it turns out ACC and PPG do not share identical bit-packing on real
    hardware. STILL-UNVERIFIED against a real Verity Sense ACC delta capture with a non-8-bit
    width; confirm before trusting delta ACC magnitudes from an overnight run that uses
    non-byte-aligned delta widths.
    """
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
    ``[delta_bit_width][sample_count]`` + bit-packed signed deltas, 3 axes per sample,
    LSB-first and continuous across byte boundaries; each block restarts on a byte boundary --
    see :func:`_read_bits` for the exact bit order and its cross-reference caveat).

    ``timestamp_ns`` is the sensor's own clock reading for the **LAST** sample in the frame, not
    the first (confirmed by both bleakheart's docstring -- "the time stamp refers to the last
    sample of the list constituting the payload" -- and Polar's own official PMD PDF worked ACC
    delta example, sec 5.3, which labels the identical field "last sample timestamp"). Callers
    that need a per-sample timestamp must count backwards from this value, not forwards.

    STILL-UNVERIFIED, deliberately not applied here: the START-measurement control response for
    ACC on a real Verity Sense (Polar's official PMD PDF, sec 5.3) includes a setting-type 5
    ("factor") TLV -- see :data:`SETTING_FACTOR` -- with a non-1.0 IEEE-754 float value. Its raw
    bytes (``40 DA 7F 39`` LE) decode to ``~0.000244`` (the PDF's own prose annotates this TLV
    with a similar-but-not-bit-exact number, most likely an OCR artifact of that page's
    sequence-diagram graphic -- see test_control_response_factor_setting_is_a_four_byte_float).
    Multiplying that PDF's own worked delta reference sample (z=4068 raw) by ~0.000244 gives
    ~0.99 (physically a plausible ~1 g resting reading), which is suspicious: it suggests
    DELTA-compressed ACC samples may need `raw * factor * 1000` to
    reach true milliG, while uncompressed samples (Table 8: "16-bit signed value (mG)") do not.
    zHElEARN/polar-python's own compressed-ACC decoder is inconsistent about this (it applies
    `factor * 1000` for its 8-bit path but bare `factor`, no `* 1000`, for the 16-bit path Verity
    Sense actually uses), and bleakheart never decodes compressed ACC at all, so neither
    reference settles the exact formula. This module does not thread a factor through
    ``parse_acc_frame`` at all (callers get raw int16 deltas/samples either way) rather than
    risk guessing the multiplier wrong -- a wrong constant scale factor would NOT be caught by
    :func:`actigraphy_counts` (which only looks at deviations from the batch's own mean) and
    would silently misrepresent every delta-compressed-frame actigraphy count all night.
    Confirm the real per-frame magnitudes against known-still/known-moving reference recordings
    before trusting absolute (not just relative) accelerometer magnitudes from compressed
    frames.
    """
    timestamp_ns, frame_type, payload = _split_frame(data, MEAS_ACC)

    # Compression is signalled by bit 7 alone -- see FRAME_TYPE_COMPRESSED_BIT above for the
    # evidence trail. This must be checked BEFORE the uncompressed-type check below.
    if frame_type & FRAME_TYPE_COMPRESSED_BIT:
        return timestamp_ns, frame_type, _parse_delta_payload(payload, channels=3)

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

    Field order/widths and bit0/bit1 CONFIRMED against Polar's official PMD PDF (Table 22, "PPi
    Data, TYPE0") and zHElEARN/polar-python's ``PPIData._data_from_raw_type_0`` (independently
    matching this module exactly, both on real H10 and Verity Sense hardware). bit2
    ("skin_contact_supported") is an UNRESOLVED disagreement between those two sources: the PDF's
    prose says "bit 2: 1 if sensor contact is not supported" (i.e. 1 = NOT supported), but
    polar-python's real-hardware-tested code treats bit2 truthy as "supported" -- the same
    polarity this module already used. We keep the polarity that matches the tested code, since
    PDF prose elsewhere in this same document has OCR/wording artifacts (see the frame-type
    bitmask notes above), and flag the PDF's contradicting wording here rather than silently
    trusting either source.
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
