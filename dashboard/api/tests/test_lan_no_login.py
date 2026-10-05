"""No login on the home network; the login stays for anything that could come from outside."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

LAN = {"x-forwarded-host": "192.168.1.163:3000", "x-forwarded-for": "192.168.1.42"}


@pytest.fixture()
def client():
    # on the box the API is always called by the web server on this machine
    return TestClient(app, client=("127.0.0.1", 50000))


def test_the_phone_on_home_wifi_needs_no_login(client):
    r = client.get("/auth/me", headers=LAN)
    assert r.status_code == 200 and r.json()["user"]
    assert client.get("/status", headers=LAN).status_code == 200


def test_a_stale_cookie_on_the_lan_does_not_lock_you_out(client):
    client.cookies.set("session", "not.a.token")
    assert client.get("/auth/me", headers=LAN).status_code == 200


@pytest.mark.parametrize("headers", [
    {},                                                                   # no forwarding info
    {"x-forwarded-host": "john-desktop.tail712dd4.ts.net", "x-forwarded-for": "192.168.1.42"},
    {**LAN, "tailscale-funnel-request": "?1"},                            # came through Funnel
    {**LAN, "tailscale-user-login": "someone@example.com"},               # Tailscale serve
    {**LAN, "x-forwarded-for": "203.0.113.9, 192.168.1.42"},              # public hop in path
    {"x-forwarded-host": "192.168.1.163:3000", "x-forwarded-for": "127.0.0.1"},  # no LAN hop
    {"x-forwarded-host": "100.101.102.103:3000", "x-forwarded-for": "100.101.102.104"},
])
def test_anything_that_could_be_the_internet_still_needs_the_login(client, headers):
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_the_exemption_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_LAN_NO_LOGIN", "0")
    assert client.get("/auth/me", headers=LAN).status_code == 401
