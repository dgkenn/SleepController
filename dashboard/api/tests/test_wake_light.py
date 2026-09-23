"""The wake light: a Wi-Fi smart plug on a 10,000-lux therapy lamp, set up from the phone.

Setup has to work without sitting at the computer: the box finds the plug on its own network,
and the one secret a Tuya plug needs (its local key) is fetched from the Tuya developer cloud
once, with the developer credentials used for that request only.
"""
import json
from types import SimpleNamespace

import pytest

from app import services


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    r = Repository(str(tmp_path / "wl.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    return r


class _FakeTuya:
    def __init__(self, scan=None, devices=None, find=None):
        self._scan, self._devices, self._find = scan or {}, devices, find or {}
        self.cloud_args = None

    def deviceScan(self, *a, **k):
        return self._scan

    def find_device(self, dev_id):
        return self._find.get(dev_id) or {}

    def Cloud(self, **kw):
        self.cloud_args = kw
        return SimpleNamespace(getdevices=lambda *a: self._devices)


def test_without_the_library_setup_says_so_instead_of_failing(repo, monkeypatch):
    monkeypatch.setattr(services, "_tinytuya", lambda: None)
    out = services.plug_scan(repo)
    assert out["ok"] is False and "tinytuya" in out["error"]


def test_a_scan_remembers_what_answered(repo, monkeypatch):
    fake = _FakeTuya(scan={"192.168.1.40": {"gwId": "abc123", "version": "3.3"}})
    monkeypatch.setattr(services, "_tinytuya", lambda: fake)
    out = services.plug_scan(repo)
    assert out["ok"] and out["devices"][0]["device_id"] == "abc123"
    assert services._last_scan(repo)[0]["ip"] == "192.168.1.40"


def test_an_empty_scan_explains_what_it_means(repo, monkeypatch):
    monkeypatch.setattr(services, "_tinytuya", lambda: _FakeTuya(scan={}))
    out = services.plug_scan(repo)
    assert out["ok"] and out["devices"] == [] and "Matter" in out["hint"]


def test_the_cloud_key_configures_the_plug_found_on_the_lan(repo, monkeypatch):
    fake = _FakeTuya(scan={"192.168.1.40": {"gwId": "plug1", "version": "3.4"}},
                     devices=[{"id": "plug1", "key": "K3Y", "name": "Lamp", "category": "cz"},
                              {"id": "bulb9", "key": "OTHER", "name": "Bulb", "category": "dj"}])
    monkeypatch.setattr(services, "_tinytuya", lambda: fake)
    services.plug_scan(repo)
    out = services.plug_tuya_cloud_setup(repo, "us", "id", "secret")
    assert out["ok"] and out["plug"]["device_id"] == "plug1" and out["found_on_lan"]
    cfg = services._get_plug_config(repo)
    assert cfg["enabled"] and cfg["backend"] == "tuya"
    assert cfg["config"]["local_key"] == "K3Y" and cfg["config"]["ip"] == "192.168.1.40"
    assert cfg["config"]["version"] == "3.4"
    # the developer secret is not stored anywhere
    dump = json.dumps([dict(r) for r in repo.conn.execute("SELECT * FROM settings_kv")])
    assert "secret" not in dump
    # ...and the key never goes back to a client
    assert services.plug_config_view(repo)["config"]["local_key"] == "***"


def test_several_plugs_ask_which_one(repo, monkeypatch):
    fake = _FakeTuya(devices=[{"id": "a", "key": "1", "name": "Lamp", "category": "cz"},
                              {"id": "b", "key": "2", "name": "Fan", "category": "cz"}])
    monkeypatch.setattr(services, "_tinytuya", lambda: fake)
    out = services.plug_tuya_cloud_setup(repo, "us", "id", "secret")
    assert out["ok"] is False and {c["device_id"] for c in out["choose"]} == {"a", "b"}
    out = services.plug_tuya_cloud_setup(repo, "us", "id", "secret", device_id="b")
    assert out["ok"] and services._get_plug_config(repo)["config"]["local_key"] == "2"


def test_a_cloud_error_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setattr(services, "_tinytuya",
                        lambda: _FakeTuya(devices={"Error": "sign invalid"}))
    out = services.plug_tuya_cloud_setup(repo, "us", "id", "bad")
    assert out["ok"] is False and "sign invalid" in out["error"]


def test_echoing_the_masked_config_back_keeps_the_key(repo):
    services.plug_config_update(repo, {"enabled": True, "backend": "tuya",
                                       "config": {"device_id": "p", "ip": "1.2.3.4",
                                                  "local_key": "REAL"}})
    view = services.plug_config_view(repo)
    services.plug_config_update(repo, {"config": {**view["config"], "ip": "1.2.3.5"}})
    cfg = services._get_plug_config(repo)["config"]
    assert cfg["local_key"] == "REAL" and cfg["ip"] == "1.2.3.5"


def test_the_light_endpoint_queues_a_command(auth_client):
    r = auth_client.post("/wake/light", json={"on": True, "minutes": 20})
    assert r.status_code == 200 and r.json()["queued"] == "light_on"
    r = auth_client.post("/wake/light", json={"on": False})
    assert r.json()["queued"] == "light_off"
