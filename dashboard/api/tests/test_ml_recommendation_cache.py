"""Cache correctness for the ML recommendation surfaced in ``/status``/SSE.

``build_status`` used to call ``services.ml_recommendation`` directly, which refits an
O(all-history) ridge model (``recommend_action`` -> ``build_feature_rows`` ->
``SetpointModel().fit()``) on EVERY call -- and ``build_status`` is hit every 5s by the SSE
loop plus every ``/status`` request. That refit is meant to run once per night. These tests
pin down: (1) ``build_status`` never triggers the expensive path, it only reads whatever was
last cached, (2) ``ml_recommendation`` (the real computation, used by ``/ml/overview``,
``/tonight``, etc) writes through to that shared cache, and (3) the safe default before
anything has ever been computed.
"""

from __future__ import annotations

import pytest

from app import services


@pytest.fixture()
def repo(tmp_path):
    """A fresh Repository with the dashboard tables applied, isolated per test (same pattern
    as test_diagnostics.py's fixture)."""
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "ml_cache_test.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def test_cached_ml_recommendation_default_before_anything_computed(repo):
    """Brand-new DB, no night has ever closed out -- must degrade to the safe fallback shape
    WITHOUT calling recommend_action (nothing has populated the cache yet)."""
    rec = services.cached_ml_recommendation(repo)
    assert rec["action"] == "rule-policy"
    assert rec["source"] == "fallback"
    assert rec["confidence"] == 0.0
    assert "mode" in rec


def test_ml_recommendation_writes_through_to_the_cache(repo, monkeypatch):
    """A real (fresh) computation via ``ml_recommendation`` must populate the cache that
    ``cached_ml_recommendation`` (and therefore ``build_status``) reads."""
    calls = {"n": 0}

    def _fake_recommend_action(repo_arg, profile, cfg, mode=None):
        calls["n"] += 1
        return None  # simulate "insufficient data" -> the rule-policy fallback shape

    monkeypatch.setattr(services, "recommend_action", _fake_recommend_action)

    fresh = services.ml_recommendation(repo)
    assert calls["n"] == 1
    assert fresh["action"] == "rule-policy"

    cached = services.cached_ml_recommendation(repo)
    assert cached == fresh
    # Reading the cache must NOT have triggered another computation.
    assert calls["n"] == 1


def test_build_status_never_recomputes_the_ml_recommendation(repo, monkeypatch):
    """The core regression this fix targets: build_status must read the cache and must NEVER
    call recommend_action/build_feature_rows/.fit() itself, even when the cache already holds
    a real ("ml") recommendation from an earlier computation."""
    # Seed the cache with a distinguishable "ml" recommendation via a real ml_recommendation()
    # call (as the nightly path / periodic watchdog refresh would do).
    class _FakeChosen:
        name = "cooler"
        reason = "test-seeded"
        confidence = 0.9
        predicted = {"wake_events": 1.0}

    monkeypatch.setattr(services, "recommend_action", lambda *a, **k: _FakeChosen())
    seeded = services.ml_recommendation(repo)
    assert seeded["action"] == "cooler" and seeded["source"] == "ml"

    # Now make ANY further recompute attempt fail loudly, and call build_status: it must
    # still succeed and show exactly the seeded ("last computed") recommendation.
    def _boom(*a, **k):
        raise AssertionError("build_status must not trigger recommend_action")

    monkeypatch.setattr(services, "recommend_action", _boom)
    status = services.build_status(repo)
    assert status["recommendation"]["action"] == "cooler"
    assert status["recommendation"]["reason"] == "test-seeded"


def test_refresh_ml_recommendation_cache_updates_what_build_status_shows(repo, monkeypatch):
    """Simulates the nightly-close-out / periodic-watchdog refresh path: calling
    ``refresh_ml_recommendation_cache`` (NOT part of any request path) is the only thing that
    should change what a subsequent build_status shows."""
    class _First:
        name = "warmer"; reason = "r1"; confidence = 0.5; predicted = {}

    class _Second:
        name = "hold"; reason = "r2"; confidence = 0.8; predicted = {}

    monkeypatch.setattr(services, "recommend_action", lambda *a, **k: _First())
    services.refresh_ml_recommendation_cache(repo)
    assert services.build_status(repo)["recommendation"]["action"] == "warmer"

    monkeypatch.setattr(services, "recommend_action", lambda *a, **k: _Second())
    services.refresh_ml_recommendation_cache(repo)
    assert services.build_status(repo)["recommendation"]["action"] == "hold"
