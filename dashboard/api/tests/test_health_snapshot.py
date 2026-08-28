"""Unit tests for the health-snapshot publisher (app/health_snapshot.py).

Covers the belt-and-suspenders ``scrub`` pass, the whitelist-copy that builds the published
snapshot from ``run_diagnostics``, the JSON encoding, and ``write_snapshot``'s file contract.

Mirrors test_diagnostics.py: a throwaway Repository over a temp SQLite file with the dashboard
tables applied, isolated per test so it never touches the shared test DB.
"""

from __future__ import annotations

import json

import pytest

from app import health_snapshot


# ------------------------------------------------------------------ fixtures / helpers
@pytest.fixture()
def repo(tmp_path):
    """A fresh Repository with the dashboard tables applied, isolated per test."""
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "health_test.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _walk_keys_and_strings(obj):
    """Yield ('key', name) for every dict key and ('str', value) for every string, recursively."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield ("key", k)
            yield from _walk_keys_and_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_keys_and_strings(v)
    elif isinstance(obj, str):
        yield ("str", obj)


# ------------------------------------------------------------------ scrub: redacts secrets
def test_scrub_redacts_email():
    out = scrub_val("contact me at admin@example.com please")
    assert out == "[redacted]"


def test_scrub_redacts_age_key():
    out = scrub_val("age1qzg3zqp5w9x8v7t6r5e4w3q2y1u0i9o8p7a6s5d4f3g2h1j0k9l8m7n6")
    assert out == "[redacted]"


def test_scrub_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert scrub_val(jwt) == "[redacted]"


def test_scrub_redacts_long_hex():
    assert scrub_val("a" * 40) == "[redacted]"
    assert scrub_val("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") == "[redacted]"


def test_scrub_redacts_inline_password():
    assert scrub_val("password=hunter2") == "[redacted]"


def test_scrub_redacts_by_key_name():
    obj = {"password": "hunter2", "token": "abc", "email": "a@b.com", "note": "fine"}
    out = health_snapshot.scrub(obj)
    assert out["password"] == "[redacted]"
    assert out["token"] == "[redacted]"
    assert out["email"] == "[redacted]"
    assert out["note"] == "fine"


# ------------------------------------------------------------------ scrub: preserves normal data
def test_scrub_preserves_ordinary_values():
    obj = {
        "status": "ok",
        "verdict": "HEALTHY",
        "count": 42,
        "ratio": 3.14,
        "alive": True,
        "empty": None,
        "sha": "deadbee",          # 7-char git short sha -- must NOT be redacted
        "detail": "last heartbeat 5s ago",
        "words": "the quick brown fox",
    }
    out = health_snapshot.scrub(obj)
    assert out == obj  # untouched


def test_scrub_does_not_mutate_input():
    obj = {"email": "a@b.com", "nested": {"token": "x"}}
    _ = health_snapshot.scrub(obj)
    assert obj["email"] == "a@b.com"
    assert obj["nested"]["token"] == "x"


def scrub_val(s: str):
    """Helper: scrub a bare string by wrapping it so we test the string-pattern path."""
    return health_snapshot.scrub({"detail": s})["detail"]


# ------------------------------------------------------------------ build_health_snapshot
def test_build_health_snapshot_shape(repo):
    snap = health_snapshot.build_health_snapshot(repo)
    assert snap["schema"] == "sleepctl.health/v1"
    assert "verdict" in snap
    assert isinstance(snap["checks"], list)
    assert "generated_utc" in snap
    # each check must carry EXACTLY the five whitelisted keys
    for c in snap["checks"]:
        assert set(c.keys()) == {"id", "title", "status", "detail", "remedy"}


def test_build_health_snapshot_has_no_secret_or_biometric_keys(repo):
    snap = health_snapshot.build_health_snapshot(repo)
    secret_substrings = ("password", "secret", "token", "email", "recipient",
                         "authorization", "cookie", "bearer")
    biometric_keys = {"heart_rate", "hr", "hrv", "respiratory_rate", "bcg"}
    for kind, val in _walk_keys_and_strings(snap):
        if kind != "key":
            continue
        low = val.lower()
        for sub in secret_substrings:
            assert sub not in low, f"secret-like key leaked: {val}"
        assert val not in biometric_keys, f"biometric key leaked: {val}"


def test_build_health_snapshot_survives_diag_failure(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("diag exploded")

    monkeypatch.setattr(health_snapshot, "_run_diagnostics", boom)
    snap = health_snapshot.build_health_snapshot(repo)
    assert snap["schema"] == "sleepctl.health/v1"
    assert snap["verdict"] == "unknown"
    assert "diag exploded" in snap["error"]
    assert "generated_utc" in snap


# ------------------------------------------------------------------ snapshot_json_bytes
def test_snapshot_json_bytes_roundtrips(repo):
    snap = health_snapshot.build_health_snapshot(repo)
    raw = health_snapshot.snapshot_json_bytes(snap)
    assert isinstance(raw, bytes)
    assert raw.endswith(b"\n")
    parsed = json.loads(raw)
    assert parsed["schema"] == "sleepctl.health/v1"
    assert parsed == snap


# ------------------------------------------------------------------ write_snapshot
def test_write_snapshot_writes_parseable_file(tmp_path):
    db_path = str(tmp_path / "ws_test.db")
    out_path = str(tmp_path / "nested" / "out" / "latest.json")
    returned = health_snapshot.write_snapshot(db_path, out_path)
    assert returned == out_path
    with open(out_path, "r", encoding="utf-8") as fh:
        parsed = json.load(fh)
    assert parsed["schema"] == "sleepctl.health/v1"
    assert "verdict" in parsed


# ------------------------------------- operational log tails (2026-08-28 remote-debug gap)
def test_log_tails_are_published_so_a_remote_operator_can_see_WHY(tmp_path):
    """Checks say WHAT is wrong; the forwarder's own log says WHY. Without it, diagnosing a
    26-hour wearable outage required someone to read a file off the box by hand -- exactly the
    loop this GitHub relay exists to remove."""
    run = tmp_path / ".run"
    run.mkdir()
    (run / "verity.log").write_text(
        "\n".join(f"2026-08-28 01:0{i}  no Polar/HR sensor found this scan; will retry"
                  for i in range(9)), encoding="utf-8")
    tails = health_snapshot._log_tails(str(run))
    assert "verity" in tails
    assert any("no Polar/HR sensor found" in ln for ln in tails["verity"])


def test_log_tails_are_bounded(tmp_path):
    run = tmp_path / ".run"
    run.mkdir()
    (run / "verity.log").write_text("\n".join(str(i) for i in range(5000)), encoding="utf-8")
    assert len(health_snapshot._log_tails(str(run))["verity"]) <= 40


def test_missing_logs_are_simply_absent_not_an_error(tmp_path):
    run = tmp_path / ".run"
    run.mkdir()
    assert health_snapshot._log_tails(str(run)) == {}


def test_log_tails_appear_in_the_published_snapshot(repo, tmp_path, monkeypatch):
    run = tmp_path / ".run"
    run.mkdir()
    (run / "verity.log").write_text("connecting to AA:BB:CC ...\n", encoding="utf-8")
    snap = health_snapshot.build_health_snapshot(repo, run_dir=str(run))
    assert "log_tails" in snap
    assert any("connecting to" in ln for ln in snap["log_tails"].get("verity", []))


def test_a_token_in_a_log_line_is_still_scrubbed(repo, tmp_path):
    """Log tails go through the same belt-and-suspenders scrub as every other field."""
    run = tmp_path / ".run"
    run.mkdir()
    (run / "verity.log").write_text(
        "forwarding to http://localhost:8000/hr/ingest?token=" + ("a" * 40) + "\n",
        encoding="utf-8")
    snap = health_snapshot.build_health_snapshot(repo, run_dir=str(run))
    blob = repr(snap.get("log_tails"))
    assert "a" * 40 not in blob


# ------------------------------------- funnel URL publishing (2026-08-28, operator-approved)
def test_the_funnel_url_is_published_when_the_watchdog_recorded_one(tmp_path):
    """The difference between polling a 3-minute snapshot and actually debugging: with the URL
    an off-site operator can query /api/diag live and on demand."""
    run = tmp_path / ".run"
    run.mkdir()
    (run / "funnel.url").write_text("https://box.tailnet-abc.ts.net\n", encoding="utf-8")
    assert health_snapshot._funnel_url(str(run)) == "https://box.tailnet-abc.ts.net"


def test_no_funnel_recorded_is_simply_absent(tmp_path):
    run = tmp_path / ".run"
    run.mkdir()
    assert health_snapshot._funnel_url(str(run)) is None


def test_the_funnel_url_survives_the_scrub(repo, tmp_path):
    """The scrub must NOT redact this one. It is a hostname, not a credential, and the entire
    point is for it to travel -- a redacted URL would silently defeat the feature."""
    run = tmp_path / ".run"
    run.mkdir()
    (run / "funnel.url").write_text("https://box.tailnet-abc.ts.net", encoding="utf-8")
    snap = health_snapshot.build_health_snapshot(repo, run_dir=str(run))
    assert snap["funnel_url"] == "https://box.tailnet-abc.ts.net"
