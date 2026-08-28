"""An unclean disconnect leaves the band's PMD streams believing they are still RUNNING, so the
very next session is refused ACC and PPI with error code 6 ("already in state") and silently
degrades to HR-only -- losing the accelerometer, which is the single best wake signal we have
(6/6 vs the HR stager's 2/6 against message-timestamp ground truth).

Observed live 2026-08-27 22:12: a deploy restarted the forwarder mid-stream and the whole night
ran without actigraphy. These tests pin the recovery: STOP the stale stream, start it again.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import polar_pmd as pmd  # noqa: E402
import verity_forwarder as vf  # noqa: E402


def _resp(opcode: int, meas_type: int, error: int) -> bytes:
    return bytes([pmd.CONTROL_RESPONSE_HEADER, opcode, meas_type, error, 0x00])


class FakeClient:
    """Minimal bleak-shaped client. ``refuse`` maps measurement type -> how many START commands
    to refuse with code 6 before accepting; STOP always succeeds and clears the stale state."""

    def __init__(self, refuse: dict[int, int], stop_clears: bool = True):
        self.refuse = dict(refuse)
        self.stop_clears = stop_clears
        self.control_cb = None
        self.notified: list = []
        self.commands: list[tuple[int, int]] = []   # (opcode, measurement type)

    async def start_notify(self, uuid, cb):
        if str(uuid) == str(pmd.PMD_CONTROL_UUID):
            self.control_cb = cb
        self.notified.append(str(uuid))

    async def stop_notify(self, uuid):
        pass

    async def read_gatt_char(self, uuid):
        raise RuntimeError("no battery service")

    async def write_gatt_char(self, uuid, cmd, response=True):
        opcode, meas = cmd[0], cmd[1]
        self.commands.append((opcode, meas))
        if opcode == pmd.OP_STOP_MEASUREMENT:
            if self.stop_clears:
                self.refuse[meas] = 0        # the stale stream is now genuinely stopped
            self.control_cb(0, _resp(opcode, meas, 0))
            return
        remaining = self.refuse.get(meas, 0)
        if remaining > 0:
            self.refuse[meas] = remaining - 1
            self.control_cb(0, _resp(opcode, meas, pmd.ERROR_ALREADY_IN_STATE))
            return
        self.control_cb(0, _resp(opcode, meas, 0))

    #: The streaming loop is `while client.is_connected`; False makes _pmd_session return as soon
    #: as the streams are up, which is the only part these tests are about.
    is_connected = False

    @property
    def services(self):
        class _S:
            uuid = pmd.PMD_SERVICE_UUID
        return [_S()]


class _Args:
    acc_rate = 52
    acc_resolution = 16
    acc_range = 8
    control_timeout = 1.0
    batch_seconds = 0.01
    url = "http://localhost:8000/hr/ingest"
    source = "verity"
    stall_timeout = 0.05
    verbose = False


def _run(refuse: dict, stop_clears: bool = True) -> tuple[bool, FakeClient, list[str]]:
    client = FakeClient(refuse, stop_clears=stop_clears)
    logs: list[str] = []
    orig_log = vf._log
    vf._log = lambda msg, *a, **k: logs.append(str(msg))
    try:
        ok = asyncio.run(asyncio.wait_for(vf._pmd_session(client, _Args()), timeout=5.0))
    except asyncio.TimeoutError:
        ok = True     # streams started; the session then runs until disconnect
    finally:
        vf._log = orig_log
    return ok, client, logs


def _starts(client, meas: int) -> int:
    return sum(1 for op, m in client.commands if op == pmd.OP_START_MEASUREMENT and m == meas)


def _stops(client, meas: int) -> int:
    """STOPs issued as part of RECOVERY -- i.e. before the last START of this stream. The session's
    own teardown also stops every started stream, and counting those would hide a missing retry."""
    starts = [i for i, (op, m) in enumerate(client.commands)
              if op == pmd.OP_START_MEASUREMENT and m == meas]
    if not starts:
        return 0
    return sum(1 for i, (op, m) in enumerate(client.commands)
               if op == pmd.OP_STOP_MEASUREMENT and m == meas and i < starts[-1])


def test_a_stale_acc_stream_is_stopped_and_restarted_rather_than_lost():
    _ok, client, logs = _run({pmd.MEAS_ACC: 1})
    assert _stops(client, pmd.MEAS_ACC) == 1, "the stale ACC stream was never stopped"
    assert _starts(client, pmd.MEAS_ACC) == 2, "ACC was not retried after the stop"
    assert not any("FAILED" in m and "ACC" in m for m in logs), logs


def test_a_stale_ppi_stream_is_recovered_too():
    _ok, client, logs = _run({pmd.MEAS_PPI: 1})
    assert _stops(client, pmd.MEAS_PPI) == 1
    assert _starts(client, pmd.MEAS_PPI) == 2
    assert not any("FAILED" in m and "PPI" in m for m in logs), logs


def test_both_streams_recover_independently():
    _ok, client, logs = _run({pmd.MEAS_ACC: 1, pmd.MEAS_PPI: 1})
    assert _starts(client, pmd.MEAS_ACC) == 2 and _starts(client, pmd.MEAS_PPI) == 2
    assert not any("FAILED" in m for m in logs), logs


def test_the_retry_happens_at_most_once_so_a_genuinely_stuck_stream_still_degrades():
    """A band that refuses code 6 forever must not spin: one stop, one retry, then give up."""
    _ok, client, logs = _run({pmd.MEAS_ACC: 99, pmd.MEAS_PPI: 99}, stop_clears=False)
    assert _starts(client, pmd.MEAS_ACC) == 2 and _stops(client, pmd.MEAS_ACC) == 1
    assert _starts(client, pmd.MEAS_PPI) == 2 and _stops(client, pmd.MEAS_PPI) == 1
    assert any("FAILED" in m for m in logs)


def test_the_sdk_mode_remedy_is_not_offered_for_a_stale_stream():
    """Power-cycling the band cannot fix a stale stream. Printing that remedy sends the user to do
    the one thing that will not help, which is worse than printing nothing."""
    _ok, _client, logs = _run({pmd.MEAS_PPI: 99, pmd.MEAS_ACC: 99}, stop_clears=False)
    assert not any("SDK MODE" in m.upper() for m in logs), logs


def test_an_unrelated_refusal_is_not_treated_as_a_stale_stream():
    """Only code 6 means "already running". Any other refusal must degrade immediately -- issuing
    a stop for a stream that was never started is how a working stream gets torn down."""
    class Refuser(FakeClient):
        async def write_gatt_char(self, uuid, cmd, response=True):
            self.commands.append((cmd[0], cmd[1]))
            self.control_cb(0, _resp(cmd[0], cmd[1], 11))

    client = Refuser({})
    logs: list[str] = []
    orig_log = vf._log
    vf._log = lambda msg, *a, **k: logs.append(str(msg))
    try:
        ok = asyncio.run(asyncio.wait_for(vf._pmd_session(client, _Args()), timeout=5.0))
    finally:
        vf._log = orig_log
    assert ok is False, "no stream started, so the caller must fall back to the generic HR service"
    assert _stops(client, pmd.MEAS_ACC) == 0 and _stops(client, pmd.MEAS_PPI) == 0
    assert _starts(client, pmd.MEAS_ACC) == 1 and _starts(client, pmd.MEAS_PPI) == 1
