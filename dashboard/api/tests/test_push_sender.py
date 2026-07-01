"""Unit tests for the Web Push DECISION logic (app/push_sender.py).

Deliberately does NOT exercise the network/crypto path (pywebpush is an optional
dependency, not installed here) — instead it verifies: which issues should trigger a
send, that delivery is skipped cleanly when VAPID isn't configured, and that a fake
transport gets invoked once per subscription when it IS configured.
"""

from __future__ import annotations

from app import push_sender as ps


def test_should_notify_only_for_new_critical():
    critical_new = {"code": "no_water", "severity": "critical", "message": "x"}
    critical_seen = {"code": "no_water", "severity": "critical", "message": "x"}
    warning_new = {"code": "telemetry_stale", "severity": "warning", "message": "x"}

    assert ps.should_notify(critical_new, previously_active_codes=set()) is True
    assert ps.should_notify(critical_seen, previously_active_codes={"no_water"}) is False
    assert ps.should_notify(warning_new, previously_active_codes=set()) is False


def test_select_new_critical_filters_correctly():
    issues = [
        {"code": "daemon_down", "severity": "critical", "message": "a"},
        {"code": "telemetry_stale", "severity": "warning", "message": "b"},
        {"code": "no_water", "severity": "critical", "message": "c"},
    ]
    selected = ps.select_new_critical(issues, previously_active_codes={"no_water"})
    assert [i["code"] for i in selected] == ["daemon_down"]


def test_select_new_critical_empty_when_all_seen():
    issues = [{"code": "daemon_down", "severity": "critical", "message": "a"}]
    assert ps.select_new_critical(issues, previously_active_codes={"daemon_down"}) == []


def test_build_payload_contains_message_and_tag():
    issue = {"code": "no_water", "severity": "critical", "message": "Refill the tank."}
    import json
    payload = json.loads(ps.build_payload(issue))
    assert payload["body"] == "Refill the tank."
    assert payload["tag"] == "no_water"
    assert payload["severity"] == "critical"


def test_vapid_configured_false_by_default(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "")
    assert ps.vapid_configured() is False


def test_deliver_without_vapid_is_a_clean_noop(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "")
    issue = {"code": "no_water", "severity": "critical", "message": "x"}
    result = ps.deliver(issue, subscriptions=[{"endpoint": "e", "p256dh": "p", "auth": "a"}])
    assert result.ok is False
    assert result.reason == "vapid_not_configured"


def test_deliver_with_no_subscriptions(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "pub")
    issue = {"code": "no_water", "severity": "critical", "message": "x"}
    result = ps.deliver(issue, subscriptions=[])
    assert result.ok is False
    assert result.reason == "no_subscriptions"


class _FakeTransport:
    def __init__(self, fail_endpoints=()):
        self.calls = []
        self.fail_endpoints = set(fail_endpoints)

    def send(self, subscription, payload, vapid_private_key, vapid_claims):
        self.calls.append(subscription["endpoint"])
        if subscription["endpoint"] in self.fail_endpoints:
            raise RuntimeError("simulated push failure")


def test_deliver_calls_transport_once_per_subscription(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "pub")
    monkeypatch.setattr(ps.settings, "vapid_subject", "mailto:test@example.com")
    issue = {"code": "no_water", "severity": "critical", "message": "Refill."}
    subs = [
        {"endpoint": "https://push.example/a", "p256dh": "p1", "auth": "a1"},
        {"endpoint": "https://push.example/b", "p256dh": "p2", "auth": "a2"},
    ]
    transport = _FakeTransport()
    result = ps.deliver(issue, subs, transport=transport)
    assert transport.calls == ["https://push.example/a", "https://push.example/b"]
    assert result.ok is True
    assert result.sent == 2
    assert result.failed == 0


def test_deliver_reports_partial_failure(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "pub")
    issue = {"code": "no_water", "severity": "critical", "message": "Refill."}
    subs = [
        {"endpoint": "ok", "p256dh": "p1", "auth": "a1"},
        {"endpoint": "bad", "p256dh": "p2", "auth": "a2"},
    ]
    transport = _FakeTransport(fail_endpoints={"bad"})
    result = ps.deliver(issue, subs, transport=transport)
    assert result.sent == 1
    assert result.failed == 1
    assert result.ok is True  # at least one delivery succeeded


def test_deliver_all_fail(monkeypatch):
    monkeypatch.setattr(ps.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(ps.settings, "vapid_public_key", "pub")
    issue = {"code": "no_water", "severity": "critical", "message": "Refill."}
    subs = [{"endpoint": "bad", "p256dh": "p2", "auth": "a2"}]
    transport = _FakeTransport(fail_endpoints={"bad"})
    result = ps.deliver(issue, subs, transport=transport)
    assert result.ok is False
    assert result.reason == "all_failed"
