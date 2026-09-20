"""Can a phone on the same network actually open the dashboard?

2026-09-19/20: every check on the box was green -- port 3000 listening, build current, API up,
daemon ticking -- and the site could not be opened. The inbound firewall rule for port 3000 had
been created once, at first start, for the PRIVATE profile only; nothing re-checked it and
nothing published the address to open.
"""
import json
import os

import pytest

from app import diagnostics, health_snapshot


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / ".run"
    d.mkdir()
    return str(d)


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "lan.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _state(run_dir, **kw):
    st = {"url": "http://192.168.1.50:3000", "ips": ["192.168.1.50"],
          "profiles": ["Home=Private"], "rules": ["private+domain"], "public": False,
          "ts": "2026-09-20T00:00:00-04:00"}
    st.update(kw)
    with open(os.path.join(run_dir, "lan.state"), "w", encoding="utf-8") as fh:
        json.dump(st, fh)


def test_no_state_yet_is_info(run_dir):
    c = diagnostics._check_lan_access(run_dir)
    assert c["status"] == "info"


def test_a_private_network_reports_the_url_to_open(run_dir):
    _state(run_dir)
    c = diagnostics._check_lan_access(run_dir)
    assert c["status"] == "ok"
    assert "http://192.168.1.50:3000" in c["detail"]


def test_a_public_network_without_the_subnet_rule_is_the_phone_being_blocked(run_dir):
    _state(run_dir, public=True, profiles=["Home=Public"], rules=["private+domain"])
    c = diagnostics._check_lan_access(run_dir)
    assert c["status"] == "warn"
    assert "PUBLIC" in c["detail"]
    assert "Private" in (c["remedy"] or "")


def test_a_public_network_with_the_subnet_rule_is_fine(run_dir):
    _state(run_dir, public=True, profiles=["Home=Public"],
           rules=["private+domain", "public/localsubnet"])
    c = diagnostics._check_lan_access(run_dir)
    assert c["status"] == "ok"
    assert "local subnet only" in c["detail"]


def test_no_lan_address_at_all_is_a_warning(run_dir):
    _state(run_dir, url=None, ips=[])
    c = diagnostics._check_lan_access(run_dir)
    assert c["status"] == "warn"


def test_the_lan_url_is_published_in_the_snapshot(repo, tmp_path):
    run = tmp_path / ".run"
    run.mkdir()
    _state(str(run))
    snap = health_snapshot.build_health_snapshot(repo, run_dir=str(run))
    assert snap["lan_url"] == "http://192.168.1.50:3000"


def test_the_web_logs_are_published(repo, tmp_path):
    """A dashboard that will not load leaves its reason only in next.js's own output."""
    run = tmp_path / ".run"
    run.mkdir()
    (run / "web.err").write_text("Error: listen EADDRINUSE 0.0.0.0:3000\n", encoding="utf-8")
    snap = health_snapshot.build_health_snapshot(repo, run_dir=str(run))
    assert any("EADDRINUSE" in ln for ln in snap["log_tails"].get("web_err", []))


# ------------------------------------------------- the pending tailscale login is a credential
def test_a_pending_login_is_reported_without_publishing_the_url(run_dir):
    secret = "https://login.tailscale.com/a/0123456789abcdef"
    with open(os.path.join(run_dir, "tailscale.state"), "w", encoding="utf-8") as fh:
        fh.write("NoState (backend not running)")
    with open(os.path.join(run_dir, "tailscale-login.url"), "w", encoding="utf-8") as fh:
        fh.write(secret)
    c = diagnostics._check_remote_access(run_dir)
    assert c["status"] == "warn"
    assert "browser login is waiting" in c["detail"]
    assert secret not in json.dumps(c)
    assert "login.tailscale.com" not in json.dumps(c)


def test_the_login_url_is_served_only_to_an_authenticated_session(auth_client):
    # A FRESH client, so it carries none of the suite's session cookies.
    from starlette.testclient import TestClient
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as anon:
        assert anon.get("/admin/tailscale-login").status_code in (401, 403)
    r = auth_client.get("/admin/tailscale-login")
    assert r.status_code == 200
    assert "login_url" in r.json()


def test_remote_access_never_decides_the_verdict_even_with_a_pending_login(run_dir):
    from app.diagnostics import _aggregate, _check
    checks = [_check("daemon_heartbeat", "Daemon", "ok", "fine"),
              _check("remote_access", "Remote access", "warn", "login waiting", "tap the link")]
    assert _aggregate(checks)[0] == "HEALTHY"
