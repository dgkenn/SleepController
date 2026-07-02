"""Tests for GET /diag/bundle (the "send this to Claude" single-artifact diagnostic bundle) --
same token gating as /diag, and (critically) that secret env values NEVER appear in either the
text or zip output while non-secret ones do."""

from __future__ import annotations

import io
import zipfile

import pytest


SECRET_PASSWORD = "hunter2-super-secret-password"
SECRET_ICS_URL = "https://calendar.example.com/private/abc123def456/basic.ics"
SECRET_JWT = "totally-secret-jwt-signing-key"


def _set_secrets(monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("EIGHTSLEEP_PASSWORD", SECRET_PASSWORD)
    monkeypatch.setenv("CALENDAR_ICS_URL", SECRET_ICS_URL)
    monkeypatch.setenv("JWT_SECRET", SECRET_JWT)
    monkeypatch.setenv("SLEEPCTL_DRY_RUN", "1")  # non-secret -- must appear verbatim


# ------------------------------------------------------------------ token gate
def test_diag_bundle_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/bundle").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/bundle").status_code == 404          # no token
    assert client.get("/diag/bundle?token=nope").status_code == 404  # wrong token


def test_diag_playbook_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/playbook").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/playbook").status_code == 404
    assert client.get("/diag/playbook?token=nope").status_code == 404


# ------------------------------------------------------------------ text bundle: shape + redaction
def test_diag_bundle_text_is_sectioned_and_never_leaks_secrets(client, monkeypatch):
    _set_secrets(monkeypatch)

    r = client.get("/diag/bundle?token=s3cret-xyz")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text

    # sectioned, per spec
    assert "===== SLEEPCONTROLLER DIAGNOSTIC BUNDLE =====" in body
    assert "===== DIAGNOSIS (summary) =====" in body
    assert "===== DIAG (full JSON) =====" in body
    assert "===== RECENT EVENTS (JSON) =====" in body
    assert "===== HEARTBEATS =====" in body
    assert "===== RESULT / ALERT FILES =====" in body
    assert "===== LOG: daemon =====" in body
    assert "===== CONFIG SNAPSHOT (redacted -- secret values NEVER included) =====" in body

    # secrets NEVER present, no matter how deep in the doc
    assert SECRET_PASSWORD not in body
    assert SECRET_ICS_URL not in body
    assert SECRET_JWT not in body
    assert "<redacted>" in body

    # non-secret values DO appear (redaction isn't blanket-hiding everything)
    assert "SLEEPCTL_DRY_RUN = 1" in body


def test_diag_bundle_size_is_capped_near_1mb(client, monkeypatch):
    _set_secrets(monkeypatch)
    r = client.get("/diag/bundle?token=s3cret-xyz")
    assert r.status_code == 200
    assert len(r.content) <= 1_050_000  # ~1MB cap + small slack for the truncation notice


# ------------------------------------------------------------------ zip bundle
def test_diag_bundle_zip_format_never_leaks_secrets(client, monkeypatch):
    _set_secrets(monkeypatch)

    r = client.get("/diag/bundle?token=s3cret-xyz&format=zip")
    assert r.status_code == 200
    assert "zip" in r.headers["content-type"]
    assert "attachment" in r.headers.get("content-disposition", "")

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "diag.json" in names
    assert "diagnosis_summary.txt" in names
    assert "events.json" in names
    assert "config_redacted.txt" in names
    assert any(n.startswith("logs/") for n in names)

    all_bytes = b"".join(zf.read(n) for n in names)
    assert SECRET_PASSWORD.encode() not in all_bytes
    assert SECRET_ICS_URL.encode() not in all_bytes
    assert SECRET_JWT.encode() not in all_bytes

    config_txt = zf.read("config_redacted.txt").decode()
    assert "<redacted>" in config_txt
    assert "SLEEPCTL_DRY_RUN = 1" in config_txt


# ------------------------------------------------------------------ redaction unit coverage
def test_is_secret_key_matches_the_documented_patterns():
    from app.diag_bundle import is_secret_key

    for key in ("EIGHTSLEEP_PASSWORD", "DASHBOARD_PASSWORD", "JWT_SECRET",
               "EIGHTSLEEP_CLIENT_SECRET", "CALENDAR_ICS_URL", "DIAG_TOKEN",
               "jwt_secret", "some_token"):
        assert is_secret_key(key), key

    for key in ("SLEEPCTL_DRY_RUN", "SLEEPCTL_LIVE", "TZ", "CORS_ORIGINS",
               "EIGHTSLEEP_EMAIL", "DASHBOARD_USER"):
        assert not is_secret_key(key), key


def test_redacted_env_lines_never_returns_the_real_secret_value():
    from app.diag_bundle import redacted_env_lines

    lines = redacted_env_lines({"EIGHTSLEEP_PASSWORD": SECRET_PASSWORD,
                                "SLEEPCTL_DRY_RUN": "1"})
    joined = "\n".join(lines)
    assert SECRET_PASSWORD not in joined
    assert "EIGHTSLEEP_PASSWORD = <redacted>" in joined
    assert "SLEEPCTL_DRY_RUN = 1" in joined
