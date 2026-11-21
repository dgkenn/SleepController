"""The free path: Apple Shortcuts posts Health samples the way Shortcuts can."""
import time

import pytest

from app import apple_health


@pytest.fixture()
def eastern(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("raw,want", [
    ("Nov 21, 2026 at 3:14 AM", "2026-11-21T08:14:00+00:00"),     # iOS 17+ display format
    ("Nov 21, 2026 at 3:14 AM", "2026-11-21T08:14:00+00:00"),
    ("2026-11-21T03:14:00-05:00", "2026-11-21T08:14:00+00:00"),        # ISO 8601 format option
    ("2026-11-21T08:14:00Z", "2026-11-21T08:14:00+00:00"),
    ("11/21/2026, 3:14 AM", "2026-11-21T08:14:00+00:00"),
    ("2026-11-21 03:14:00 -0500", "2026-11-21T08:14:00+00:00"),
    ("yesterday", None),
])
def test_dates_in_the_formats_shortcuts_sends(eastern, raw, want):
    assert apple_health.parse_any_ts(raw) == want


def test_sleep_samples_as_lists(auth_client, monkeypatch):
    monkeypatch.setattr(apple_health, "kick_consensus", lambda force=False: "started")
    body = {"kind": "sleep",
            "start": ["2026-11-20T23:10:00-05:00", "2026-11-20T23:40:00-05:00"],
            "end": ["2026-11-20T23:40:00-05:00", "2026-11-21T00:20:00-05:00"],
            "value": ["Core", "Deep"], "source": ["Apple Watch", "Bevel"]}
    out = auth_client.post("/apple/shortcut", json=body).json()
    assert out["ok"] and out["stored"] == 2 and out["consensus"] == "started"


def test_sleep_samples_as_newline_text(auth_client, monkeypatch, eastern):
    """A list dropped into a Text field arrives joined by newlines, dates in display format."""
    monkeypatch.setattr(apple_health, "kick_consensus", lambda force=False: "started")
    body = {"kind": "sleep",
            "start": "Nov 19, 2026 at 11:10 PM\nNov 19, 2026 at 11:40 PM\n",
            "end": "Nov 19, 2026 at 11:40 PM\nNov 20, 2026 at 12:20 AM",
            "value": "Core\nREM", "source": "Dean’s Apple Watch"}
    out = auth_client.post("/apple/shortcut", json=body).json()
    assert out["stored"] == 2, out
    from app.db import get_repo
    row = get_repo().conn.execute("SELECT start, stage, source FROM health_sleep WHERE "
                                  "start = '2026-11-20T04:40:00+00:00'").fetchone()
    assert tuple(row) == ("2026-11-20T04:40:00+00:00", "rem", "Dean’s Apple Watch")


def test_heart_rate_values_with_units(auth_client, monkeypatch):
    calls = []
    monkeypatch.setattr(apple_health, "kick_consensus", lambda force=False: calls.append(force) or "started")
    body = {"kind": "heart_rate", "start": "2026-11-21T03:00:00-05:00\n2026-11-21T03:05:00-05:00",
            "value": "56 count/min\n54", "source": "Apple Watch"}
    out = auth_client.post("/apple/shortcut", json=body).json()
    assert out["stored"] == 2 and calls == [True]     # reruns the comparison straight away


def test_an_unreadable_upload_says_what_it_saw(auth_client):
    out = auth_client.post("/apple/shortcut", json={"kind": "sleep", "start": "soon",
                                                    "end": "later", "value": "Core"}).json()
    assert out["ok"] is False and out["first_unread"]["start"] == "soon"


def test_unknown_kind(auth_client):
    assert auth_client.post("/apple/shortcut", json={"kind": "steps", "start": ["x"],
                                                     "value": ["1"]}).json()["ok"] is False


@pytest.mark.parametrize("raw,want", [
    ("Core", "core"), ("Asleep Core", "core"), ("Asleep (Unspecified)", "asleep"),
    ("In Bed", "in_bed"), ("REM Sleep", "rem"), ("Deep", "deep"), ("4", "deep"), ("0", "in_bed"),
    ("Awake", "awake"), ("Light", "core"), ("", None), ("Nap", None),
])
def test_sleep_value_spellings(raw, want):
    assert apple_health.shortcut_stage(raw) == want


def test_skips_are_explained_per_reason_and_source(auth_client, monkeypatch):
    monkeypatch.setattr(apple_health, "kick_consensus", lambda force=False: "started")
    body = {"kind": "sleep",
            "start": ["2026-11-19T23:10:00-05:00", "2026-11-19T23:40:00-05:00", "soon"],
            "end": ["2026-11-19T23:40:00-05:00", "2026-11-20T00:20:00-05:00",
                    "2026-11-20T00:30:00-05:00"],
            "value": ["Core", "Snoring", "Deep"], "source": ["Apple Watch", "Bevel", "Bevel"]}
    out = auth_client.post("/apple/shortcut", json=body).json()
    assert out["stored"] == 1 and out["skipped"] == 2
    assert out["skipped_because"] == {"unknown sleep value 'Snoring'": 1,
                                      "unreadable start date 'soon'": 1}
    assert list(out["by_source"].items()) == [("Apple Watch", [1, 0]), ("Bevel", [0, 2])]
