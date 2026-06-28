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


def test_hue_config_get_set_hides_token(auth_client):
    cfg = auth_client.get("/wake/light/config").json()
    assert cfg["paired"] is False and "token" not in cfg
    r = auth_client.put("/wake/light/config", json={"enabled": True, "bridge_ip": "192.168.1.50",
                                                    "target_ids": ["1", "2"], "kind": "lights"})
    assert r.status_code == 200
    cfg2 = r.json()
    assert cfg2["enabled"] is True and cfg2["bridge_ip"] == "192.168.1.50"
    assert cfg2["target_ids"] == ["1", "2"]    # both bulbs
    assert "token" not in cfg2                 # secret never returned to the client


def test_wake_plan_unifies_gym_and_smart_alarm(auth_client):
    auth_client.put("/gym/config", json={"enabled": True, "early_offset_min": 75})
    p = auth_client.get("/wake/plan").json()
    assert p["gym_enabled"] is True
    assert "effective_wake" in p and "normal_wake" in p
    assert p["smart_window_min"] >= 1
    assert isinstance(p["vibration_ladder"], list) and len(p["vibration_ladder"]) == 3
    assert p["silent_only"] is True
