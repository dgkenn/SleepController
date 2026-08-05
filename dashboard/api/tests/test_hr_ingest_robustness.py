"""Adversarial input on the Verity ingest path.

The forwarder is a BLE decoder feeding a network endpoint. Every value on that path originates in
a radio packet that can be truncated, duplicated, or garbled, and the codec turns bad bytes into
plausible-looking floats. This is the seam where a bad night starts, so it is worth being explicit
that nothing here can 500, wedge, or poison the fusion the controller reads.

The bar for each case: the API answers, the DB stays sane, and nothing physiologically absurd
reaches ``read_fused_sensor``.
"""

from __future__ import annotations

import math

import pytest


def _post_raw(client, body):
    """POST a body that may contain NaN/Inf.

    ``TestClient.post(json=...)`` serialises with httpx, which REFUSES non-finite floats and
    raises before any request is made -- so the NaN/Inf cases never reached the server and the
    tests failed on the client side while asserting nothing about the API. Real traffic can carry
    them: JSON parsers (including the one FastAPI uses) accept the bare ``NaN``/``Infinity``
    tokens Python's json emits with allow_nan=True, and a BLE codec turning garbled bytes into
    floats is exactly how they would arise. Serialise them ourselves and send raw bytes so the
    server path is genuinely exercised.
    """
    import json as _json

    return client.post("/hr/ingest",
                       content=_json.dumps(body, allow_nan=True),
                       headers={"content-type": "application/json"})


def _clear(conn):
    for t in ("live_cardiac", "rr_intervals", "actigraphy", "sensor_samples", "live_sensor"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


@pytest.fixture(autouse=True)
def _clean():
    from app.db import get_repo

    repo = get_repo()
    _clear(repo.conn)
    repo.close()
    yield
    repo = get_repo()
    _clear(repo.conn)
    repo.close()


# ------------------------------------------------------------------ malformed bodies
@pytest.mark.parametrize("body", [
    {},                                             # empty
    {"source": "verity"},                           # tag only, no data
    {"hr": None, "rr": None},                       # explicit nulls
    {"hr": "fast"},                                 # wrong type
    {"rr": "1000,1010"},                            # rr as a string
    {"hr": float("nan")},                           # NaN
    {"hr": float("inf")},                           # infinity
    {"rr": [float("nan"), float("inf")]},           # non-finite intervals
    {"hr": -50},                                    # negative
    {"hr": 0},                                      # zero
    {"hr": 1e12},                                   # absurd
    {"rr": []},                                     # empty batch
    {"rr": [0, 0, 0]},                              # zero intervals
    {"rr": [-1000.0]},                              # negative interval
    {"acc": "not-a-dict"},                          # wrong acc type
    {"hr": 60, "acc": {"pim": "lots"}},             # unparseable counts
    {"hr": 60, "acc": {}},                          # empty counts
    {"hr": 60, "acc": {"pim": float("nan")}},       # NaN counts
])
def test_malformed_batches_never_500(auth_client, body):
    r = _post_raw(auth_client, body)
    assert r.status_code in (200, 400, 422), f"{body} -> {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        assert isinstance(r.json().get("ok"), bool)


def test_absurd_hr_is_rejected_not_ingested(auth_client):
    """A garbled packet decoding to 1e12 bpm must never reach the controller as physiology."""
    from app import bridge
    from app.db import get_repo

    auth_client.post("/hr/ingest", json={"hr": 1e12, "source": "verity"})
    repo = get_repo()
    try:
        fused = bridge.read_fused_sensor(repo.conn)
        assert fused is None or fused.get("hr") is None or fused["hr"] < 240
    finally:
        repo.close()


def test_hr_derived_from_rr_ignores_out_of_band_intervals(auth_client):
    """RR values outside the physiological band would otherwise drag the derived HR anywhere."""
    r = auth_client.post("/hr/ingest",
                         json={"rr": [1000.0, 1010.0, 50000.0, 1.0], "source": "verity"})
    assert r.status_code == 200
    body = r.json()
    if body.get("ok") and body.get("hr") is not None:
        assert 25.0 <= body["hr"] <= 240.0, body


def test_a_giant_rr_batch_is_bounded(auth_client):
    """A reconnect storm could dump a huge backlog; it must be refused, not written."""
    from app.db import get_repo

    r = auth_client.post("/hr/ingest", json={"hr": 60, "rr": [1000.0] * 100000})
    assert r.status_code in (200, 400, 413, 422)
    if r.status_code == 200 and r.json().get("ok") is False:
        repo = get_repo()
        try:
            n = repo.conn.execute("SELECT COUNT(*) c FROM rr_intervals").fetchone()["c"]
            assert n == 0, "a refused batch must not partially persist"
        finally:
            repo.close()


def test_unicode_and_overlong_source_tags_are_handled(auth_client):
    for src in ("💤" * 50, "x" * 5000, "'; DROP TABLE live_cardiac; --"):
        r = auth_client.post("/hr/ingest", json={"hr": 58, "source": src})
        assert r.status_code in (200, 400, 422)

    from app.db import get_repo
    repo = get_repo()
    try:
        repo.conn.execute("SELECT COUNT(*) FROM live_cardiac").fetchone()  # table still there
    finally:
        repo.close()


# ------------------------------------------------------------------ fusion stays sane
def test_no_batch_can_put_a_non_finite_value_into_fusion(auth_client):
    """NaN/inf reaching the controller would silently poison every downstream comparison."""
    from app import bridge
    from app.db import get_repo

    for body in ({"hr": float("nan")}, {"hr": float("inf")},
                 {"rr": [float("nan"), float("inf")]},
                 {"hr": 60, "acc": {"pim": float("inf")}}):
        _post_raw(auth_client, body)

    repo = get_repo()
    try:
        fused = bridge.read_fused_sensor(repo.conn)
    finally:
        repo.close()
    if fused:
        for key in ("hr", "hrv", "movement"):
            v = fused.get(key)
            assert v is None or math.isfinite(v), f"{key}={v}"


def test_repeated_identical_batches_do_not_grow_the_live_row(auth_client):
    """live_cardiac is a singleton snapshot; a chatty forwarder must not accumulate rows."""
    from app.db import get_repo

    for _ in range(50):
        auth_client.post("/hr/ingest", json={"hr": 57, "rr": [1010.0], "source": "verity"})

    repo = get_repo()
    try:
        n = repo.conn.execute("SELECT COUNT(*) c FROM live_cardiac").fetchone()["c"]
        assert n == 1, f"live_cardiac should hold exactly one row, has {n}"
    finally:
        repo.close()


def test_ingest_is_idempotent_enough_to_survive_a_retry_storm(auth_client):
    """The forwarder retries on network blips; a duplicate must not corrupt anything."""
    from app import bridge
    from app.db import get_repo

    payload = {"hr": 56, "rr": [1030.0, 1042.0], "source": "verity"}
    for _ in range(20):
        assert auth_client.post("/hr/ingest", json=payload).status_code == 200

    repo = get_repo()
    try:
        fused = bridge.read_fused_sensor(repo.conn)
        assert fused["hr"] == 56.0
        assert fused["hrv"] is not None and math.isfinite(fused["hrv"])
    finally:
        repo.close()
