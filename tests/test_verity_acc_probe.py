"""An accelerometer that starts and sends nothing is stepped down on the live link within
the first minute, not after a two-minute stall and a reconnect -- but only when the beat stream
on the same link IS delivering. With both silent the link is dead, and a dead link is not an
accelerometer fault (audit 2026-09-25: the probe persisted "off" for a radio drop)."""
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


class _PpiClient(_LiveClient):
    """A live link whose PPI stream delivers while the accelerometer stays silent."""

    data_cb = None

    async def start_notify(self, uuid, cb):
        await super().start_notify(uuid, cb)
        if str(uuid) == str(pmd.PMD_DATA_UUID):
            self.data_cb = cb


def _ppi_frame():
    # one good beat: hr 60, 1000 ms, skin contact supported + detected, blocker clear
    sample = bytes([60]) + (1000).to_bytes(2, "little") + (5).to_bytes(2, "little") + bytes([0b110])
    return bytes([pmd.MEAS_PPI]) + (0).to_bytes(8, "little") + bytes([0x00]) + sample


def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(vf, "_ACC_PROBE_S", 0.05)
    monkeypatch.setattr(vf, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vf, "_post_link", lambda *a, **k: None)
    monkeypatch.setattr(vf, "_post", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(vf, "_beat", lambda *a, **k: None)
    logs = []
    monkeypatch.setattr(vf, "_log", lambda m, *a, **k: logs.append(str(m)))
    vf._STATS["acc_rung"] = 0
    return logs


def _drive(client, feed_ppi: bool):
    async def feeder():
        if not feed_ppi:
            return
        while client.data_cb is None:
            await asyncio.sleep(0.005)
        client.data_cb(0, bytearray(_ppi_frame()))

    async def run():
        task = asyncio.ensure_future(feeder())
        try:
            await asyncio.wait_for(vf._pmd_session(client, _Args()), timeout=0.6)
        except asyncio.TimeoutError:
            pass
        task.cancel()
    asyncio.run(run())
    return [op for op, _ in _acc_starts(client)]


def test_silent_acc_with_ppi_flowing_is_restarted_at_26hz_then_dropped(monkeypatch, tmp_path):
    logs = _quiet(monkeypatch, tmp_path)
    client = _PpiClient({})
    kinds = _drive(client, feed_ppi=True)
    # start@52 -> stop -> start@26 -> stop, then no further ACC start
    assert kinds[:4] == [pmd.OP_START_MEASUREMENT, pmd.OP_STOP_MEASUREMENT,
                         pmd.OP_START_MEASUREMENT, pmd.OP_STOP_MEASUREMENT], kinds
    assert kinds.count(pmd.OP_START_MEASUREMENT) == 2
    assert any("retrying at 26Hz" in l for l in logs), logs
    assert any("PPI only" in l for l in logs), logs
    # the rung is persisted as the RATE it settled on, not its position in the ladder
    assert vf._load_acc_rate(tmp_path) is None


def test_a_dead_link_does_not_step_the_accelerometer_down(monkeypatch, tmp_path):
    """Regression (audit 2026-09-25): ACC AND PPI both silent is a dead link. The probe used to
    step the rate down anyway -- persisting "off" -- so the next, healthy session started
    without the accelerometer."""
    logs = _quiet(monkeypatch, tmp_path)
    client = _PpiClient({})
    kinds = _drive(client, feed_ppi=False)
    assert kinds.count(pmd.OP_START_MEASUREMENT) == 1, kinds    # never restarted at 26 Hz
    assert not any("retrying at" in l or "sent nothing at any rate" in l for l in logs), logs
    assert vf._STATS["acc_rung"] == 0
    assert vf._load_acc_rate(tmp_path) == ""                     # nothing persisted
