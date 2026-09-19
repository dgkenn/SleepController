"""The transport alternation must be driven by connected-but-silent sessions only, and a forced
HR-only session must give PMD another chance.

2026-09-18: six "device not found" attempts while the band was still off the arm counted as
barren sessions, the seventh connected in "hr" mode, and the accelerometer/PPI were never asked
for again that night.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verity_forwarder as vf  # noqa: E402


class _Stop(BaseException):
    """Escapes the forwarder's `except Exception` so a test can end the forever loop."""


def _args():
    return types.SimpleNamespace(mode="auto", address=None, acc_rate=52, retry_seconds=0.0,
                                 batch_seconds=0.001, url="http://x", source="verity",
                                 stall_seconds=100.0)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(vf, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vf, "_beat", lambda *a, **k: None)
    monkeypatch.setattr(vf, "_load_acc_rung", lambda root: 0)
    monkeypatch.setattr(vf, "_post_link", lambda *a, **k: None)
    monkeypatch.setitem(vf._STATS, "posts", 0)
    monkeypatch.setitem(vf._STATS, "acc_rung", 0)
    monkeypatch.setitem(vf._STATS, "session_opened", False)
    logs = []
    monkeypatch.setattr(vf, "_log", lambda m, *a, **k: logs.append(str(m)))
    yield logs
    vf._STATS.pop("pmd_stall", None)
    vf._STATS.pop("pmd_streamed_s", None)


def _drive(monkeypatch, sessions):
    """`sessions`: list of callables run in place of _run_once; each may set _STATS and raise."""
    modes = []
    it = iter(sessions)

    async def fake_run_once(args, env):
        modes.append(args.mode)
        try:
            fn = next(it)
        except StopIteration:
            raise _Stop()
        fn(args)

    monkeypatch.setattr(vf, "_run_once", fake_run_once)
    with pytest.raises(_Stop):
        asyncio.run(vf._main_async(_args(), {}))
    return modes


def _not_found(args):
    raise RuntimeError("Device with address X was not found.")


def _opened_silent(args):
    vf._STATS["session_opened"] = True


def _opened_streamed_hr(args):
    vf._STATS["session_opened"] = True
    vf._STATS["posts"] += 10


def test_failed_connects_do_not_flip_the_transport(monkeypatch):
    modes = _drive(monkeypatch, [_not_found] * 6 + [_opened_streamed_hr])
    assert modes[:7] == ["auto"] * 7, modes


def test_connected_but_silent_sessions_still_alternate(monkeypatch):
    modes = _drive(monkeypatch, [_opened_silent, _opened_silent, _opened_streamed_hr, _opened_silent])
    assert modes[:3] == ["auto", "auto", "hr"]
    # the productive HR session reset the streak: PMD is tried again next
    assert modes[3] == "auto"


def test_a_forced_hr_session_is_time_limited_so_pmd_gets_retried(monkeypatch, _quiet):
    limits = []

    def remember_limit(args):
        limits.append(getattr(args, "hr_session_max_s", None))
        _opened_streamed_hr(args)

    _drive(monkeypatch, [_opened_silent, _opened_silent, remember_limit])
    assert limits == [vf._FORCED_HR_RETRY_PMD_S]
    assert any("retry PMD" in m for m in _quiet)


def test_pmd_retries_are_capped_when_pmd_never_delivers(monkeypatch):
    limits = []

    def remember_limit(args):
        limits.append(getattr(args, "hr_session_max_s", None))
        _opened_streamed_hr(args)

    # auto(silent) auto(silent) -> hr(limited) -> auto(silent) auto(silent) -> hr(limited)
    # -> auto(silent) auto(silent) -> hr: budget of 2 PMD retries spent, no limit this time
    seq = ([_opened_silent, _opened_silent, remember_limit] * 3)
    _drive(monkeypatch, seq)
    assert limits[0] == vf._FORCED_HR_RETRY_PMD_S
    assert limits[-1] is None


def test_the_hr_session_ends_itself_after_the_limit(monkeypatch, _quiet):
    """The generic-HR flusher returns once the limit passes and the session has posted."""
    from test_verity_pmd_stale_stream import FakeClient

    class Live(FakeClient):
        is_connected = True

    monkeypatch.setattr(vf, "_post", lambda url, payload, timeout=5.0: vf._STATS.__setitem__(
        "posts", vf._STATS["posts"] + 1) or {"ok": True})
    args = _args()
    args.hr_session_max_s = 0.02
    args.hr_max_age = 100.0
    client = Live({})

    async def run():
        # feed one HR notification so the flusher has something to post
        orig = client.start_notify

        async def start_notify(uuid, cb):
            await orig(uuid, cb)
            cb(None, bytearray([0x00, 70]))
        client.start_notify = start_notify
        await asyncio.wait_for(vf._hr_session(client, args), timeout=5.0)

    asyncio.run(run())
    assert any("dropping the link" in m for m in _quiet)
