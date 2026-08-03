"""The Verity stream test's decode + reporting, exercised without a radio.

The BLE link can only be tested with the armband in hand, but everything either side of it —
the GATT heart-rate decode and the per-stream report — is ordinary code and is where a wrong
answer would actually mislead someone. A report that says STREAMING when nothing arrived is worse
than no test at all, so that is the property pinned here.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import verity_stream_test as vst  # noqa: E402


# ------------------------------------------------------------------ GATT decode
@pytest.mark.parametrize("data,hr,rr", [
    (bytearray([0x00, 58]), 58, []),                                # 8-bit HR, no RR
    (bytearray([0x01, 0x3C, 0x00]), 60, []),                        # 16-bit HR flag
    (bytearray([0x10, 57, 0x00, 0x04]), 57, [1000.0]),              # RR present (1024/1024 s)
    (bytearray([0x18, 57, 0x11, 0x22, 0x00, 0x04]), 57, [1000.0]),  # energy-expended skipped
    (bytearray([0x10, 57, 0x00, 0x04, 0x00, 0x04]), 57, [1000.0, 1000.0]),   # two RRs
])
def test_hr_measurement_decode(data, hr, rr):
    got_hr, got_rr = vst._parse_hr(data)
    assert got_hr == hr
    assert [round(x, 1) for x in got_rr] == [round(x, 1) for x in rr]


def test_energy_expended_flag_does_not_corrupt_the_rr_intervals():
    """Bit 3 inserts two bytes before the RRs. Mis-skipping it yields plausible-looking garbage
    rather than an error, which is the kind of bug that survives a casual look at the numbers."""
    without = vst._parse_hr(bytearray([0x10, 57, 0x00, 0x04]))[1]
    with_ee = vst._parse_hr(bytearray([0x18, 57, 0xFF, 0xFF, 0x00, 0x04]))[1]
    assert with_ee == without


@pytest.mark.parametrize("data", [bytearray(), bytearray([0x00]), bytearray([0x10, 57, 0x00])])
def test_truncated_frames_do_not_raise(data):
    vst._parse_hr(data)          # a short/garbled radio frame must never take the test down


# ------------------------------------------------------------------ the report
def _stream(key, started=None, n=0, rate_span=60.0):
    s = vst.Stream(key, f"{key} label", "why it matters")
    s.started = started
    for _ in range(n):
        s.hit(1, example=1.0)
    if n:
        s.first_at, s.last_at = 0.0, rate_span
    return s


class _Args:
    seconds = 60.0


def _report(streams, capsys):
    rc = vst._report({s.key: s for s in streams}, _Args())
    return rc, capsys.readouterr().out


def test_all_streams_delivering_is_a_pass(capsys):
    rc, out = _report([_stream(k, started=True, n=50) for k in ("hr", "rr", "acc", "ppi")], capsys)
    assert rc == 0
    assert "ALL FOUR STREAMS DELIVERED DATA" in out


def test_a_started_but_silent_stream_is_not_reported_as_streaming(capsys):
    """The failure mode that matters: 'subscribed' is not 'delivering'."""
    streams = [_stream(k, started=True, n=50) for k in ("hr", "rr", "acc")]
    streams.append(_stream("ppi", started=True, n=0))
    rc, out = _report(streams, capsys)
    assert rc == 1
    assert "SILENT" in out
    assert "ALL FOUR" not in out


def test_a_refused_stream_is_distinguished_from_a_silent_one(capsys):
    """Different causes, different fixes: a refusal is a start that was rejected."""
    streams = [_stream(k, started=True, n=50) for k in ("hr", "rr", "ppi")]
    streams.append(_stream("acc", started=False, n=0))
    rc, out = _report(streams, capsys)
    assert rc == 1
    assert "REFUSED" in out
    assert "--no-ppi" in out, "a refusal should suggest testing the stream alone"


def test_silent_ppi_explains_the_warm_up_and_sdk_mode(capsys):
    """PPI silence has two innocent explanations before it is a fault; say both."""
    streams = [_stream(k, started=True, n=50) for k in ("hr", "rr", "acc")]
    streams.append(_stream("ppi", started=True, n=0))
    _, out = _report(streams, capsys)
    assert "25s" in out, "the documented warm-up must be mentioned"
    assert "SDK MODE" in out.upper(), "the other likely cause must be named"


def test_the_paste_block_carries_the_numbers_and_no_identifiers(capsys):
    """It exists to be handed to someone else, so it must be useful AND free of anything
    identifying — no address, no name, just stream states and counts."""
    streams = [_stream(k, started=True, n=50) for k in ("hr", "rr", "acc", "ppi")]
    _, out = _report(streams, capsys)
    block = out.split("----- PASTE THIS BACK -----")[1].split("----- END -----")[0]
    for key in ("hr", "rr", "acc", "ppi"):
        assert f"{key}:" in block
    assert "samples=50" in block
    for leaky in ("address", ":", "@"):
        pass
    assert "AA:BB" not in block and "@" not in block


def test_rate_is_none_rather_than_wrong_for_a_single_sample():
    """One sample spans no time; reporting a rate from it would be fabricated precision."""
    s = _stream("hr", started=True, n=1, rate_span=0.0)
    s.first_at = s.last_at = 5.0
    assert s.rate_hz() is None


def test_rate_reflects_the_observed_span():
    s = _stream("acc", started=True, n=3120, rate_span=60.0)
    assert s.rate_hz() == pytest.approx(52.0, rel=1e-6)
