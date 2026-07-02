"""CALENDAR_ICS_URL env seeding: connect the shift calendar without the dashboard UI."""

from app import services
from app.db import get_repo


def test_seeds_when_env_set_and_unconfigured(monkeypatch):
    monkeypatch.setenv("CALENDAR_ICS_URL", "https://calendar.google.com/calendar/ical/x/basic.ics")
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM settings_kv WHERE key='calendar_config'")
        repo.conn.commit()
        assert services.seed_calendar_from_env(repo) is True
        cfg = services._get_calendar_config(repo)
        assert cfg["enabled"] is True and cfg["ics_url"].endswith("basic.ics")
        # the raw URL is masked on read-back
        assert services.calendar_config_view(repo)["configured"] is True
    finally:
        repo.close()


def test_noop_when_already_configured(monkeypatch):
    monkeypatch.setenv("CALENDAR_ICS_URL", "https://example.com/env.ics")
    repo = get_repo()
    try:
        services.calendar_config_update(repo, {"enabled": True, "ics_url": "https://example.com/ui.ics"})
        assert services.seed_calendar_from_env(repo) is False       # UI value respected
        assert services._get_calendar_config(repo)["ics_url"] == "https://example.com/ui.ics"
    finally:
        repo.close()


def test_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("CALENDAR_ICS_URL", raising=False)
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM settings_kv WHERE key='calendar_config'")
        repo.conn.commit()
        assert services.seed_calendar_from_env(repo) is False
    finally:
        repo.close()
