"""The forwarder's real session flow (_run_once) against a scripted fake BLE stack.

Every scenario is a night that actually happened:
  * 2026-09-19 05:07  band on its charger: PMD refused with code 13, forwarder fell back to
                      heart rate and the session ran on for an hour.
  * 2026-09-18 23:41  PMD streams went silent on a healthy link; HR-only for five hours.
  * two receivers     the Pi at the bedside and the Windows box sharing one band.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polar_pmd as pmd  # noqa: E402
import verity_forwarder as vf  # noqa: E402

ADDR = "24:AC:AC:16:96:1D"


class Script:
    def __init__(self):
        self.refuse_code = 0          # PMD START error code (0 = accept)
        self.hr_stream = True         # generic HR service delivers packets
        self.clients = []


class _Adv:
    rssi = -58


class _Device:
    name, address = "Polar Sense 16961D33", ADDR


class _Svc:
    uuid = pmd.PMD_SERVICE_UUID


def _fake_bleak(script: Script):
    class BleakScanner:
        @staticmethod
        async def discover(timeout=20.0, return_adv=False):
            return {ADDR: (_Device(), _Adv())} if return_adv else [_Device()]

    class BleakClient:
        def __init__(self, address, **kw):
            self.address, self.is_connected, self.mtu_size = address, True, 232
            self.notify, self.commands, self._pump = {}, [], None
            self.control_cb = None

        async def __aenter__(self):
            script.clients.append(self)
            return self

        async def __aexit__(self, *a):
            self.is_connected = False
            if self._pump:
                self._pump.cancel()

        @property
        def services(self):
            return [_Svc()]

        async def start_notify(self, uuid, cb):
            self.notify[str(uuid)] = cb
            if str(uuid) == str(pmd.PMD_CONTROL_UUID):
                self.control_cb = cb
            if str(uuid) == vf.HR_MEASUREMENT_UUID and script.hr_stream:
                async def pump():
                    while True:
                        await asyncio.sleep(0.005)
                        cb(None, bytearray([0x00, 60]))
                self._pump = asyncio.ensure_future(pump())

        async def stop_notify(self, uuid):
            if str(uuid) == vf.HR_MEASUREMENT_UUID and self._pump:
                self._pump.cancel()

        async def read_gatt_char(self, uuid):
            return bytes([61])

        async def write_gatt_char(self, uuid, cmd, response=True):
            opcode, meas = cmd[0], cmd[1]
            self.commands.append((opcode, meas))
            err = script.refuse_code if opcode == pmd.OP_START_MEASUREMENT else 0
            self.control_cb(0, bytes([pmd.CONTROL_RESPONSE_HEADER, opcode, meas, err, 0x00]))

    mod = types.ModuleType("bleak")
    mod.BleakClient, mod.BleakScanner = BleakClient, BleakScanner
    return mod


def _args():
    return types.SimpleNamespace(
        address=ADDR, url="http://box:8000/hr/ingest?token=t", source="verity", mode="auto",
        batch_seconds=0.01, stall_seconds=0.15, pmd_grace_seconds=0.0, control_timeout=1.0,
        hr_max_age=100.0, connect_timeout=1.0, retry_seconds=0.0,
        acc_rate=52, acc_rate_cli=52, acc_resolution=16, acc_range=8, verbose=False)


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    script = Script()
    monkeypatch.setitem(sys.modules, "bleak", _fake_bleak(script))
    monkeypatch.setattr(vf, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vf, "_beat", lambda *a, **k: None)
    posts, logs = [], []
    def fake_post(url, payload, timeout=5.0):
        posts.append(payload)
        vf._STATS["posts"] += 1          # what the real _post does on an accepted batch
        return {"ok": True}

    monkeypatch.setattr(vf, "_post", fake_post)
    monkeypatch.setattr(vf, "_log", lambda m, *a, **k: logs.append(str(m)))
    monkeypatch.setattr(vf, "_fetch_streams", lambda args, timeout=3.0: None)
    monkeypatch.setattr(vf, "_PMD_RESTART_AFTER_S", 0.05)
    monkeypatch.setattr(vf, "_FORCED_HR_RETRY_PMD_S", 0.1)
    monkeypatch.setattr(vf, "_ROLE_CHECK_S", 0.05)
    monkeypatch.setattr(vf, "_PMD_MISSING_BEFORE_STEPUP_S", 0.05)
    for k, v in (("posts", 0), ("acc_rung", 0), ("pmd_retries", 0), ("last_data_at", 0.0),
                 ("session_opened", False)):
        monkeypatch.setitem(vf._STATS, k, v)
    monkeypatch.setitem(vf._RELEASE, "until", 0.0)
    monkeypatch.setitem(vf._RELEASE, "run", 0)
    vf._RELEASE.pop("off_arm", None)
    yield script, posts, logs
    vf._RELEASE.pop("off_arm", None)
    vf._STATS.pop("pmd_stall", None)
    vf._STATS.pop("pmd_streamed_s", None)


def _run(args):
    asyncio.run(asyncio.wait_for(vf._run_once(args, {}), timeout=10.0))


def _links(posts):
    return [p["link"] for p in posts if "link" in p]


def _starts(client):
    return [m for op, m in client.commands if op == pmd.OP_START_MEASUREMENT]


def _stops(client):
    return [m for op, m in client.commands if op == pmd.OP_STOP_MEASUREMENT]


def test_a_band_on_its_charger_ends_the_session_instead_of_falling_back_to_heart_rate(harness):
    script, posts, logs = harness
    script.refuse_code = pmd.ERROR_DEVICE_IN_CHARGER
    _run(_args())
    assert _links(posts)[-1] == "charging"
    client = script.clients[-1]
    assert vf.HR_MEASUREMENT_UUID not in client.notify, "fell back to the generic HR service"
    assert not any(p.get("hr") for p in posts)
    assert vf._RELEASE["until"] > 0            # and the band is left alone to charge


def test_silent_pmd_is_restarted_on_the_link_then_falls_back_for_a_bounded_time(harness):
    script, posts, logs = harness
    _run(_args())
    client = script.clients[-1]
    assert _starts(client).count(pmd.MEAS_PPI) >= 2, "PPI was not restarted on the live link"
    assert _starts(client).count(pmd.MEAS_ACC) >= 2, "ACC was not restarted on the live link"
    assert any("restarting them on the live link" in m for m in logs)
    assert any("falling back to the generic HR service for up to" in m for m in logs)
    assert any("dropping the link after" in m for m in logs), "HR fallback was not bounded"
    assert any(p.get("hr") for p in posts)                  # heart rate did flow meanwhile
    assert vf._STATS.get("pmd_stall") is not None           # the ladder will step down next
    assert _links(posts)[-1] == "lost"


def test_the_second_receiver_takes_heart_rate_and_steps_up_when_the_holder_goes_stale(harness, monkeypatch):
    script, posts, logs = harness
    calls = {"n": 0}

    def streams(args, timeout=3.0):
        calls["n"] += 1
        age = 1.0 if calls["n"] == 1 else 999.0      # fresh at connect, stale afterwards
        return {"pmd_holder": "verity-pi", "acc": {"age_s": age, "source": "verity-pi"},
                "ppi": {"age_s": age, "source": "verity-pi"}}

    monkeypatch.setattr(vf, "_fetch_streams", streams)
    _run(_args())
    client = script.clients[-1]
    assert _starts(client) == [], "the second receiver must not start PMD"
    assert vf.HR_MEASUREMENT_UUID in client.notify
    assert any("second receiver" in m for m in logs)
    assert any("take over" in m for m in logs)
    assert any(p.get("hr") for p in posts)
    assert _links(posts)[0] == "connected" and _links(posts)[-1] == "lost"


def test_with_the_api_unreachable_the_receiver_acts_alone(harness):
    script, posts, logs = harness
    script.refuse_code = pmd.ERROR_DEVICE_IN_CHARGER     # any quick exit will do
    _run(_args())
    assert not any("second receiver" in m for m in logs)
