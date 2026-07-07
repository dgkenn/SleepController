"""Eight Sleep credential loading/saving.

Resolves credentials from (in priority order) explicit args, environment variables, then a
0600 JSON file (default ``~/.config/sleepctl/credentials.json``). Passwords in a local file
are convenient for a personal daemon; prefer env vars in shared environments.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


DEFAULT_PATH = Path.home() / ".config" / "sleepctl" / "credentials.json"

_ENV = {
    "email": "EIGHTSLEEP_EMAIL",
    "password": "EIGHTSLEEP_PASSWORD",
    "timezone": "EIGHTSLEEP_TIMEZONE",
    "side": "EIGHTSLEEP_SIDE",
    "client_id": "EIGHTSLEEP_CLIENT_ID",
    "client_secret": "EIGHTSLEEP_CLIENT_SECRET",
}


@dataclass
class Credentials:
    email: str = ""
    password: str = ""
    timezone: str = "UTC"
    # Physical sleeping side. This account reports ``currentDevice.side == "solo"`` (single
    # sleeper), so the side can't be auto-detected and must be configured; the owner sleeps on
    # the RIGHT, so that is the default. Commands hit both sides (shared profile), but per-side
    # READS (bed temp, presence, any future physiology) must come from the occupied side.
    # Override with EIGHTSLEEP_SIDE if the sleeper ever changes sides.
    side: str = "right"
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def is_complete(self) -> bool:
        return bool(self.email and self.password)


def load_credentials(path: Optional[str] = None) -> Credentials:
    """Load from file, then overlay any environment variables that are set."""
    p = Path(path) if path else DEFAULT_PATH
    data: dict = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    creds = Credentials(
        email=data.get("email", ""),
        password=data.get("password", ""),
        timezone=data.get("timezone", "UTC"),
        side=data.get("side", "right"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
    )
    for field_name, env_name in _ENV.items():
        env_val = os.environ.get(env_name)
        if env_val:
            setattr(creds, field_name, env_val)
    return creds


def save_credentials(creds: Credentials, path: Optional[str] = None) -> Path:
    """Write credentials to a 0600 JSON file, creating parent dirs."""
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(creds), indent=2), encoding="utf-8")
    os.chmod(p, 0o600)
    return p
