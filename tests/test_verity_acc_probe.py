"""An accelerometer that starts and sends nothing is stepped down on the live link within
the first minute, not after a two-minute stall and a reconnect."""
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


from test_verity_pmd_stale_stream import FakeClient  # noqa: E402


class _Args:
    acc_rate = 52
    acc_rate_cli = 52
    acc_resolution = 16
    acc_range = 8
    control_timeout = 1.0
    batch_seconds = 0.01
    url = "http://localhost:8000/hr/ingest"
    source = "verity"
    stall_seconds = 100.0          # the stall guard must NOT be what ends this session
    pmd_grace_seconds = 0.0
    verbose = False


class _LiveClient(FakeClient):
    is_connected = True


def _acc_starts(client):
    return [(op, m) for op, m in client.commands if m == pmd.MEAS_ACC]


def test_silent_acc_is_restarted_at_26hz_then_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(vf, "_ACC_PROBE_S", 0.05)
    monkeypatch.setattr(vf, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vf, "_post_link", lambda *a, **k: None)
    monkeypatch.setattr(vf, "_beat", lambda *a, **k: None)
    logs = []
    monkeypatch.setattr(vf, "_log", lambda m, *a, **k: logs.append(str(m)))
    vf._STATS["acc_rung"] = 0
    client = _LiveClient({})

    async def run():
        try:
            await asyncio.wait_for(vf._pmd_session(client, _Args()), timeout=0.6)
        except asyncio.TimeoutError:
            pass
    asyncio.run(run())
    ops = _acc_starts(client)
    # start@52 -> stop -> start@26 -> stop, then no further ACC start
    kinds = [op for op, _ in ops]
    assert kinds[:4] == [pmd.OP_START_MEASUREMENT, pmd.OP_STOP_MEASUREMENT,
                         pmd.OP_START_MEASUREMENT, pmd.OP_STOP_MEASUREMENT], kinds
    assert kinds.count(pmd.OP_START_MEASUREMENT) == 2
    assert any("retrying at 26Hz" in l for l in logs), logs
    assert any("PPI only" in l for l in logs), logs
    assert vf._load_acc_rung(tmp_path) == 2
