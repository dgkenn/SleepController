"""Dashboard API settings (env-driven; safe self-hosted defaults, no signups/keys)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


def _default_db() -> str:
    return os.environ.get("SLEEPCTL_DB", "/data/sleepctl.db")


@dataclass
class Settings:
    db_path: str = field(default_factory=_default_db)
    # Auth: secret is auto-generated if not supplied (the deploy entrypoint persists one).
    jwt_secret: str = field(default_factory=lambda: os.environ.get("JWT_SECRET") or secrets.token_hex(32))
    jwt_algorithm: str = "HS256"
    jwt_ttl_hours: int = int(os.environ.get("JWT_TTL_HOURS", "720"))  # 30 days (phone-friendly)
    # "Keep me logged in": a checked box issues a long-lived, persistent cookie that slides
    # forward on each app open (see /auth/me); unchecked issues a short-lived SESSION cookie
    # that ends when the browser is closed.
    jwt_remember_hours: int = int(os.environ.get("JWT_REMEMBER_HOURS", str(365 * 24)))  # 1 year
    jwt_session_hours: int = int(os.environ.get("JWT_SESSION_HOURS", "12"))  # a working day
    # Single-user bootstrap credentials (created on first run if no users exist).
    bootstrap_user: str = os.environ.get("DASHBOARD_USER", "admin")
    bootstrap_password: str = os.environ.get("DASHBOARD_PASSWORD", "changeme")
    cors_origins: list[str] = field(
        default_factory=lambda: os.environ.get("CORS_ORIGINS", "*").split(",")
    )
    runtime_stale_seconds: int = 180  # daemon snapshot older than this -> STALE
    # Drop auth on the phone-sensor endpoints (/bcg/ingest, /bcg/should-record) ONLY, so a
    # header-less device on a trusted LAN can stream without a token. Off by default; everything
    # else stays token-protected. Only enable when the API isn't exposed to the open internet.
    bcg_ingest_open: bool = field(
        default_factory=lambda: os.environ.get("BCG_INGEST_OPEN", "").strip().lower()
        in ("1", "true", "yes", "on"))

    # Web Push (VAPID). Keys are env-only, never committed — see deploy/.env.example.
    # Generate with: python3 -m app.push_keys  (prints a keypair; requires the optional
    # `py_vapid`/`pywebpush` dependency — see requirements.txt). Left blank, /push/subscribe
    # still records subscriptions but sending is a documented no-op until keys are set.
    vapid_public_key: str = field(default_factory=lambda: os.environ.get("VAPID_PUBLIC_KEY", ""))
    vapid_private_key: str = field(default_factory=lambda: os.environ.get("VAPID_PRIVATE_KEY", ""))
    vapid_subject: str = field(
        default_factory=lambda: os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com"))


settings = Settings()
