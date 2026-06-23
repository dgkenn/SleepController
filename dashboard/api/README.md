# sleepctl dashboard API (FastAPI)

Orchestrator over the `sleepctl` engine. Reuses the same SQLite dataset and drives the same
controller. It **never calls the device directly** — it enqueues commands the control daemon
applies, and reads the daemon's `runtime_state` snapshot for status (see `app/bridge.py`).

## Run locally

```bash
# from the repo root
pip install -e .                                  # the sleepctl engine
pip install -r dashboard/api/requirements.txt     # fastapi, uvicorn, PyYAML

export SLEEPCTL_DB=$PWD/sleepctl.db
export DASHBOARD_USER=owner DASHBOARD_PASSWORD=secret   # bootstrap login
cd dashboard/api
PYTHONPATH=$PWD/../..:$PWD python3 -m app.seed          # demo data (optional)
PYTHONPATH=$PWD/../..:$PWD uvicorn app.main:app --host 0.0.0.0 --port 8000

# in another shell: start the control daemon (simulator by default — no Pod needed)
PYTHONPATH=$PWD/../..:$PWD python3 ../daemon/run_daemon.py
```

## Tests

```bash
cd dashboard/api
PYTHONPATH=$PWD/../..:$PWD python3 -m pytest -q tests/
```

## Auth

Stdlib-only: HS256 JWT + PBKDF2 password hashing (no `jose`/`passlib`/`cryptography`). The
single owner user is bootstrapped from `DASHBOARD_USER`/`DASHBOARD_PASSWORD` on first run; the
`JWT_SECRET` is auto-generated if not supplied. Login sets an httpOnly `session` cookie.

## Key modules

- `app/main.py` — all routes + SSE (`/stream/status`).
- `app/bridge.py` — the API↔daemon contract (commands queue + runtime_state).
- `app/services.py` — status assembly, analytics, ML surfacing, alerts, data-source health.
- `app/security.py` — stdlib JWT + password hashing.
- `app/db.py` — shared SQLite (engine tables + dashboard tables).
