"""Test config: set env + an isolated DB BEFORE the app is imported, then a client fixture."""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Make sure `import sleepctl` resolves THIS checkout's package, not whatever `pip install -e`
# happens to point at (an editable install is a single global mapping; it isn't per-worktree,
# so a plain `sleepctl` import can otherwise silently pick up a stale/foreign copy that's
# missing modules only added here). Insert ahead of everything else on sys.path.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Must run before any `app.*` import so module-level Settings() picks these up.
_TMP = tempfile.mkdtemp()
os.environ["SLEEPCTL_DB"] = os.path.join(_TMP, "test.db")
os.environ["DASHBOARD_USER"] = "owner"
os.environ["DASHBOARD_PASSWORD"] = "secret"
os.environ["JWT_SECRET"] = "test-secret"


@pytest.fixture(scope="session")
def client():
    from app.main import app
    from app.security import ensure_bootstrap_user
    from app.seed import seed

    ensure_bootstrap_user()  # startup event doesn't run unless TestClient is a context mgr
    seed(nights=21)
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture()
def auth_client(client):
    client.post("/auth/login", json={"username": "owner", "password": "secret"})
    return client
