"""The default on the box: no login screen anywhere (owner's request, 2026-11-18).

Two things stay guarded with the login gone, because they reach past the bed to the PC: the
Tailscale join URL (home network only) and the /diag actions (their own DIAG_TOKEN)."""
import pytest
from fastapi.testclient import TestClient

from app import health_snapshot
from app.main import app

FUNNEL = {"x-forwarded-host": "box.tailnet-abc.ts.net", "x-forwarded-for": "203.0.113.9",
          "tailscale-funnel-request": "?1"}
LAN = {"x-forwarded-host": "192.168.1.163:3000", "x-forwarded-for": "192.168.1.42"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("SLEEPCTL_NO_LOGIN", raising=False)  # the shipped default
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.mark.parametrize("headers", [{}, LAN, FUNNEL])
def test_no_login_is_needed_from_anywhere(client, headers):
    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 200 and r.json()["user"]
    assert client.get("/status", headers=headers).status_code == 200


def test_a_stale_cookie_does_not_lock_you_out(client):
    client.cookies.set("session", "not.a.token")
    assert client.get("/auth/me", headers=FUNNEL).status_code == 200


def test_the_tailscale_join_url_stays_home_network_only(client):
    assert client.get("/admin/tailscale-login", headers=FUNNEL).status_code == 401
    assert client.get("/admin/tailscale-login", headers=LAN).status_code == 200


def test_the_funnel_url_is_not_published_while_the_login_is_off(client, tmp_path):
    run = tmp_path / ".run"
    run.mkdir()
    (run / "funnel.url").write_text("https://box.tailnet-abc.ts.net", encoding="utf-8")
    assert health_snapshot._funnel_url(str(run))  # recorded, just not published
    assert health_snapshot._public_funnel_url(str(run)) is None


def test_the_login_can_be_brought_back(client, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NO_LOGIN", "0")
    assert client.get("/auth/me", headers=FUNNEL).status_code == 401
