"""Gym advisor API: config get/set + the morning advice call."""

from __future__ import annotations


def test_gym_config_defaults_off(auth_client):
    cfg = auth_client.get("/gym/config").json()["config"]
    assert cfg["enabled"] is False
    assert cfg["early_offset_min"] == 75 or isinstance(cfg["early_offset_min"], int)


def test_gym_config_update_and_persist(auth_client):
    r = auth_client.put("/gym/config", json={"enabled": True, "early_offset_min": 75,
                                             "lean": "balanced"})
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["enabled"] is True and cfg["early_offset_min"] == 75 and cfg["lean"] == "balanced"
    # bad lean is clamped to balanced, not stored raw
    cfg2 = auth_client.put("/gym/config", json={"lean": "nonsense"}).json()["config"]
    assert cfg2["lean"] == "balanced"
    # persisted
    assert auth_client.get("/gym/config").json()["config"]["enabled"] is True


def test_gym_advice_runs_when_enabled(auth_client):
    auth_client.put("/gym/config", json={"enabled": True})
    a = auth_client.get("/gym/advice").json()
    assert a["enabled"] is True
    assert a["recommend"] in ("go", "sleep_in", "rest_day")
    assert "headline" in a and isinstance(a["reasons"], list)


def test_gym_advice_off_when_disabled(auth_client):
    auth_client.put("/gym/config", json={"enabled": False})
    a = auth_client.get("/gym/advice").json()
    assert a["recommend"] == "off"
