"""The n-of-1 temperature trial is ON, and stoppable from the phone without a deploy.

The controller's only prevention move is to cool, and nobody knows whether cooling prevents
this user's awakenings or causes them. Observation cannot answer it -- the controller warms
BECAUSE you woke, so rate-by-temperature is reverse-causal -- so the answer has to come from a
randomised, comfort-clamped offset. An experiment that changes what the bed does overnight must
be revocable at 2am from the phone, not only by a push.
"""
import json

import pytest

from app import diagnostics


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "tt.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _set(repo, value):
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('thermal_trial', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
    repo.conn.commit()


def test_the_trial_ships_enabled():
    from sleepctl.config import AppConfig
    assert AppConfig.default().thermal_trial.enabled is True


def test_the_ladder_tests_warming_as_well_as_cooling():
    """Raymann 2008 found a small skin-temperature RISE suppressed nocturnal wakefulness --
    the opposite of this controller's default cool bias. The trial has to be able to find that."""
    from sleepctl.config import AppConfig
    ladder = AppConfig.default().thermal_trial.offset_ladder_f
    assert any(x > 0 for x in ladder) and any(x < 0 for x in ladder) and 0.0 in ladder


def test_every_arm_stays_inside_the_comfort_band():
    from sleepctl.config import AppConfig
    tc = AppConfig.default().thermal_trial
    assert all(abs(x) <= tc.comfort_band_f for x in tc.offset_ladder_f)


def test_the_dashboard_toggle_switches_it_off(repo):
    _set(repo, False)
    c = diagnostics._check_thermal_trial(repo)
    assert c["status"] == "info"
    assert "switched off" in c["detail"]


def test_the_toggle_back_on_is_honoured(repo):
    _set(repo, True)
    c = diagnostics._check_thermal_trial(repo)
    assert "switched off" not in c["detail"]


def test_the_api_exposes_the_default_so_the_settings_page_can_show_it(auth_client):
    d = auth_client.get("/settings").json()["defaults"]
    assert d["thermal_trial"] is True
