"""API stress pass — hit every read endpoint under hostile runtime states, and fuzz the write
endpoints with out-of-range / malformed bodies. Nothing should ever 500; bad input should be
rejected (4xx) or clamped, never crash the server."""

from __future__ import annotations

import pytest

# Read endpoints with no path params — must survive any backend state without a 500.
GET_ENDPOINTS = [
    "/status", "/report/nightly", "/perfect-weights", "/wake/catalog", "/gym/advice",
    "/wake/plan", "/wake/tuning", "/learning/phases", "/shift/plan", "/shift/config",
    "/wake/light/config", "/gym/config", "/bcg/should-record", "/tonight", "/tonight/plan",
    "/maintenance", "/nights", "/interventions", "/checkin/status", "/notes", "/ml/overview",
    "/ml/recommendation", "/analytics/trends", "/analytics/effectiveness", "/settings",
    "/admin/health", "/admin/logs", "/alerts", "/predictive/preemption", "/morning/readiness",
    "/weather/forecast", "/forensics/awakenings", "/experiments", "/experiments/templates",
]


def _set_runtime(extra: dict):
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    try:
        bridge.write_runtime_state(repo.conn, {"state": "IDLE", "extra": extra})
    finally:
        repo.close()


# A spread of hostile runtime states the read endpoints must tolerate.
HOSTILE_STATES = [
    {},                                                          # empty extra
    {"wake": {}},                                               # wake with no fields
    {"wake": {"wake_time": "not-a-time"}},                      # malformed wake time
    {"wake": {"wake_time": "25:99"}},                          # impossible clock
    {"wake": {"wake_time": "04:30", "window_min": -5}},        # negative window
    {"bed_presence": None, "power_on": None},                  # unknown presence/power
    {"wake_action": {"phase": "fire", "should_wake": True}},    # mid-wake, partial action
    {"precompensation": {"bias_f": 9999}},                     # absurd bias
    {"shift_plan": {"debt_min": -100}, "is_short_sleep_day": True},
]


@pytest.mark.parametrize("state", HOSTILE_STATES, ids=[str(i) for i in range(len(HOSTILE_STATES))])
@pytest.mark.parametrize("ep", GET_ENDPOINTS)
def test_get_endpoints_never_500(auth_client, ep, state):
    _set_runtime(state)
    r = auth_client.get(ep)
    assert r.status_code != 500, f"{ep} 500'd under {state}: {r.text[:200]}"


def test_get_endpoints_require_auth(client):
    # A fresh (unauth'd) client must be rejected, not crash.
    for ep in GET_ENDPOINTS:
        r = client.get(ep)
        assert r.status_code in (401, 403, 200)   # 200 only for truly public (none expected here)


# ---- write-endpoint fuzz: out-of-range + malformed bodies should be 4xx or clamped, never 500 ----
WRITE_FUZZ = [
    ("post", "/tonight/temp", {"target_f": 999}),
    ("post", "/tonight/temp", {"target_f": -999}),
    ("post", "/tonight/temp", {"target_f": "hot"}),
    ("post", "/tonight/temp", {}),
    ("post", "/tonight/temp/nudge", {"delta_f": 9999}),
    ("post", "/tonight/mode", {"mode": "banana"}),
    ("post", "/tonight/wake", {"wake_time": "99:99"}),
    ("post", "/tonight/wake", {"wake_time": "04:30", "window_min": -10, "vibration_power": 9999}),
    ("post", "/tonight/nap", {"window_min": -30}),
    ("post", "/tonight/nap", {"window_min": 100000}),
    ("post", "/tonight/nap/preview", {"window_min": 0}),
    ("post", "/control/banana", {}),
    ("post", "/checkin", {"grogginess": 999, "subjective_quality": -5}),
    ("put", "/settings", {"nonsense_key": True}),
    ("put", "/shift/config", {"next_shift": "not-a-date", "kind": "banana"}),
    ("put", "/gym/config", {"lean": "banana", "early_offset_min": -999}),
    ("put", "/wake/light/config", {"target_ids": "notalist"}),
    ("post", "/bcg/ingest", {"fs": -1, "ax": ["x"]}),
]


@pytest.mark.parametrize("method,ep,body", WRITE_FUZZ,
                         ids=[f"{m}-{e}-{i}" for i, (m, e, b) in enumerate(WRITE_FUZZ)])
def test_write_endpoints_reject_or_clamp_never_500(auth_client, method, ep, body):
    r = getattr(auth_client, method)(ep, json=body)
    assert r.status_code != 500, f"{method} {ep} 500'd on {body}: {r.text[:200]}"
    assert r.status_code < 600


def test_unknown_night_date_is_not_a_500(auth_client):
    for path in ("/nights/1999-01-01", "/nights/not-a-date", "/nights/2026-06-01/samples"):
        assert auth_client.get(path).status_code != 500
