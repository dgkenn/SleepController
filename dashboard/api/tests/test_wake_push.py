"""The smart-wake push: the one wake channel Eight Sleep cannot paywall.

The Pod's vibration alarm write returns 403 without an active subscription, and no client can
work around a server-side refusal. Without this the wake degrades to the thermal ramp alone,
which is silent -- so the user was woken by their own phone alarm instead. The Web Push stack
already existed for critical alerts; this is the wake-side hook onto it.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app import push_sender as ps
from app import services
from app.db import get_repo


class _FakeTransport:
    def __init__(self):
        self.payloads = []

    def send(self, subscription, payload, vapid_private_key, vapid_claims):
        self.payloads.append(payload)


@contextmanager
def _repo():
    """The seeded suite DB (conftest's TestClient), which carries the API-side migrations --
    push_subscriptions lives there, not in the engine schema."""
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM push_subscriptions")
        repo.conn.execute("DELETE FROM settings_kv WHERE key = 'wake_push_last_sent'")
        repo.conn.commit()
        yield repo
    finally:
        repo.close()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "pub")
    monkeypatch.setattr(ps.settings, "vapid_subject", "mailto:test@example.com")


def _subscribe(repo):
    services.add_push_subscription(repo, "https://push.example/phone", "p1", "a1")


def test_no_subscription_is_a_clean_no_op(configured):
    """Nothing registered yet must report plainly rather than raise into the control loop."""
    with _repo() as repo:
        out = services.deliver_wake_push(repo, stage="light", minutes_early=12.0,
                                         night_date="2026-06-23")
        assert out["sent"] is False
        assert out["reason"] == "no_subscriptions"


def test_wake_push_is_sent_once_per_night(configured, monkeypatch):
    """The orchestrator re-asserts should_wake across several escalation ticks; re-pushing every
    60 s would itself become an alarm clock."""
    transport = _FakeTransport()
    monkeypatch.setattr(ps, "_default_transport", lambda: transport, raising=False)
    with _repo() as repo:
        _subscribe(repo)
        real = ps.deliver_custom
        monkeypatch.setattr(ps, "deliver_custom",
                            lambda **kw: real(**{**kw, "transport": transport}))

        first = services.deliver_wake_push(repo, stage="light", minutes_early=12.0,
                                           night_date="2026-06-23")
        second = services.deliver_wake_push(repo, stage="light", minutes_early=11.0,
                                            night_date="2026-06-23")

        assert first["sent"] is True
        assert second["sent"] is False and second["reason"] == "already_sent_tonight"
        assert len(transport.payloads) == 1
        assert "light" in transport.payloads[0]

        # a NEW night is allowed through again
        third = services.deliver_wake_push(repo, stage="light", minutes_early=9.0,
                                           night_date="2026-06-24")
        assert third["sent"] is True
        assert len(transport.payloads) == 2
