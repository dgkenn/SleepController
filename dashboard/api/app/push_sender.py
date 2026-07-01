"""Web Push sending: the DECISION of what/when to send is pure and unit-tested here;
the actual network+crypto delivery is an optional, lazily-imported side effect.

Why lazy: this repo intentionally avoids ``cryptography``/``jose``-family dependencies
for its stdlib-only JWT auth (see ``app/security.py``'s docstring — those libs have
caused problems in this deployment environment before). Real Web Push requires VAPID
(ECDSA P-256) signing, which pulls in ``pywebpush`` -> ``py_vapid`` -> ``cryptography``.
Rather than force that dependency on every install, ``pywebpush`` is imported only
inside ``deliver()``, at the moment a push is actually sent. Everything else (deciding
which new critical alerts should push, tracking what's already been notified, building
the payload, pruning dead subscriptions) has zero import-time dependency on it and is
fully testable without the network or the crypto stack.

If ``pywebpush`` isn't installed, or VAPID keys aren't configured, ``deliver()`` returns
a clear "not_configured" result instead of raising — subscribing still works today, and
push delivery lights up the moment the operator installs the optional dependency and
sets ``VAPID_PRIVATE_KEY``/``VAPID_PUBLIC_KEY``/``VAPID_SUBJECT`` in the environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.config import settings


@dataclass
class PushResult:
    ok: bool
    reason: str = ""
    sent: int = 0
    failed: int = 0
    stale_removed: int = 0


class PushTransport(Protocol):
    """Thin seam so tests can substitute a fake transport instead of the real network."""

    def send(self, subscription: dict, payload: str, vapid_private_key: str,
              vapid_claims: dict) -> None:
        ...


class WebPushTransport:
    """Real transport — imports ``pywebpush`` lazily so it's only required when a push
    is actually attempted, not at module import / app startup time."""

    def send(self, subscription: dict, payload: str, vapid_private_key: str,
              vapid_claims: dict) -> None:
        from pywebpush import WebPushException, webpush  # optional dependency

        try:
            webpush(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
                },
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=dict(vapid_claims),
            )
        except WebPushException:
            raise


def should_notify(issue: dict[str, Any], previously_active_codes: set[str]) -> bool:
    """The core decision: push only for a NEWLY-appearing critical issue.

    A 6-hour silent outage should become a phone buzz within one evaluation cycle — but
    we must NOT re-buzz every poll while the same issue stays open (that trains the user
    to ignore notifications), and must NOT push for warning/info severities. Re-fires
    correctly once the condition clears and reappears, because the caller recomputes
    ``previously_active_codes`` from what's currently un-acknowledged/open each cycle.
    """
    if issue.get("severity") != "critical":
        return False
    return issue.get("code") not in previously_active_codes


def select_new_critical(
    issues: list[dict[str, Any]], previously_active_codes: set[str]
) -> list[dict[str, Any]]:
    """All issues in ``issues`` that should trigger a push right now."""
    return [i for i in issues if should_notify(i, previously_active_codes)]


def build_payload(issue: dict[str, Any]) -> str:
    return json.dumps({
        "title": "SleepCtl alert",
        "body": issue.get("message", "A controller issue needs attention."),
        "tag": issue.get("code", "sleepctl-alert"),
        "severity": issue.get("severity", "critical"),
        "url": "/",
    })


def vapid_configured() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


def deliver(
    issue: dict[str, Any],
    subscriptions: list[dict[str, Any]],
    transport: PushTransport | None = None,
) -> PushResult:
    """Send ``issue`` to every subscription. Pure orchestration over the transport seam —
    unit tests pass a fake transport; production leaves ``transport`` None to use the
    real (lazily-imported) one."""
    if not vapid_configured():
        return PushResult(ok=False, reason="vapid_not_configured")
    if not subscriptions:
        return PushResult(ok=False, reason="no_subscriptions")

    transport = transport or WebPushTransport()
    payload = build_payload(issue)
    claims = {"sub": settings.vapid_subject}

    sent = failed = stale = 0
    dead_endpoints: list[str] = []
    for sub in subscriptions:
        try:
            transport.send(sub, payload, settings.vapid_private_key, claims)
            sent += 1
        except Exception as exc:  # pywebpush.WebPushException or any transport error
            failed += 1
            if _looks_stale(exc):
                dead_endpoints.append(sub.get("endpoint", ""))
                stale += 1

    return PushResult(ok=sent > 0, sent=sent, failed=failed, stale_removed=stale,
                       reason="" if sent else "all_failed")


def _looks_stale(exc: Exception) -> bool:
    """Best-effort: a 404/410 from the push service means the subscription is dead and
    should be pruned so we stop retrying it forever."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status in (404, 410)
