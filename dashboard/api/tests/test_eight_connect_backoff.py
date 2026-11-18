"""A refused Eight Sleep connection is waited out in-process, not crashed into a restart loop."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "daemon"))
import run_daemon  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_retries_with_a_doubling_gap_then_returns_the_client():
    calls, slept, waits = [], [], []

    async def build():
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError("GET /users/me -> HTTP 403")
        return "client"

    async def fake_sleep(s):
        slept.append(s)

    out = _run(run_daemon.connect_eight_with_backoff(
        build, lambda e, left, n: waits.append((left, n)), first_s=60, max_s=1200, beat_s=30,
        sleep=fake_sleep))
    assert out == "client" and len(calls) == 4
    assert sum(slept) == 60 + 120 + 240          # 1, 2, 4 minutes between the attempts
    assert max(slept) <= 30                     # runtime_state is refreshed every 30 s meanwhile
    assert {n for _, n in waits} == {1, 2, 3}


def test_the_gap_is_capped():
    slept = []

    async def build():
        raise RuntimeError("403")

    async def fake_sleep(s):
        slept.append(s)

    with pytest.raises(RuntimeError):
        _run(run_daemon.connect_eight_with_backoff(build, first_s=600, max_s=1200, beat_s=1200,
                                                  max_attempts=4, sleep=fake_sleep))
    assert slept == [600, 1200, 1200]


def test_the_wait_state_is_written_to_runtime_state():
    from app import bridge
    from app.db import get_repo
    repo = get_repo()
    run_daemon._wait_state_writer(repo)(RuntimeError("HTTP 403"), 90, 2)
    rt = bridge.read_runtime_state(repo.conn, 10**9)
    assert rt["state"] == "waiting_for_eight_sleep"
