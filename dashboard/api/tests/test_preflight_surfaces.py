"""The two REMOTE surfaces for the GO/NO-GO verdict.

The CLI preflight is only useful standing at the machine. The situation it is actually needed in is
the opposite one: away from the box, deciding whether tonight will work. So the verdict has to be
reachable over HTTP from a phone, and present in the published health snapshot — which is the only
window into the box from off-site.

The property both share: the verdict must be derived from the SAME battery whose checks are being
reported, never a second sample that can disagree with what's printed beside it.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "pf_surface.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


# ------------------------------------------------------------------ the health snapshot
def test_snapshot_carries_a_preflight_verdict(repo, tmp_path):
    from app.health_snapshot import build_health_snapshot

    snap = build_health_snapshot(repo, run_dir=str(tmp_path))
    pf = snap.get("preflight")
    assert pf and pf.get("available") is True
    assert pf["verdict"] in ("GO", "GO_DEGRADED", "NO_GO")
    assert isinstance(pf["blocking"], list) and isinstance(pf["degraded"], list)


def test_snapshot_preflight_agrees_with_its_own_checks(repo, tmp_path):
    """Built from the checks published beside it, so the two can't contradict each other."""
    from app.health_snapshot import build_health_snapshot

    snap = build_health_snapshot(repo, run_dir=str(tmp_path))
    ids = {c["id"] for c in snap["checks"]}
    for item in snap["preflight"]["blocking"] + snap["preflight"]["degraded"]:
        # every reported item must correspond to a check in the same snapshot (api is the one
        # exception -- it's replaced by a real socket probe, see preflight.api_port_open)
        assert item["id"] in ids or item["id"] == "api", item


def test_snapshot_preflight_is_json_serializable(repo, tmp_path):
    from app.health_snapshot import build_health_snapshot, snapshot_json_bytes

    snap = build_health_snapshot(repo, run_dir=str(tmp_path))
    round_tripped = json.loads(snapshot_json_bytes(snap))
    assert round_tripped["preflight"]["verdict"] == snap["preflight"]["verdict"]


def test_snapshot_survives_a_broken_preflight(repo, tmp_path, monkeypatch):
    """A preflight failure must cost the preflight block, never the whole snapshot."""
    import sleepctl.preflight as pf_mod
    from app.health_snapshot import build_health_snapshot

    def _boom(*a, **k):
        raise RuntimeError("preflight exploded")

    monkeypatch.setattr(pf_mod, "evaluate", _boom)
    snap = build_health_snapshot(repo, run_dir=str(tmp_path))
    assert snap["verdict"] is not None, "the rest of the snapshot must still be built"
    assert snap["preflight"]["available"] is False
    assert "exploded" in snap["preflight"]["error"]


def test_snapshot_preflight_contains_no_biometrics(repo, tmp_path):
    """This branch is public. Operational metadata only."""
    from app.health_snapshot import build_health_snapshot

    snap = build_health_snapshot(repo, run_dir=str(tmp_path))
    blob = json.dumps(snap["preflight"]).lower()
    for forbidden in ("heart_rate", "hrv", "rr_ms", "bpm", "password", "token"):
        assert forbidden not in blob, forbidden


# ------------------------------------------------------------------ the HTTP endpoint
@pytest.fixture()
def diag_token(monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-preflight")
    return "s3cret-preflight"


def test_endpoint_is_invisible_without_the_token(client):
    assert client.get("/diag/preflight").status_code == 404


def test_endpoint_is_invisible_with_a_wrong_token(client, diag_token):
    assert client.get("/diag/preflight?token=nope").status_code == 404


def test_endpoint_returns_the_verdict(client, diag_token):
    r = client.get(f"/diag/preflight?token={diag_token}")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] in ("GO", "GO_DEGRADED", "NO_GO")
    assert "blocking" in body and "degraded" in body


def test_endpoint_renders_text_on_request(client, diag_token):
    r = client.get(f"/diag/preflight?token={diag_token}&format=text")
    assert r.status_code == 200
    assert "GO" in r.text


def test_sensor_flag_changes_the_verdict_basis(client, diag_token):
    """?sensor=0 is a Pod-only night: a silent Verity stops being blocking."""
    with_sensor = client.get(f"/diag/preflight?token={diag_token}&sensor=1").json()
    without = client.get(f"/diag/preflight?token={diag_token}&sensor=0").json()
    ids_with = {b["id"] for b in with_sensor["blocking"]}
    ids_without = {b["id"] for b in without["blocking"]}
    assert "cardiac_sensor" not in ids_without
    assert ids_without <= ids_with


def test_endpoint_never_500s_on_a_bare_database(client, diag_token):
    """Whatever state the box is in, this must answer -- it's what you reach for when it's sick."""
    r = client.get(f"/diag/preflight?token={diag_token}")
    assert r.status_code == 200
