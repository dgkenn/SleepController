"""Tests for the two remote deep-dive tools: raw log fetch (/diag/logs) and a live,
read-only Eight Sleep round-trip (/diag/probe). Both are gated exactly like /diag (a secret
DIAG_TOKEN, 404 on missing/wrong token). Neither test hits the real network -- /diag/probe's
cloud client is monkeypatched.
"""

from __future__ import annotations

import os

import pytest

from app.main import _run_dir


# ------------------------------------------------------------------ /diag/logs
def test_diag_logs_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/logs?file=daemon").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/logs?file=daemon").status_code == 404          # no token
    assert client.get("/diag/logs?file=daemon&token=nope").status_code == 404  # wrong token


def test_diag_logs_rejects_unknown_file(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.get("/diag/logs?token=s3cret-xyz&file=../../etc/passwd")
    assert r.status_code == 400
    r = client.get("/diag/logs?token=s3cret-xyz&file=nonsense")
    assert r.status_code == 400


def test_diag_logs_missing_file_is_placeholder(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    # api-err is whitelisted but (almost certainly) never written in the test run dir.
    r = client.get("/diag/logs?token=s3cret-xyz&file=api-err")
    assert r.status_code == 200
    assert r.text == "(file not found)"


def _write_daemon_log(lines: list[str]) -> None:
    run = _run_dir()
    os.makedirs(run, exist_ok=True)
    with open(os.path.join(run, "daemon.log"), "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")


def test_diag_logs_honors_lines_and_returns_raw_tail(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    seeded = [f"line {i}: control_tick ok" for i in range(20)]
    _write_daemon_log(seeded)

    r = client.get("/diag/logs?token=s3cret-xyz&file=daemon&lines=5")
    assert r.status_code == 200
    got = r.text.splitlines()
    assert got == seeded[-5:]  # raw, unsummarized tail -- exact match


def test_diag_logs_lines_is_capped(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    seeded = [f"line {i}" for i in range(1500)]
    _write_daemon_log(seeded)
    r = client.get("/diag/logs?token=s3cret-xyz&file=daemon&lines=999999")
    assert r.status_code == 200
    # capped at 1000 -- the response is the last 1000 seeded lines, not all 1500
    assert r.text.splitlines() == seeded[-1000:]


def test_diag_logs_grep_filters_case_insensitively(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    seeded = [
        "2026-07-02 control_tick ok",
        "2026-07-02 WATCHDOG restart triggered",
        "2026-07-02 control_tick ok",
        "2026-07-02 watchdog: restart complete",
        "2026-07-02 nothing interesting",
    ]
    _write_daemon_log(seeded)

    r = client.get("/diag/logs?token=s3cret-xyz&file=daemon&lines=100&grep=restart")
    assert r.status_code == 200
    got = r.text.splitlines()
    assert len(got) == 2
    assert all("restart" in ln.lower() for ln in got)


def test_diag_logs_grep_falls_back_to_literal_on_bad_regex(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    seeded = [
        "2026-07-02 error: something[broke",   # contains the literal, invalid-as-regex text
        "2026-07-02 all good",
    ]
    _write_daemon_log(seeded)
    # "something[broke" is not a valid regex (unterminated char class) -- must fall back to a
    # literal, case-insensitive substring match rather than raising/500ing.
    r = client.get("/diag/logs?token=s3cret-xyz&file=daemon&lines=100&grep=something%5Bbroke")
    assert r.status_code == 200
    got = r.text.splitlines()
    assert len(got) == 1 and "something[broke" in got[0]


def test_diag_logs_grep_no_matches_placeholder(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _write_daemon_log(["2026-07-02 all good"])
    r = client.get("/diag/logs?token=s3cret-xyz&file=daemon&grep=totally-absent-string")
    assert r.status_code == 200
    assert r.text == "(no matching lines)"


# ------------------------------------------------------------------ /diag/probe
def test_diag_probe_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/probe").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/probe").status_code == 404
    assert client.get("/diag/probe?token=nope").status_code == 404


def test_diag_probe_no_creds_returns_ok_false_not_500(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.delenv("EIGHTSLEEP_EMAIL", raising=False)
    monkeypatch.delenv("EIGHTSLEEP_PASSWORD", raising=False)
    # make sure no local credentials.json on the test machine leaks in
    from sleepctl.adapters import credentials as creds_mod
    monkeypatch.setattr(creds_mod, "DEFAULT_PATH", creds_mod.Path("/nonexistent/creds.json"))

    r = client.get("/diag/probe?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "credential" in body["error"].lower()
    assert body["device"] is None and body["frame"] is None


def test_diag_probe_failing_client_returns_ok_false_not_500(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("EIGHTSLEEP_EMAIL", "user@example.com")
    monkeypatch.setenv("EIGHTSLEEP_PASSWORD", "hunter2")

    class _FakeFailingClient:
        def __init__(self, *a, **k):
            self.closed = False

        async def connect(self):
            raise RuntimeError("boom-cloud-down")

        async def update(self):  # pragma: no cover - never reached
            pass

        def read_frame(self):  # pragma: no cover - never reached
            pass

        def device_status(self):  # pragma: no cover - never reached
            return {}

        async def close(self):
            self.closed = True

    import sleepctl.adapters.eightsleep_cloud as cloud_mod
    monkeypatch.setattr(cloud_mod, "EightSleepClient", _FakeFailingClient)

    r = client.get("/diag/probe?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "boom-cloud-down" in body["error"]
    assert body["device"] is None and body["frame"] is None


def test_diag_probe_success_shape_and_never_calls_write_commands(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("EIGHTSLEEP_EMAIL", "user@example.com")
    monkeypatch.setenv("EIGHTSLEEP_PASSWORD", "hunter2")

    from sleepctl.models import SensorFrame, SleepStage
    from datetime import datetime

    class _FakeOkClient:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            pass

        async def update(self):
            pass

        def read_frame(self):
            return SensorFrame(timestamp=datetime.now(), stage=SleepStage.DEEP,
                               heart_rate=58.0, hrv=42.0, respiratory_rate=14.0,
                               presence=True, bed_temp_f=91.5, device_level=10,
                               target_level=10, data_age_seconds=3.0)

        def device_status(self):
            return {"online": True, "has_water": True, "priming": False, "needs_priming": False}

        async def close(self):
            pass

        # if the probe ever called any of these, that would be a control-command leak
        async def set_heating_level(self, *a, **k):  # pragma: no cover
            raise AssertionError("probe must never send a device command")

        async def turn_on_side(self, *a, **k):  # pragma: no cover
            raise AssertionError("probe must never send a device command")

        async def prime_pod(self, *a, **k):  # pragma: no cover
            raise AssertionError("probe must never send a device command")

    import sleepctl.adapters.eightsleep_cloud as cloud_mod
    monkeypatch.setattr(cloud_mod, "EightSleepClient", _FakeOkClient)

    r = client.get("/diag/probe?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["latency_ms"] is not None and body["latency_ms"] >= 0
    assert body["device"] == {"online": True, "has_water": True, "priming": False,
                              "needs_priming": False}
    assert body["frame"]["heart_rate"] == 58.0
    assert body["frame"]["stage"] == "deep"
    assert body["frame"]["data_age_seconds"] == 3.0
