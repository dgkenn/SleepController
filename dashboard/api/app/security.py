"""Auth: single-user, stdlib-only JWT (HS256) + PBKDF2 password hashing.

No external crypto deps (the host's `cryptography`/`jose` are broken), and no third-party
identity provider. A bootstrap user is created on first run from env; the JWT secret is
auto-generated if not provided.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time

from fastapi import Depends, HTTPException, Request, status

from app.config import settings
from app.db import get_repo

_PBKDF2_ITERS = 200_000


# ------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ------------------------------------------------------------------- JWT (HS256)
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(username: str, ttl_hours: int | None = None, remember: bool = False) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    hours = ttl_hours if ttl_hours is not None else settings.jwt_ttl_hours
    exp = int(time.time()) + hours * 3600
    # ``rmb`` records that the user asked to stay signed in, so the session can be slid forward
    # on activity (see /auth/me) rather than hard-expiring while the app is in active use.
    payload = _b64(json.dumps({"sub": username, "exp": exp, "rmb": bool(remember)}).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(settings.jwt_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


def decode_token(token: str) -> dict:
    try:
        header, payload, sig = token.split(".")
        signing_input = f"{header}.{payload}".encode()
        expected = hmac.new(settings.jwt_secret.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            raise ValueError("bad signature")
        claims = json.loads(_b64d(payload))
        if claims.get("exp", 0) < time.time():
            raise ValueError("expired")
        return claims
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session") from exc


# ------------------------------------------------------------------- users / deps
def ensure_bootstrap_user() -> None:
    repo = get_repo()
    try:
        if repo.conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            from datetime import datetime, timezone
            repo.conn.execute(
                "INSERT INTO users (username, password_hash, role, created) VALUES (?,?,?,?)",
                (settings.bootstrap_user, hash_password(settings.bootstrap_password),
                 "owner", datetime.now(timezone.utc).isoformat()))
            repo.conn.commit()
    finally:
        repo.close()


def authenticate(username: str, password: str) -> bool:
    repo = get_repo()
    try:
        row = repo.conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        return bool(row and verify_password(password, row["password_hash"]))
    finally:
        repo.close()


def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("session")


# --------------------------------------------------------- no login on the home network
# The owner asked for no login page at home (2026-10-05): the dashboard is opened from the
# phone on the home Wi-Fi, and a password there only got in the way. A request is treated as
# the owner's when it plainly came from the LAN; anything that could have come from the
# internet -- the Tailscale Funnel URL, any request carrying Tailscale's headers, a ts.net host,
# a public address anywhere in the path -- still needs the login, because that URL reaches the
# bed from the whole internet. SLEEPCTL_LAN_NO_LOGIN=0 turns the exemption off.
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12",
                                                      "192.168.0.0/16"))


def _ip(value: str):
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


def _is_lan(value: str) -> bool:
    ip = _ip(value)
    if ip is not None and getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip is not None and any(ip in n for n in _LAN_NETS)


def _is_loopback(value: str) -> bool:
    ip = _ip(value)
    if ip is not None and getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip is not None and ip.is_loopback


def lan_request(request: Request) -> bool:
    """True when this request plainly came from a device on the home network."""
    if os.environ.get("SLEEPCTL_LAN_NO_LOGIN", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    h = request.headers
    # Tailscale serve / Funnel adds Tailscale-* headers to what it proxies: never exempt those.
    if any(k.lower().startswith("tailscale-") for k in h.keys()):
        return False
    # The address the browser typed: the web server passes it on as X-Forwarded-Host. It must
    # be a LAN address (http://192.168.x.y:3000), not the ts.net name.
    host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip()
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not _is_lan(hostname):
        return False
    # Every hop the request passed through must be the LAN or this machine, and at least one
    # must be a LAN device: a public address anywhere in the chain means it came from outside.
    hops = [x.strip() for x in h.get("x-forwarded-for", "").split(",") if x.strip()]
    if request.client and request.client.host:
        hops.append(request.client.host)
    if not hops or not all(_is_lan(x) or _is_loopback(x) for x in hops):
        return False
    return any(_is_lan(x) for x in hops)


def current_user(request: Request) -> str:
    token = _token_from_request(request)
    if token:
        try:
            return decode_token(token)["sub"]
        except HTTPException:
            if not lan_request(request):
                raise
    if lan_request(request):
        return settings.bootstrap_user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")


AuthDep = Depends(current_user)
