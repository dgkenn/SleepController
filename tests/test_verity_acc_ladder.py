"""Every PMD session since 2026-08-31 started ACC successfully and then delivered nothing.
After a stall where the ACCELEROMETER ALONE was silent, the next session asks for less --
and a rate the band refuses outright is dropped from the ladder rather than costing the
accelerometer (2026-09-20: 52 -> 26 Hz after a link drop, the Verity refused 26 Hz with
"invalid sample rate", and the accelerometer was off from 22:37 until morning)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polar_pmd as pmd  # noqa: E402
import verity_forwarder as vf  # noqa: E402

import pytest


@pytest.fixture(autouse=True)
def _isolated_rung(monkeypatch, tmp_path):
    """Module-global forwarder state must not leak into other test files."""
    monkeypatch.setitem(vf._STATS, "acc_rung", 0)
    monkeypatch.setitem(vf._STATS, "pmd_stall", None)
    monkeypatch.setitem(vf._STATS, "acc_unsupported", set())
    monkeypatch.setattr(vf, "_repo_root", lambda: tmp_path)
    yield
    vf._STATS.pop("pmd_stall", None)
    vf._STATS.pop("pmd_streamed_s", None)
    vf._STATS["acc_unsupported"] = set()


from test_verity_pmd_stale_stream import FakeClient, _starts  # noqa: E402


class _Args:
    acc_rate = 52
    acc_rate_cli = 52
    acc_resolution = 16
    acc_range = 8
    control_timeout = 1.0
    batch_seconds = 0.01
    url = "http://localhost:8000/hr/ingest"
    source = "verity"
    stall_seconds = 0.05
    pmd_grace_seconds = 0.0
    verbose = False


class _LiveClient(FakeClient):
    is_connected = True          # the link stays up; only the data channel is silent


def _session(rung: int, live: bool = False):
    client = (_LiveClient if live else FakeClient)({})
    logs: list[str] = []
    orig_log, orig_post = vf._log, vf._post_link
    vf._log = lambda msg, *a, **k: logs.append(str(msg))
    vf._post_link = lambda *a, **k: None
    vf._STATS["acc_rung"] = rung
    vf._STATS.pop("pmd_stall", None)
    try:
        ok = asyncio.run(asyncio.wait_for(vf._pmd_session(client, _Args()), timeout=5.0))
    finally:
        vf._log, vf._post_link = orig_log, orig_post
    return ok, client, logs


def test_the_ladder_halves_the_rate_then_drops_the_accelerometer():
    assert vf._acc_ladder(52) == [52, 26, None]
    assert vf._acc_ladder(26) == [26, None]


def test_a_refused_rate_leaves_the_ladder_for_good():
    assert vf._acc_ladder(52, {26}) == [52, None]
    assert vf._acc_ladder(52, {26, 52}) == [52, None]   # never empty: the asked-for rate stays


def _stall(acc=52, acc_frames=0, ppi_frames=10):
    return {"acc": acc, "acc_frames": acc_frames, "ppi_frames": ppi_frames}


def test_only_a_silent_accelerometer_steps_the_ladder_down():
    # ACC delivered nothing while PPI kept flowing on the same link: the accelerometer's fault
    assert vf._next_acc_rung(0, 3, _stall(), 90.0) == 1
    assert vf._next_acc_rung(1, 3, _stall(acc=26), 90.0) == 2
    assert vf._next_acc_rung(2, 3, _stall(acc=None), 90.0) == 2      # nothing left to shed
    assert vf._next_acc_rung(1, 3, None, 90.0) == 1                  # a clean short session holds
    assert vf._next_acc_rung(2, 3, None, vf._ACC_RESTORE_AFTER_S) == 0   # a long clean run restores


def test_a_link_that_died_is_not_an_accelerometer_fault():
    """2026-09-20 22:36: both streams went quiet and both restarts failed with "Not connected".
    Stepping the accelerometer down for that treats a radio drop as an accelerometer fault."""
    assert vf._next_acc_rung(0, 3, _stall(acc_frames=1231, ppi_frames=888), 90.0) == 0
    assert vf._next_acc_rung(0, 3, _stall(acc_frames=0, ppi_frames=0), 90.0) == 0
    assert vf._next_acc_rung(0, 3, {"acc": 52}, 90.0) == 0      # no per-stream counts: hold


def test_rung_one_asks_for_26hz_and_rung_two_asks_for_no_accelerometer():
    ok, client, logs = _session(1)
    assert _starts(client, pmd.MEAS_ACC) == 1 and _starts(client, pmd.MEAS_PPI) == 1
    assert any("start ACC @26Hz" in l for l in logs), logs
    ok, client, logs = _session(2)
    assert _starts(client, pmd.MEAS_ACC) == 0 and _starts(client, pmd.MEAS_PPI) == 1


def test_a_silent_session_records_the_stall_with_its_rung_and_per_stream_counts():
    ok, client, logs = _session(0, live=True)
    assert ok is False
    stall = vf._STATS["pmd_stall"]
    assert stall["rung"] == 0 and stall["acc"] == 52
    assert stall["acc_frames"] == 0 and "ppi_frames" in stall


def test_the_chosen_rate_survives_a_forwarder_restart(tmp_path):
    vf._save_acc_rate(tmp_path, 26)
    assert vf._load_acc_rate(tmp_path) == 26
    vf._save_acc_rate(tmp_path, None)
    assert vf._load_acc_rate(tmp_path) is None
    assert vf._load_acc_rate(tmp_path / "nowhere") == ""


def test_a_stored_rate_the_ladder_no_longer_offers_resumes_at_the_top(tmp_path):
    """The state the box woke up in: 26 Hz was chosen the night before and is now known
    unsupported. Resuming "where we left off" would mean no accelerometer at all."""
    ladder = vf._acc_ladder(52, {26})
    assert vf._rung_for_rate(ladder, 26) == 0
    assert vf._rung_for_rate(ladder, 52) == 0
    assert vf._rung_for_rate(ladder, None) == len(ladder) - 1
    assert vf._rung_for_rate(ladder, "") == 0


def test_an_old_index_style_rung_file_self_heals_to_the_top(tmp_path):
    """Builds before 2026-09-21 persisted the ladder POSITION; read as a rate it is not one
    we offer, so the band comes back at its full rate instead of stuck at "no accelerometer"."""
    (tmp_path / ".run").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".run" / vf._ACC_RATE_FILE).write_text("2")
    assert vf._rung_for_rate(vf._acc_ladder(52), vf._load_acc_rate(tmp_path)) == 0


def test_refused_rates_are_remembered_across_restarts(tmp_path):
    assert vf._load_unsupported_rates(tmp_path) == set()
    assert vf._remember_unsupported_rate(tmp_path, 26) == {26}
    assert vf._load_unsupported_rates(tmp_path) == {26}
    assert vf._remember_unsupported_rate(tmp_path, 26) == {26}      # idempotent
