"""Every PMD session since 2026-08-31 started ACC successfully and then delivered nothing.
After a stall with the accelerometer running, the next session asks for less."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polar_pmd as pmd  # noqa: E402
import verity_forwarder as vf  # noqa: E402

import pytest


@pytest.fixture(autouse=True)
def _isolated_rung(monkeypatch):
    """Module-global forwarder state must not leak into other test files."""
    monkeypatch.setitem(vf._STATS, "acc_rung", 0)
    monkeypatch.setitem(vf._STATS, "pmd_stall", None)
    yield
    vf._STATS.pop("pmd_stall", None)
    vf._STATS.pop("pmd_streamed_s", None)


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


def test_a_stall_with_the_accelerometer_running_steps_down_one_rung():
    assert vf._next_acc_rung(0, 3, {"acc": 52}, 90.0) == 1
    assert vf._next_acc_rung(1, 3, {"acc": 26}, 90.0) == 2
    assert vf._next_acc_rung(2, 3, {"acc": None}, 90.0) == 2      # nothing left to shed
    assert vf._next_acc_rung(1, 3, None, 90.0) == 1                # a clean short session holds
    assert vf._next_acc_rung(2, 3, None, vf._ACC_RESTORE_AFTER_S) == 0   # a long clean run restores


def test_rung_one_asks_for_26hz_and_rung_two_asks_for_no_accelerometer():
    ok, client, logs = _session(1)
    assert _starts(client, pmd.MEAS_ACC) == 1 and _starts(client, pmd.MEAS_PPI) == 1
    assert any("start ACC @26Hz" in l for l in logs), logs
    ok, client, logs = _session(2)
    assert _starts(client, pmd.MEAS_ACC) == 0 and _starts(client, pmd.MEAS_PPI) == 1


def test_a_silent_session_records_the_stall_with_its_rung():
    ok, client, logs = _session(0, live=True)
    assert ok is False
    assert vf._STATS["pmd_stall"]["rung"] == 0 and vf._STATS["pmd_stall"]["acc"] == 52


def test_the_rung_survives_a_forwarder_restart(tmp_path):
    vf._save_acc_rung(tmp_path, 2)
    assert vf._load_acc_rung(tmp_path) == 2
    assert vf._load_acc_rung(tmp_path / "nowhere") == 0
