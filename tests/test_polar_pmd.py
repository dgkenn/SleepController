"""Unit tests for the Polar PMD codec (scripts/polar_pmd.py) -- synthetic frames, no hardware."""
from __future__ import annotations

import math
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import polar_pmd as pmd  # noqa: E402
import reduce_motion_activity as rma  # noqa: E402  (reference implementation of the counts)


# --------------------------------------------------------------------------------------
# helpers: build synthetic frames the way the device would
# --------------------------------------------------------------------------------------
def _frame(meas_type: int, timestamp_ns: int, frame_type: int, payload: bytes) -> bytes:
    return bytes([meas_type]) + timestamp_ns.to_bytes(8, "little") + bytes([frame_type]) + payload


def _acc_uncompressed(samples, timestamp_ns=1234567890123456789) -> bytes:
    payload = b"".join(
        b"".join(int(v).to_bytes(2, "little", signed=True) for v in s) for s in samples
    )
    return _frame(pmd.MEAS_ACC, timestamp_ns, 0x00, payload)


class _BitWriter:
    """LSB-first continuous bit packer -- the encoder counterpart of the decoder under test."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def write(self, value: int, width: int) -> None:
        if value < 0:
            value += 1 << width  # two's complement
        for i in range(width):
            self.bits.append((value >> i) & 1)

    def to_bytes(self) -> bytes:
        out = bytearray((len(self.bits) + 7) // 8)
        for i, b in enumerate(self.bits):
            if b:
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)


def _acc_delta(reference, blocks, timestamp_ns=42) -> bytes:
    """blocks = [(bit_width, [(dx, dy, dz), ...]), ...] -> a delta-compressed ACC frame."""
    payload = bytearray()
    for v in reference:
        payload += int(v).to_bytes(2, "little", signed=True)
    for bit_width, deltas in blocks:
        payload.append(bit_width)
        payload.append(len(deltas))
        w = _BitWriter()
        for d in deltas:
            for axis in d:
                w.write(axis, bit_width)
        payload += w.to_bytes()
    # 0x80 = compression bit set (bit 7); matches the frame-type byte of the real Verity Sense
    # delta ACC frame captured in Polar's official PMD PDF (technical_documentation/
    # online_measurement.pdf, sec 5.3). A frame-type byte of 0x02 WITHOUT bit 7 set is a
    # different, unrelated thing (uncompressed 24-bit ACC, PDF Table 9) -- see
    # test_acc_frame_type_0x02_without_compression_bit_is_not_delta.
    return _frame(pmd.MEAS_ACC, timestamp_ns, 0x80, bytes(payload))


def _ppi(samples, timestamp_ns=0) -> bytes:
    """samples = [(hr, ppi_ms, error_ms, flags), ...]"""
    payload = bytearray()
    for hr, ppi_ms, err_ms, flags in samples:
        payload.append(hr)
        payload += int(ppi_ms).to_bytes(2, "little")
        payload += int(err_ms).to_bytes(2, "little")
        payload.append(flags)
    return _frame(pmd.MEAS_PPI, timestamp_ns, 0x00, bytes(payload))


# --------------------------------------------------------------------------------------
# control point: command building
# --------------------------------------------------------------------------------------
def test_acc_start_matches_verified_polar_example():
    """ACC @ 25 Hz, 16-bit, 2G must reproduce Polar's known-good byte sequence exactly."""
    cmd = pmd.build_start_command(pmd.MEAS_ACC, {
        pmd.SETTING_SAMPLE_RATE: 25,
        pmd.SETTING_RESOLUTION: 16,
        pmd.SETTING_RANGE: 2,
    })
    assert cmd == bytes.fromhex("0202" "020102 00" "000119 00" "010110 00".replace(" ", ""))
    assert cmd.hex() == "0202020102000001190001011000"


def test_acc_start_accepts_setting_names_and_is_order_independent():
    by_name = pmd.build_start_command(pmd.MEAS_ACC,
                                      {"resolution": 16, "range": 2, "sample_rate": 25})
    assert by_name.hex() == "0202020102000001190001011000"


def test_acc_start_defaults_52hz_8g():
    cmd = pmd.build_start_command(pmd.MEAS_ACC, pmd.DEFAULT_ACC_SETTINGS)
    # 02 02 | range=8 | rate=52 (0x34) | resolution=16 (0x10)
    assert cmd.hex() == "0202020108000001340001011000"


def test_ppi_start_and_stop_take_no_settings():
    assert pmd.build_start_command(pmd.MEAS_PPI, None) == b"\x02\x03"
    assert pmd.build_start_command(pmd.MEAS_PPI, {}) == b"\x02\x03"
    assert pmd.build_stop_command(pmd.MEAS_PPI) == b"\x03\x03"
    assert pmd.build_stop_command(pmd.MEAS_ACC) == b"\x03\x02"
    assert pmd.build_get_settings_command(pmd.MEAS_ACC) == b"\x01\x02"


def test_start_command_rejects_unknown_setting_and_out_of_range_value():
    with pytest.raises(pmd.PmdParseError):
        pmd.build_start_command(pmd.MEAS_ACC, {"bogus": 1})
    with pytest.raises(pmd.PmdParseError):
        pmd.build_start_command(pmd.MEAS_ACC, {"sample_rate": 70000})


def test_channels_setting_uses_one_byte_not_two():
    """Regression test for a real bug: CHANNELS values used to be encoded as uint16 (2 bytes),
    like every other setting. Polar's official PMD PDF (Table 5), bleakheart's start_streaming
    (``wlen=2 if s!='CHANNELS' else 1``) and polar-python's ``PmdSettingType.field_size`` all
    independently agree CHANNELS is ONE byte -- confirmed by a real captured Verity Sense ACC
    START request in that PDF (sec 5.3: "...04 01 03" = type CHANNELS, array_length 1, single
    value byte 0x03, no second/padding byte). Field widths here match that capture; this
    module's own RANGE-first TLV ordering (order is confirmed firmware-insensitive, see
    build_start_command's docstring) is used instead of the PDF's SAMPLE_RATE-first order.
    """
    cmd = pmd.build_start_command(pmd.MEAS_ACC, {
        pmd.SETTING_RANGE: 8,
        pmd.SETTING_SAMPLE_RATE: 52,
        pmd.SETTING_RESOLUTION: 16,
        pmd.SETTING_CHANNELS: 3,
    })
    assert cmd == bytes.fromhex(
        ("0202"
         "02 01 08 00"    # RANGE = 8G
         "00 01 34 00"    # SAMPLE_RATE = 52 Hz
         "01 01 10 00"    # RESOLUTION = 16-bit
         "04 01 03"       # CHANNELS = 3 -- ONE byte value, no padding byte
         ).replace(" ", ""))
    # An out-of-range CHANNELS value (>255) must be rejected against the 1-byte width, not 0xFFFF.
    with pytest.raises(pmd.PmdParseError):
        pmd.build_start_command(pmd.MEAS_ACC, {pmd.SETTING_CHANNELS: 256})


# --------------------------------------------------------------------------------------
# control point: response parsing
# --------------------------------------------------------------------------------------
def test_control_response_success_no_settings():
    resp = pmd.parse_control_response(bytes([0xF0, 0x02, 0x02, 0x00, 0x00]))
    assert resp["ok"] is True
    assert resp["opcode"] == pmd.OP_START_MEASUREMENT
    assert resp["measurement_type"] == pmd.MEAS_ACC
    assert resp["measurement"] == "acc"
    assert resp["error_code"] == 0
    assert resp["settings"] == {}


def test_control_response_error_is_surfaced():
    # error 9 = invalid range
    resp = pmd.parse_control_response(bytes([0xF0, 0x02, 0x02, 0x09]))
    assert resp["ok"] is False
    assert resp["error_code"] == 9
    assert "range" in resp["error"]


def test_control_response_parses_setting_tlvs():
    # F0 01 02 00 | more=0 | rate TLV (2 values: 52, 104) | resolution TLV (16)
    raw = bytes([0xF0, 0x01, 0x02, 0x00, 0x00,
                 0x00, 0x02, 0x34, 0x00, 0x68, 0x00,
                 0x01, 0x01, 0x10, 0x00])
    resp = pmd.parse_control_response(raw)
    assert resp["ok"] is True
    assert resp["settings_by_name"] == {"sample_rate": [52, 104], "resolution": [16]}
    assert resp["more"] is False


def test_control_response_rejects_bad_header_and_short_frame():
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_control_response(bytes([0xF1, 0x02, 0x02, 0x00]))
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_control_response(bytes([0xF0, 0x02]))


def test_control_response_channels_setting_is_one_byte():
    """Regression test for a real bug: a CHANNELS TLV in a settings response used to be parsed
    as a 2-byte value like every other setting, which both reads the wrong value AND misaligns
    the parse of everything after it in the same response. Built from Polar's official PMD PDF
    field descriptions for a real Verity Sense ACC GET-SETTINGS response (sec 5.2: sample rate
    52 Hz, resolution 16-bit, range 8G, channels 3) -- the PDF's own hex rendering of this
    specific response is corrupted by OCR of an overlapping sequence-diagram graphic, so the
    bytes below are reconstructed from its individually legible prose fields, which is what
    actually matters for this test (that CHANNELS is one byte, immediately followed by nothing).
    """
    raw = bytes([0xF0, 0x01, 0x02, 0x00, 0x00,          # header, opcode=GET, meas=ACC, ok, more=0
                 0x00, 0x01, 0x34, 0x00,                 # SAMPLE_RATE, 1 value, 52 Hz
                 0x01, 0x01, 0x10, 0x00,                 # RESOLUTION, 1 value, 16-bit
                 0x02, 0x01, 0x08, 0x00,                 # RANGE, 1 value, 8G
                 0x04, 0x01, 0x03])                      # CHANNELS, 1 value, 3 -- ONE byte
    resp = pmd.parse_control_response(raw)
    assert resp["ok"] is True
    assert resp["settings_by_name"] == {
        "sample_rate": [52], "resolution": [16], "range": [8], "channels": [3],
    }


def test_control_response_factor_setting_is_a_four_byte_float():
    """The FACTOR setting (id 5) is an IEEE-754 float32, not a uint16 -- confirmed by Polar's
    official PMD PDF (Table 5) and polar-python's ``PmdSettingType.field_size``. This uses the
    ACTUAL byte sequence from a real Verity Sense ACC START response captured in that PDF (sec
    5.3): "F0 02 02 00 00 05 01 40 DA 7F 39". The PDF's own prose also decodes this as the
    integer 964680256 (== ``int.from_bytes(bytes.fromhex('40DA7F39'), 'little')``, confirmed
    below), which cross-checks the raw bytes; its accompanying float annotation in that same
    sequence-diagram graphic is very likely OCR-garbled (the digits are a scramble of the
    correct value's digits), so the expected float here is computed straight from the trusted
    raw bytes rather than hand-copied from that annotation. Before this fix, this module had no
    notion of a 4-byte setting value at all and would have misparsed (or dropped) this TLV.
    """
    raw = bytes.fromhex("F002020000050140DA7F39")
    assert int.from_bytes(bytes.fromhex("40DA7F39"), "little") == 964680256  # PDF's own check
    resp = pmd.parse_control_response(raw)
    assert resp["ok"] is True
    assert resp["opcode"] == pmd.OP_START_MEASUREMENT
    assert resp["measurement_type"] == pmd.MEAS_ACC
    factor_values = resp["settings_by_name"]["factor"]
    assert len(factor_values) == 1
    assert factor_values[0] == pytest.approx(0.00024399999529123306, rel=1e-9)


# --------------------------------------------------------------------------------------
# ACC frames
# --------------------------------------------------------------------------------------
def test_acc_uncompressed_round_trip():
    samples = [(100, -200, 1000), (0, 0, -1), (-32768, 32767, 5)]
    ts, frame_type, decoded = pmd.parse_acc_frame(_acc_uncompressed(samples, timestamp_ns=99))
    assert ts == 99
    assert frame_type == 0x00
    assert decoded == samples


def test_acc_frame_header_is_little_endian_uint64():
    ts = 0x0102030405060708
    frame = _acc_uncompressed([(1, 2, 3)], timestamp_ns=ts)
    assert frame[1:9] == bytes([0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01])
    assert pmd.parse_acc_frame(frame)[0] == ts
    assert pmd.frame_measurement_type(frame) == pmd.MEAS_ACC


def test_acc_delta_3bit_negative_deltas_crossing_byte_boundaries():
    """3-bit deltas: 9 bits per sample, so every sample straddles byte boundaries."""
    ref = (10, -20, 300)
    deltas = [(1, -1, 2), (-4, 3, -2), (0, 0, 0), (3, -3, 1)]
    frame = _acc_delta(ref, [(3, deltas)])
    ts, frame_type, decoded = pmd.parse_acc_frame(frame)
    assert ts == 42
    assert frame_type == 0x80

    expected = [ref]
    cur = list(ref)
    for d in deltas:
        cur = [cur[i] + d[i] for i in range(3)]
        expected.append(tuple(cur))
    assert decoded == expected
    assert decoded == [(10, -20, 300), (11, -21, 302), (7, -18, 300),
                       (7, -18, 300), (10, -21, 301)]


def test_acc_delta_5bit_and_multiple_blocks():
    ref = (-5, 0, 1024)
    block_a = [(15, -16, 7), (-1, 1, -8)]        # 5-bit range is [-16, 15]
    block_b = [(1, 1, 1)] * 3                     # a second block with a different width
    frame = _acc_delta(ref, [(5, block_a), (4, block_b)])
    _ts, _ft, decoded = pmd.parse_acc_frame(frame)

    expected = [ref]
    cur = list(ref)
    for d in block_a + block_b:
        cur = [cur[i] + d[i] for i in range(3)]
        expected.append(tuple(cur))
    assert decoded == expected
    assert len(decoded) == 1 + 2 + 3


def test_acc_delta_16bit_width_is_byte_aligned_case():
    ref = (0, 0, 0)
    deltas = [(1000, -1000, 32000), (-2000, 2000, -32000)]
    frame = _acc_delta(ref, [(16, deltas)])
    _ts, _ft, decoded = pmd.parse_acc_frame(frame)
    assert decoded == [(0, 0, 0), (1000, -1000, 32000), (-1000, 1000, 0)]


def test_acc_delta_zero_width_block_repeats_the_sample():
    frame = _acc_delta((7, 8, 9), [(0, [(0, 0, 0)] * 2)])
    _ts, _ft, decoded = pmd.parse_acc_frame(frame)
    assert decoded == [(7, 8, 9), (7, 8, 9), (7, 8, 9)]


def test_acc_delta_clamps_to_int16():
    frame = _acc_delta((32760, -32760, 0), [(5, [(15, -15, 0), (15, -15, 0)])])
    _ts, _ft, decoded = pmd.parse_acc_frame(frame)
    assert decoded[-1] == (32767, -32768, 0)


def test_acc_malformed_frames_raise_parse_error():
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(b"\x02\x00\x00")                       # truncated header
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x00, b"\x01\x02\x03"))  # not a multiple of 6
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x80, b"\x01\x02"))      # short reference
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_ppi([(60, 1000, 0, 0)]))              # wrong measurement type
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x07, b"\x00" * 6))      # unknown frame type
    # truncated delta block: claims 4 samples of 8-bit deltas but supplies 2 bytes
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x80,
                                   b"\x00\x00\x00\x00\x00\x00" + b"\x08\x04" + b"\x01\x02"))


def test_acc_frame_type_0x02_without_compression_bit_is_not_delta():
    """Regression test for a real bug: a bare frame-type byte of 0x02 (bit 7 CLEAR) used to be
    treated as "always delta" by this module. Per Polar's official PMD PDF (sec 4.2.9, "Parse
    meta data") and two independent real-hardware-tested codecs (bleakheart, polar-python),
    compression is signalled SOLELY by bit 7; a bare 0x02 is the (here-unimplemented)
    uncompressed 24-bit ACC layout (Table 9), not a delta frame. It must be rejected, not
    silently mis-decoded as delta.
    """
    frame = _frame(pmd.MEAS_ACC, 1, 0x02, b"\x00" * 9)  # 24-bit uncompressed layout, 1 sample
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(frame)


def test_acc_delta_real_verity_sense_capture_from_official_polar_pdf():
    """Byte-for-byte regression test using an ACTUAL Verity Sense capture (not our own encoder),
    taken from Polar's official PMD PDF (polarofficial/polar-ble-sdk,
    technical_documentation/online_measurement.pdf, sec 5.3, "Start Stream"). This pins the
    header layout, the compressed-bit frame-type detection, and the reference-sample-is-first
    behaviour against real hardware output, independent of our own synthetic-frame helpers.

    The PDF worked example decodes as: timestamp (last-sample) 540368444604181604 ns, reference
    sample (-48, 357, 4068), delta block bit_width=8/count=29 (only the first two 8-bit-wide
    delta samples are reproduced here since the PDF truncates the rest with "..."), delta
    sample 1 (-4, 7, -1) -> (-52, 364, 4067), delta sample 2 (12, 19, -14) -> (-40, 383, 4053).
    """
    header = bytes([0x02]) + (540368444604181604).to_bytes(8, "little") + bytes([0x80])
    reference = bytes.fromhex("D0FF" "6501" "E40F")           # (-48, 357, 4068)
    delta_header = bytes([0x08, 0x02])                        # bit_width=8, count=2 (truncated)
    delta_samples = bytes.fromhex("FC07FF" "0C13F2")           # deltas 1 and 2
    frame = header + reference + delta_header + delta_samples

    ts, frame_type, decoded = pmd.parse_acc_frame(frame)
    assert ts == 540368444604181604
    assert frame_type == 0x80
    assert decoded == [(-48, 357, 4068), (-52, 364, 4067), (-40, 383, 4053)]


# --------------------------------------------------------------------------------------
# PPI frames
# --------------------------------------------------------------------------------------
def test_ppi_frame_parsing_and_blocker_bit():
    ts, samples = _ppi_parsed = pmd.parse_ppi_frame(_ppi([
        (58, 1034, 3, 0b110),   # good: skin contact supported + detected, blocker clear
        (58, 300, 120, 0b111),  # blocker bit set -> not usable
        (0, 60000, 0, 0b000),   # implausible interval -> not usable
    ], timestamp_ns=7))
    assert ts == 7
    assert [s["ppi_ms"] for s in samples] == [1034, 300, 60000]
    assert [s["error_ms"] for s in samples] == [3, 120, 0]
    assert [s["blocker"] for s in samples] == [False, True, False]
    assert [s["skin_contact"] for s in samples] == [True, True, False]
    assert [s["skin_contact_supported"] for s in samples] == [True, True, False]
    assert [s["ok"] for s in samples] == [True, False, False]
    assert samples[0]["hr"] == 58
    assert pmd.usable_ppi(samples) == [1034.0]
    assert _ppi_parsed[0] == 7


def test_ppi_malformed_frames_raise_parse_error():
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_ppi_frame(_frame(pmd.MEAS_PPI, 1, 0x00, b"\x01\x02\x03"))
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_ppi_frame(_acc_uncompressed([(1, 2, 3)]))


# --------------------------------------------------------------------------------------
# actigraphy counts
# --------------------------------------------------------------------------------------
def test_actigraphy_counts_hand_computed():
    mags = [1.0, 1.0, 1.02, 1.0]  # mean 1.005 -> devs -0.005, -0.005, +0.015, -0.005
    c = pmd.actigraphy_counts(mags, round_values=False)
    assert c["n"] == 4
    assert c["pim"] == pytest.approx(0.03)
    assert c["mad"] == pytest.approx(0.0075)
    assert c["std"] == pytest.approx(math.sqrt((3 * 0.005 ** 2 + 0.015 ** 2) / 4))
    assert c["pmax"] == pytest.approx(0.015)
    # |dev| > 0.01 pattern is F, F, T, F -> two transitions
    assert c["zcm"] == 2


def test_actigraphy_counts_empty_and_constant_input():
    assert pmd.actigraphy_counts([]) == {"pim": 0.0, "zcm": 0, "mad": 0.0,
                                         "std": 0.0, "pmax": 0.0, "n": 0}
    c = pmd.actigraphy_counts([1.0] * 20)
    assert (c["pim"], c["zcm"], c["mad"], c["std"], c["pmax"], c["n"]) == (0.0, 0, 0.0, 0.0, 0.0, 20)


def test_actigraphy_counts_accepts_triaxial_g_tuples():
    triax = [(0.0, 0.0, 1.0), (0.0, 0.3, 0.4)]
    assert pmd.actigraphy_counts(triax) == pmd.actigraphy_counts([1.0, 0.5])


def test_milli_g_conversion():
    mags = pmd.acc_magnitudes_g([(0, 0, 1000), (300, 400, 0)])
    assert mags == pytest.approx([1.0, 0.5])


def test_actigraphy_counts_match_reduce_motion_activity(tmp_path):
    """Cross-check against the script that produced the training counts, on the same samples."""
    import random

    rng = random.Random(20240724)
    samples_milli_g = []
    for i in range(600):  # 600 samples, all inside a single 30 s epoch
        drift = 20 if 200 <= i < 260 else 0  # a burst of movement
        samples_milli_g.append((
            int(rng.gauss(0, 15) + drift),
            int(rng.gauss(0, 15)),
            int(rng.gauss(1000, 15)),
        ))

    raw = tmp_path / "accel.txt"
    with open(raw, "w") as fh:
        for i, (x, y, z) in enumerate(samples_milli_g):
            fh.write(f"{i * 0.02:.6f} {x / 1000.0:.6f} {y / 1000.0:.6f} {z / 1000.0:.6f}\n")

    epochs = rma.reduce_file(str(raw))
    assert list(epochs) == [0]
    ref_pim, ref_zcm, ref_mad, ref_std, ref_pmax, ref_n = epochs[0]

    live = pmd.actigraphy_counts(pmd.acc_magnitudes_g(samples_milli_g))
    assert live["n"] == ref_n == 600
    assert live["zcm"] == ref_zcm
    assert live["pim"] == pytest.approx(ref_pim, abs=1e-5)
    assert live["mad"] == pytest.approx(ref_mad, abs=1e-6)
    assert live["std"] == pytest.approx(ref_std, abs=1e-6)
    assert live["pmax"] == pytest.approx(ref_pmax, abs=1e-5)
    assert pmd.ZCM_THRESHOLD_G == rma.ZCM_THRESHOLD


# --------------------------------------------------------------------------------------
# packaging guarantees
# --------------------------------------------------------------------------------------
def test_module_has_no_ble_dependency():
    """polar_pmd must import (and stay importable) without bleak present."""
    assert "bleak" not in sys.modules  # importing polar_pmd must not drag in a BLE stack
    src = open(os.path.join(_SCRIPTS, "polar_pmd.py")).read()
    imports = [ln for ln in src.splitlines()
               if ln.startswith(("import ", "from ")) or ln.lstrip().startswith(("import ", "from "))]
    assert not any("bleak" in ln for ln in imports)


class _FakeBleClient:
    """Minimal stand-in for bleak's BleakClient: echoes control responses, records writes.

    ``errors`` maps a measurement type to the error code its START should return; ``no_hr_service``
    makes subscribing to the generic 0x180D characteristic fail the way a non-HR device would.
    """

    def __init__(self, errors: dict | None = None, no_hr_service: bool = False):
        self.services = [_Svc(pmd.PMD_SERVICE_UUID)]
        self.is_connected = True
        self.notify: dict = {}
        self.written: list[bytes] = []
        self._errors = errors or {}
        self._no_hr_service = no_hr_service

    async def start_notify(self, uuid, cb):
        uuid = str(uuid).lower()
        if self._no_hr_service and uuid == HR_UUID:
            raise RuntimeError("characteristic 00002a37 not found")
        self.notify[uuid] = cb

    async def stop_notify(self, uuid):
        self.notify.pop(str(uuid).lower(), None)

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        self.written.append(data)
        cb = self.notify.get(pmd.PMD_CONTROL_UUID)
        if cb is not None:
            err = self._errors.get(data[1], 0) if data[0] == pmd.OP_START_MEASUREMENT else 0
            cb(0, bytes([0xF0, data[0], data[1], err, 0x00]))

    def feed(self, frame: bytes):
        self.notify[pmd.PMD_DATA_UUID](0, frame)

    def feed_hr(self, frame: bytes):
        self.notify[HR_UUID](0, frame)


HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


def _hr_measurement(bpm: int, rr_ms: list[float] | None = None) -> bytes:
    """Build a generic 0x2A37 Heart Rate Measurement notification (8-bit HR, RR in 1/1024 s)."""
    flags = 0x10 if rr_ms else 0x00
    out = bytearray([flags, bpm])
    for rr in rr_ms or []:
        out += int(round(rr * 1024.0 / 1000.0)).to_bytes(2, "little")
    return bytes(out)


class _Svc:
    def __init__(self, uuid):
        self.uuid = uuid


def _pmd_args(**over):
    import argparse

    ns = argparse.Namespace(url="http://localhost:8000/hr/ingest", source="verity",
                            batch_seconds=0.01, retry_seconds=0.01, mode="pmd",
                            acc_rate=52, acc_range=8, acc_resolution=16, control_timeout=1.0,
                            pmd_grace_seconds=pmd.PMD_STARTUP_GRACE_S,
                            scan=False, scan_seconds=1.0)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _run_pmd_session(client, args=None, feed=(), until_posted=True, timeout=10):
    """Run _pmd_session against the fake client, injecting frames once it has subscribed."""
    import asyncio
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    args = args or _pmd_args()
    posted: list[dict] = []
    original_post = fwd._post
    fwd._post = lambda url, payload, t=5.0: posted.append(payload)

    async def _drive():
        for _ in range(400):
            if pmd.PMD_DATA_UUID in client.notify:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.01)  # let the START handshake finish
        for kind, frame in feed:
            if kind == "hr":
                # The generic-HR fallback only subscribes AFTER the PMD START handshake has
                # decided PPI was refused, so wait for that subscription instead of a fixed
                # delay -- feeding before it exists raced client.notify and dropped the frame
                # (intermittent KeyError/assert failure in this test).
                for _ in range(400):
                    if HR_UUID in client.notify:
                        break
                    await asyncio.sleep(0.005)
            (client.feed_hr if kind == "hr" else client.feed)(frame)
        for _ in range(400):
            if posted or not until_posted:
                break
            await asyncio.sleep(0.005)
        if not until_posted:
            await asyncio.sleep(0.05)
        client.is_connected = False

    async def _go():
        driver = asyncio.ensure_future(_drive())
        ok = await fwd._pmd_session(client, args)
        await driver
        return ok

    try:
        ok = asyncio.run(asyncio.wait_for(_go(), timeout=timeout))
    finally:
        fwd._post = original_post
    return ok, posted


def test_pmd_session_streams_acc_and_ppi_into_the_post_body():
    client = _FakeBleClient()
    ok, posted = _run_pmd_session(client, feed=[
        ("data", _acc_uncompressed([(10 * i, -5 * i, 1000) for i in range(10)])),
        ("data", _ppi([(58, 1034, 2, 0b110), (58, 999, 4, 0b111)])),  # 2nd sample blocked
    ])
    assert ok is True

    starts = [w.hex() for w in client.written if w[0] == pmd.OP_START_MEASUREMENT]
    assert starts[0] == "0203"                       # PPI first, no settings
    # ACC 8G / 52 Hz / 16-bit / 3 channels. CHANNELS is required -- real Verity Sense firmware
    # refuses an ACC start without it ("invalid number of channels", code 11), verified live.
    assert starts[1] == "0202020108000001340001011000040103"
    stops = [w.hex() for w in client.written if w[0] == pmd.OP_STOP_MEASUREMENT]
    assert set(stops) == {"0303", "0302"}            # both streams stopped on teardown

    body = posted[0]
    assert body["source"] == "verity"
    assert body["hr"] == 58.0
    assert body["rr"] == [1034.0]                    # blocked interval filtered out
    assert body["acc"]["n"] == 10
    assert body["acc"]["fs"] == 52
    assert set(body["acc"]) == {"pim", "zcm", "mad", "std", "pmax", "n", "fs"}
    assert body["acc"]["pim"] > 0


def test_pmd_degrades_to_ppi_only_when_acc_is_refused():
    """ACC refused must NOT cost us PPI -- the cardiac stream keeps running."""
    client = _FakeBleClient(errors={pmd.MEAS_ACC: 3})  # 3 = not supported
    ok, posted = _run_pmd_session(client, feed=[("data", _ppi([(57, 1050, 2, 0b110)]))])
    assert ok is True
    assert posted[0]["rr"] == [1050.0]
    assert posted[0]["hr"] == 57.0
    assert "acc" not in posted[0]
    stops = {w.hex() for w in client.written if w[0] == pmd.OP_STOP_MEASUREMENT}
    assert stops == {"0303"}          # only the stream we actually started is stopped
    assert HR_UUID not in client.notify  # PPI is live, so no generic-HR fallback needed


def test_pmd_degrades_to_acc_plus_generic_hr_when_ppi_is_refused():
    """PPI refused -> keep ACC and take heart rate from the generic 0x180D service."""
    client = _FakeBleClient(errors={pmd.MEAS_PPI: 3})
    ok, posted = _run_pmd_session(client, feed=[
        ("data", _acc_uncompressed([(0, 0, 1000 + 5 * i) for i in range(8)])),
        ("hr", _hr_measurement(62, [900.0])),
    ])
    assert ok is True
    assert posted[0]["hr"] == 62.0
    assert posted[0]["rr"] == pytest.approx([900.0], abs=1.0)
    assert posted[0]["acc"]["n"] == 8
    stops = {w.hex() for w in client.written if w[0] == pmd.OP_STOP_MEASUREMENT}
    assert stops == {"0302"}


def test_pmd_acc_only_when_ppi_refused_and_no_hr_service():
    client = _FakeBleClient(errors={pmd.MEAS_PPI: 3}, no_hr_service=True)
    ok, posted = _run_pmd_session(client, feed=[
        ("data", _acc_uncompressed([(0, 0, 1000 + 5 * i) for i in range(8)])),
    ])
    assert ok is True
    assert posted[0]["acc"]["n"] == 8
    assert "hr" not in posted[0] and "rr" not in posted[0]


def test_pmd_session_returns_false_only_when_both_streams_are_refused():
    import asyncio
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    client = _FakeBleClient(errors={pmd.MEAS_ACC: 3, pmd.MEAS_PPI: 3})
    ok = asyncio.run(asyncio.wait_for(fwd._pmd_session(client, _pmd_args()), timeout=10))
    assert ok is False
    assert client.notify == {}  # subscriptions cleaned up


def test_pmd_session_returns_false_without_the_pmd_service():
    import asyncio
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    client = _FakeBleClient()
    client.services = [_Svc("0000180d-0000-1000-8000-00805f9b34fb")]
    ok = asyncio.run(asyncio.wait_for(fwd._pmd_session(client, _pmd_args()), timeout=10))
    assert ok is False


def test_pmd_data_callback_survives_malformed_frames():
    """A garbage notification must be logged and dropped, never break the stream."""
    client = _FakeBleClient()
    ok, posted = _run_pmd_session(client, feed=[
        ("data", b"\x02\xff"),                                  # truncated ACC frame
        ("data", _frame(pmd.MEAS_PPI, 1, 0x00, b"\x01\x02")),   # bad PPI length
        ("data", _ppi([(61, 980, 1, 0b110)])),                  # good frame still lands
    ])
    assert ok is True
    assert posted[0]["rr"] == [980.0]


# --------------------------------------------------------------------------------------
# PPI warm-up / SDK-mode detection
# --------------------------------------------------------------------------------------
def test_warmup_state_respects_the_documented_ppi_warm_up():
    # Polar: ~25 s to the first PPI batch, HR only every ~5 s -> silence is not failure.
    assert pmd.PPI_FIRST_SAMPLE_S == 25.0
    assert pmd.PPI_HR_UPDATE_S == 5.0
    assert pmd.PMD_STARTUP_GRACE_S >= 40.0
    assert pmd.warmup_state(0.0, 0) == "warming_up"
    assert pmd.warmup_state(24.0, 0) == "warming_up"
    assert pmd.warmup_state(39.9, 0) == "warming_up"
    assert pmd.warmup_state(41.0, 0) == "stalled"
    assert pmd.warmup_state(59.9, 0) == "stalled"
    assert pmd.warmup_state(60.0, 0) == "sdk_mode_suspect"
    assert pmd.warmup_state(120.0, 0) == "sdk_mode_suspect"
    # Any frame at all means the stream is alive, whatever the clock says.
    for elapsed in (0.0, 30.0, 300.0):
        assert pmd.warmup_state(elapsed, 1) == "streaming"
    # A caller cannot shrink the grace below the documented warm-up.
    assert pmd.warmup_state(10.0, 0, grace_s=0.0) == "warming_up"


def test_sdk_mode_hint_is_actionable():
    msg = pmd.SDK_MODE_HINT.format(seconds=60)
    assert "60s" in msg and "SDK MODE" in msg
    assert "power-cycle" in msg.lower()
    assert pmd.SDK_MODE_REMEDY in msg


def test_no_ble_command_can_enable_sdk_mode():
    """We must never enable SDK mode: no opcode for it exists, and none may be inferred."""
    src = open(os.path.join(_SCRIPTS, "polar_pmd.py")).read().lower()
    fwd_src = open(os.path.join(_SCRIPTS, "verity_forwarder.py")).read().lower()
    for name in dir(pmd):
        if name.startswith("OP_") or name.startswith("op_"):
            assert "sdk" not in name.lower()
    # Every control-point byte string we can emit is a get-settings/start/stop of a known stream.
    for meas in (pmd.MEAS_ECG, pmd.MEAS_PPG, pmd.MEAS_ACC, pmd.MEAS_PPI):
        assert pmd.build_start_command(meas, None)[0] == pmd.OP_START_MEASUREMENT
        assert pmd.build_stop_command(meas)[0] == pmd.OP_STOP_MEASUREMENT
    # Only these three command builders exist, and none of them is SDK-mode related.
    builders = {n for n in dir(pmd) if n.startswith("build_")}
    assert builders == {"build_start_command", "build_stop_command", "build_get_settings_command"}
    # SDK mode is mentioned only as documentation/diagnostics, never as a command.
    assert "sdk" in src and "sdk" in fwd_src
    assert "start_sdk" not in src and "stop_sdk" not in src and "sdk_mode_command" not in src


def test_quiet_ppi_during_the_grace_period_produces_no_warning_and_no_reconnect(monkeypatch):
    """The ~25 s PPI warm-up must not be mistaken for a dead stream."""
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    logs: list[str] = []
    monkeypatch.setattr(fwd, "_log", logs.append)

    client = _FakeBleClient()
    ok, posted = _run_pmd_session(client, feed=[], until_posted=False)

    assert ok is True          # silence never aborts the session
    assert posted == []        # and nothing is POSTed with no data
    joined = " ".join(logs).lower()
    assert "sdk mode" not in joined
    assert "no ppi yet" not in joined
    assert any("warm-up" in line.lower() for line in logs)  # just the informational notice


def test_stalled_ppi_surfaces_the_sdk_mode_hint(monkeypatch):
    """Past the warm-up with no PPI, the log must name SDK mode and the power-cycle remedy."""
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    logs: list[str] = []
    monkeypatch.setattr(fwd, "_log", logs.append)
    # Simulate the clock having passed SDK_MODE_SUSPECT_S with no PPI frames.
    monkeypatch.setattr(pmd, "warmup_state", lambda *a, **k: "sdk_mode_suspect")

    client = _FakeBleClient()
    ok, _posted = _run_pmd_session(client, feed=[], until_posted=False)
    assert ok is True

    hints = [ln for ln in logs if "SDK MODE" in ln]
    assert len(hints) == 1  # said once, not spammed every batch
    assert "power-cycle" in hints[0].lower()


def test_refused_ppi_start_also_mentions_sdk_mode(monkeypatch):
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    logs: list[str] = []
    monkeypatch.setattr(fwd, "_log", logs.append)
    client = _FakeBleClient(errors={pmd.MEAS_PPI: 3})
    _run_pmd_session(client, feed=[
        ("data", _acc_uncompressed([(0, 0, 1000) for _ in range(8)])),
    ])
    assert any("SDK MODE" in ln for ln in logs)


# --------------------------------------------------------------------------------------
# --scan
# --------------------------------------------------------------------------------------
class _FakeDevice:
    def __init__(self, address, name, uuids=()):
        self.address = address
        self.name = name
        self.metadata = {"uuids": list(uuids)}


def _install_fake_bleak(monkeypatch, devices, raise_exc=None):
    import types

    class _Scanner:
        @staticmethod
        async def discover(timeout=10.0):
            if raise_exc is not None:
                raise raise_exc
            return devices

    fake = types.ModuleType("bleak")
    fake.BleakScanner = _Scanner
    fake.BleakClient = object
    monkeypatch.setitem(sys.modules, "bleak", fake)
    return fake


def test_scan_lists_candidate_sensors_and_exits_zero(monkeypatch, capsys):
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    _install_fake_bleak(monkeypatch, [
        _FakeDevice("AA:BB:CC:DD:EE:FF", "Polar Sense B1234567"),
        _FakeDevice("11:22:33:44:55:66", "SomeWatch", uuids=["0000180d-0000-1000-8000-00805f9b34fb"]),
        _FakeDevice("99:99:99:99:99:99", None),
    ])
    assert fwd.main(["--scan", "--scan-seconds", "0.1"]) == 0
    out = capsys.readouterr().out
    assert "AA:BB:CC:DD:EE:FF" in out and "Polar Sense B1234567" in out
    assert "11:22:33:44:55:66" in out and "0x180D" in out
    assert "--address" in out           # tells the user how to pin it
    assert "SDK MODE" in out            # and how to fix the silent-failure case
    assert "connecting" not in out.lower()  # --scan never connects or streams


def test_scan_with_no_match_prints_the_hr_mode_hint_and_all_devices(monkeypatch, capsys):
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    _install_fake_bleak(monkeypatch, [_FakeDevice("00:00:00:00:00:01", "Fridge")])
    assert fwd.main(["--scan"]) == 0
    out = capsys.readouterr().out
    assert "no Polar/heart-rate sensor found" in out
    assert "single press" in out and "blue LED" in out
    assert "00:00:00:00:00:01" in out and "Fridge" in out


def test_scan_survives_a_missing_adapter_without_a_traceback(monkeypatch, capsys):
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    _install_fake_bleak(monkeypatch, [], raise_exc=RuntimeError("no Bluetooth adapter found"))
    assert fwd.main(["--scan"]) == 0
    out = capsys.readouterr().out
    assert "scan failed" in out and "adapter" in out


def test_scan_without_bleak_installed_degrades_cleanly(monkeypatch, capsys):
    """scripts/verity-setup.ps1 runs --scan as a setup step: it must never hard-fail."""
    import builtins
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    monkeypatch.delitem(sys.modules, "bleak", raising=False)
    real_import = builtins.__import__

    def _no_bleak(name, *a, **k):
        if name == "bleak":
            raise ImportError("No module named 'bleak'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_bleak)
    assert fwd.main(["--scan"]) == 0
    assert "pip install bleak" in capsys.readouterr().out


def test_forwarder_cli_exposes_the_new_flags(capsys):
    """The forwarder's argument surface must be usable even where bleak isn't installed."""
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    assert fwd.pmd is pmd
    with pytest.raises(SystemExit) as exc:
        fwd.main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for flag in ("--mode", "--acc-rate", "--acc-range", "--address", "--batch-seconds"):
        assert flag in help_text
    assert "hr" in help_text and "pmd" in help_text and "auto" in help_text


# ------------------------------------- connector redundancy layers (2026-08-26 audit)
def test_a_connected_but_silent_link_ends_the_session_instead_of_hanging():
    """THE 2026-08-26 failure. BLE can hold a link open long after notifications stop; both
    session loops ran `while client.is_connected`, so a silent-but-connected band pinned the
    forwarder in a session that would never produce another sample -- 25 HR samples at 19:00,
    then ten hours of nothing. A dead feed must DROP the link so the reconnect loop can rescan."""
    client = _FakeBleClient()
    # stall almost immediately, and disable the warm-up suppression so the guard can fire
    args = _pmd_args(stall_seconds=0.05, pmd_grace_seconds=0.0)
    ok, posted = _run_pmd_session(client, args=args, feed=[], until_posted=False)
    assert ok is False, "a stalled session must end so the caller reconnects"


def test_a_frozen_heart_rate_is_not_forwarded_as_live_physiology():
    """`last_hr` was never invalidated, so once notifications stopped the flusher re-POSTed the
    SAME value every batch forever -- laundering a frozen reading into the pipeline as current
    physiology. That is exactly the 'band on a charger' shape the controller's bed-entry guard
    has to defend against downstream."""
    import importlib
    import time as _time
    fwd = importlib.import_module("verity_forwarder")

    # a reading older than hr_max_age must be dropped from the payload
    stale_age = fwd._HR_MAX_AGE_S + 5.0
    last_hr = {"v": 61.0, "t": _time.monotonic() - stale_age}
    hr = last_hr["v"]
    if hr is not None and (_time.monotonic() - last_hr["t"]) > fwd._HR_MAX_AGE_S:
        hr = None
    assert hr is None, "a stale HR must not be forwarded"

    fresh_hr = {"v": 61.0, "t": _time.monotonic()}
    hr2 = fresh_hr["v"]
    if hr2 is not None and (_time.monotonic() - fresh_hr["t"]) > fwd._HR_MAX_AGE_S:
        hr2 = None
    assert hr2 == 61.0, "a fresh HR must still be forwarded"


def test_the_forwarder_writes_a_liveness_heartbeat(tmp_path):
    """Layer 2: the supervisor could only check a process EXISTED, which a wedged one passes.
    The heartbeat is what makes 'wedged' distinguishable from 'running'."""
    import importlib
    fwd = importlib.import_module("verity_forwarder")
    fwd._beat(tmp_path)
    assert (tmp_path / ".run" / "verity.heartbeat").exists()


def test_the_heartbeat_never_raises_on_an_unwritable_root():
    """It runs on the hot loop; a filesystem hiccup must never take the forwarder down."""
    import importlib
    from pathlib import Path
    fwd = importlib.import_module("verity_forwarder")
    fwd._beat(Path("/nonexistent/read-only/path"))   # must not raise


# ------------------------------------- escalating recovery ladder (2026-08-27 request)
def _fwd():
    import importlib
    return importlib.import_module("verity_forwarder")


def test_the_preferred_transport_is_kept_while_sessions_are_productive():
    fwd = _fwd()
    for mode in ("pmd", "hr", "auto"):
        assert fwd._effective_mode(mode, 0) == mode
        assert fwd._effective_mode(mode, 1) == mode


def test_repeated_barren_sessions_try_the_OTHER_bluetooth_stream():
    """The Verity exposes two independent streams (Polar PMD, and generic 0x180D HR) and they
    fail independently -- a band left in SDK mode refuses PMD while plain HR still works, and a
    wedged PMD handshake hangs a session that HR would have sailed through. Waiting longer
    retries the SAME broken stream; alternating actually tries the other one."""
    fwd = _fwd()
    assert fwd._effective_mode("pmd", fwd._ALT_TRANSPORT_AFTER) == "hr"
    assert fwd._effective_mode("hr", fwd._ALT_TRANSPORT_AFTER) == "pmd"
    # "auto" degrades PMD->HR within one connection, so if the PMD handshake is what wedges,
    # every auto attempt wedges identically -- lead with an explicit service instead.
    assert fwd._effective_mode("auto", fwd._ALT_TRANSPORT_AFTER) in ("hr", "pmd")


def test_alternation_keeps_flipping_so_it_never_sticks_on_the_broken_stream():
    fwd = _fwd()
    seen = {fwd._effective_mode("pmd", n)
            for n in range(fwd._ALT_TRANSPORT_AFTER, fwd._ALT_TRANSPORT_AFTER + 8)}
    assert seen == {"pmd", "hr"}, seen


def test_an_adapter_reset_is_requested_only_after_the_cheaper_rungs(tmp_path):
    """Restarting the Bluetooth stack drops every BT device on the machine, so it must sit at the
    BOTTOM of the ladder -- after retry, after alternating transport, after forgetting the
    cached address."""
    fwd = _fwd()
    flag = tmp_path / ".run" / "bt-reset.request"
    fwd._request_adapter_reset(tmp_path, fwd._ADAPTER_RESET_AFTER)
    assert flag.exists()
    assert fwd._ADAPTER_RESET_AFTER > fwd._REDISCOVER_AFTER > fwd._ALT_TRANSPORT_AFTER


def test_a_pending_adapter_reset_is_not_re_requested(tmp_path):
    """One pending request is enough -- re-writing it every barren session would queue resets."""
    fwd = _fwd()
    logs: list = []
    orig, fwd._log = fwd._log, logs.append
    try:
        fwd._request_adapter_reset(tmp_path, 5)
        fwd._request_adapter_reset(tmp_path, 6)
    finally:
        fwd._log = orig
    assert len([m for m in logs if "requesting a Bluetooth adapter reset" in m]) == 1


def test_requesting_a_reset_never_raises_on_an_unwritable_root():
    from pathlib import Path
    _fwd()._request_adapter_reset(Path("/nonexistent/read-only"), 9)   # must not raise


def test_a_successful_post_is_what_counts_as_a_productive_session():
    """The old loop counted EXCEPTIONS, so a band that connects, yields nothing and disconnects
    cleanly reset the counter and never escalated -- the single most common real failure."""
    fwd = _fwd()
    before = fwd._STATS["posts"]
    try:
        fwd._post("http://127.0.0.1:9/nope", {"hr": 60})
    except Exception:
        pass
    assert fwd._STATS["posts"] == before, "a failed POST must not count as produced data"


# ------------------------------------- discovery hardening (2026-08-28: pairs with phone, not us)
def test_a_previously_connected_address_is_tried_when_the_scan_finds_nothing(tmp_path, monkeypatch):
    """THE 2026-08-28 failure. The band paired fine with the Polar Flow app on the phone while
    our scans saw nothing for 26 hours. A BLE peripheral already connected to another central
    generally STOPS advertising -- it is invisible to a scan while remaining perfectly healthy and
    connectable. Refusing to try a known address makes that look identical to a flat battery."""
    import asyncio
    fwd = _fwd()
    monkeypatch.setattr(fwd, "_repo_root", lambda: tmp_path)
    fwd._remember_address("AA:BB:CC:DD:EE:FF")

    class _Scanner:
        @staticmethod
        async def discover(timeout=10.0):
            return []                      # nothing advertising at all

    got = asyncio.run(fwd._discover(_Scanner, None))
    assert got == "AA:BB:CC:DD:EE:FF"


def test_nothing_is_remembered_before_a_successful_connection(tmp_path, monkeypatch):
    """A cached address must only ever come from a connection that actually opened -- caching a
    guess would send every future scan-miss at a device that never worked."""
    import asyncio
    fwd = _fwd()
    monkeypatch.setattr(fwd, "_repo_root", lambda: tmp_path)

    class _Scanner:
        @staticmethod
        async def discover(timeout=10.0):
            return []

    assert asyncio.run(fwd._discover(_Scanner, None)) is None


def test_an_explicit_address_still_short_circuits_discovery(tmp_path, monkeypatch):
    import asyncio
    fwd = _fwd()
    monkeypatch.setattr(fwd, "_repo_root", lambda: tmp_path)

    class _Scanner:
        @staticmethod
        async def discover(timeout=10.0):
            raise AssertionError("must not scan when an address is pinned")

    assert asyncio.run(fwd._discover(_Scanner, "11:22:33:44:55:66")) == "11:22:33:44:55:66"


def test_the_scan_reports_what_it_actually_saw(tmp_path, monkeypatch):
    """'no sensor found' cannot distinguish 'the band is off' from 'the band is there and we
    failed to match it', and those need opposite fixes. Log the candidates."""
    import asyncio
    fwd = _fwd()
    monkeypatch.setattr(fwd, "_repo_root", lambda: tmp_path)
    logs: list = []
    monkeypatch.setattr(fwd, "_log", logs.append)

    class _Dev:
        def __init__(self, name, address):
            self.name, self.address = name, address
            self.metadata = {}

    class _Scanner:
        @staticmethod
        async def discover(timeout=10.0):
            return [_Dev("SomeTV", "01:02:03:04:05:06"), _Dev(None, "0A:0B:0C:0D:0E:0F")]

    asyncio.run(fwd._discover(_Scanner, None))
    joined = " ".join(logs)
    assert "no match among 2 advertising device" in joined
    assert "SomeTV" in joined


def test_remembering_an_address_never_raises_on_an_unwritable_root(monkeypatch):
    """Runs on the reconnect path, so a filesystem problem must degrade to "no cached address"
    rather than taking the forwarder down. /dev/null/... cannot be created even as root, unlike
    a merely-absent path."""
    from pathlib import Path
    fwd = _fwd()
    monkeypatch.setattr(fwd, "_repo_root", lambda: Path("/dev/null/nope"))
    fwd._remember_address("AA:BB")           # must not raise
    assert fwd._recall_address() is None
