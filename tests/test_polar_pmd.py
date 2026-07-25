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
    return _frame(pmd.MEAS_ACC, timestamp_ns, 0x02, bytes(payload))


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
    assert frame_type == 0x02

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
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x02, b"\x01\x02"))      # short reference
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_ppi([(60, 1000, 0, 0)]))              # wrong measurement type
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x07, b"\x00" * 6))      # unknown frame type
    # truncated delta block: claims 4 samples of 8-bit deltas but supplies 2 bytes
    with pytest.raises(pmd.PmdParseError):
        pmd.parse_acc_frame(_frame(pmd.MEAS_ACC, 1, 0x02,
                                   b"\x00\x00\x00\x00\x00\x00" + b"\x08\x04" + b"\x01\x02"))


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
    """Minimal stand-in for bleak's BleakClient: echoes control responses, records writes."""

    def __init__(self, error_code: int = 0):
        self.services = [_Svc(pmd.PMD_SERVICE_UUID)]
        self.is_connected = True
        self.notify: dict = {}
        self.written: list[bytes] = []
        self._error_code = error_code

    async def start_notify(self, uuid, cb):
        self.notify[str(uuid).lower()] = cb

    async def stop_notify(self, uuid):
        self.notify.pop(str(uuid).lower(), None)

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        self.written.append(data)
        cb = self.notify.get(pmd.PMD_CONTROL_UUID)
        if cb is not None:
            err = self._error_code if data[0] == pmd.OP_START_MEASUREMENT else 0
            cb(0, bytes([0xF0, data[0], data[1], err, 0x00]))

    def feed(self, frame: bytes):
        self.notify[pmd.PMD_DATA_UUID](0, frame)


class _Svc:
    def __init__(self, uuid):
        self.uuid = uuid


def _pmd_args(**over):
    import argparse

    ns = argparse.Namespace(url="http://localhost:8000/hr/ingest", source="verity",
                            batch_seconds=0.01, retry_seconds=0.01, mode="pmd",
                            acc_rate=52, acc_range=8, acc_resolution=16, control_timeout=1.0)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_pmd_session_streams_acc_and_ppi_into_the_post_body(monkeypatch):
    import asyncio
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    posted: list[dict] = []
    monkeypatch.setattr(fwd, "_post", lambda url, payload, timeout=5.0: posted.append(payload))

    client = _FakeBleClient()

    async def _drive():
        for _ in range(200):  # wait for the data subscription, then inject frames
            if pmd.PMD_DATA_UUID in client.notify:
                break
            await asyncio.sleep(0.005)
        client.feed(_acc_uncompressed([(10 * i, -5 * i, 1000) for i in range(10)]))
        client.feed(_ppi([(58, 1034, 2, 0b110), (58, 999, 4, 0b111)]))  # 2nd sample blocked
        for _ in range(200):
            if posted:
                break
            await asyncio.sleep(0.005)
        client.is_connected = False

    async def _go():
        driver = asyncio.ensure_future(_drive())
        ok = await fwd._pmd_session(client, _pmd_args())
        await driver
        return ok

    ok = asyncio.run(asyncio.wait_for(_go(), timeout=10))
    assert ok is True

    starts = [w.hex() for w in client.written if w[0] == pmd.OP_START_MEASUREMENT]
    assert starts[0] == "0203"                       # PPI first, no settings
    assert starts[1] == "0202020108000001340001011000"  # ACC 8G / 52 Hz / 16-bit
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


def test_pmd_session_returns_false_when_a_stream_is_refused():
    """A non-zero control-point error must make auto mode fall back to the HR service."""
    import asyncio
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    client = _FakeBleClient(error_code=3)  # "not supported"
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


def test_pmd_data_callback_survives_malformed_frames(monkeypatch):
    """A garbage notification must be logged and dropped, never break the stream."""
    import asyncio
    import importlib

    fwd = importlib.import_module("verity_forwarder")
    posted: list[dict] = []
    monkeypatch.setattr(fwd, "_post", lambda url, payload, timeout=5.0: posted.append(payload))
    client = _FakeBleClient()

    async def _drive():
        for _ in range(200):
            if pmd.PMD_DATA_UUID in client.notify:
                break
            await asyncio.sleep(0.005)
        client.feed(b"\x02\xff")                                  # truncated ACC frame
        client.feed(_frame(pmd.MEAS_PPI, 1, 0x00, b"\x01\x02"))   # bad PPI length
        client.feed(_ppi([(61, 980, 1, 0b110)]))                  # good frame still lands
        for _ in range(200):
            if posted:
                break
            await asyncio.sleep(0.005)
        client.is_connected = False

    async def _go():
        driver = asyncio.ensure_future(_drive())
        ok = await fwd._pmd_session(client, _pmd_args())
        await driver
        return ok

    assert asyncio.run(asyncio.wait_for(_go(), timeout=10)) is True
    assert posted[0]["rr"] == [980.0]


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
