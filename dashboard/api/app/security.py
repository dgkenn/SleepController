"""Auth: single-user, stdlib-only JWT (HS256) + PBKDF2 password hashing.

No external crypto deps (the host's `cryptography`/`jose` are broken), and no third-party
identity provider. A bootstrap user is created on first run from env; the JWT secret is
auto-generated if not provided.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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


def create_token(username: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    exp = int(time.time()) + settings.jwt_ttl_hours * 3600
    payload = _b64(json.dumps({"sub": username, "exp": exp}).encode())
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


def current_user(request: Request) -> str:
    token = _token_from_request(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return decode_token(token)["sub"]


AuthDep = Depends(current_user)
