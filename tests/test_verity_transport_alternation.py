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
    monkeypatch.setattr(vf, "_load_acc_rate", lambda root: "")
    monkeypatch.setattr(vf, "_post_link", lambda *a, **k: None)
    monkeypatch.setitem(vf._STATS, "posts", 0)
    monkeypatch.setitem(vf._STATS, "data_posts", 0)
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
        return fn(args)     # a session may return an outcome (backoff / released)

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
    vf._STATS["data_posts"] += 10


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
        "data_posts", vf._STATS["data_posts"] + 1) or {"ok": True})
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


def test_the_pmd_retry_budget_is_spent_then_refilled_by_a_productive_stall(monkeypatch):
    monkeypatch.setitem(vf._STATS, "pmd_retries", 0)
    assert vf._grant_pmd_retry(None) == vf._FORCED_HR_RETRY_PMD_S
    assert vf._grant_pmd_retry(None) == vf._FORCED_HR_RETRY_PMD_S
    assert vf._grant_pmd_retry(None) is None                       # budget spent
    # a stall after 23 minutes of PMD frames (2026-09-18 23:43) refills it
    assert vf._grant_pmd_retry({"streamed_s": 23 * 60.0}) == vf._FORCED_HR_RETRY_PMD_S
    # a stall after 30 s of frames does not
    monkeypatch.setitem(vf._STATS, "pmd_retries", vf._MAX_PMD_RETRIES)
    assert vf._grant_pmd_retry({"streamed_s": 30.0}) is None


# ---------------------------------------------------------------- receiver roles
def test_the_streams_url_sits_next_to_the_ingest_url():
    assert vf._streams_url("http://box:8000/hr/ingest?token=abc") == "http://box:8000/hr/streams?token=abc"


def test_a_receiver_defers_to_another_that_is_delivering_pmd():
    st = {"pmd_holder": "verity", "acc": {"age_s": 4.0, "source": "verity"},
          "ppi": {"age_s": 6.0, "source": "verity"}}
    assert vf._pmd_held_elsewhere(st, "verity-pi") == "verity"
    assert vf._pmd_held_elsewhere(st, "verity") is None          # that is me


def test_a_stale_pmd_holder_does_not_keep_the_standby_down():
    st = {"pmd_holder": "verity", "acc": {"age_s": 400.0, "source": "verity"},
          "ppi": {"age_s": 400.0, "source": "verity"}}
    assert vf._pmd_held_elsewhere(st, "verity-pi") is None
    assert vf._pmd_held_elsewhere(None, "verity-pi") is None      # API unreachable: act alone
    assert vf._pmd_held_elsewhere({"pmd_holder": None}, "verity-pi") is None


def test_the_adapter_reset_comes_sooner_after_a_mid_night_drop(monkeypatch):
    """2026-09-18 23:08: link dropped after 2 h of streaming; six 'not found' attempts and
    11 minutes before the adapter reset that fixed it."""
    import time as _t
    requests = []
    monkeypatch.setattr(vf, "_request_adapter_reset", lambda root, n: requests.append(n))
    monkeypatch.setitem(vf._STATS, "last_data_at", _t.monotonic())      # streaming a moment ago
    _drive(monkeypatch, [_not_found] * 3)
    assert requests and min(requests) == vf._ADAPTER_RESET_AFTER_RECENT


def test_the_adapter_reset_waits_when_the_band_has_been_away(monkeypatch):
    requests = []
    monkeypatch.setattr(vf, "_request_adapter_reset", lambda root, n: requests.append(n))
    monkeypatch.setitem(vf._STATS, "last_data_at", 0.0)
    _drive(monkeypatch, [_not_found] * 3)
    assert not requests


# ---------------------------------------------------------------- audit 2026-09-25
def test_a_charging_band_never_escalates_to_an_adapter_reset(monkeypatch, _quiet):
    """Regression: while the band is released ("device in charger", code 13) every backoff
    minute counted as a barren session, a Bluetooth adapter reset was requested from streak 5
    every minute, and the watchdog's bthserv restart killed the forwarder and its backoff."""
    import time as _t
    requests = []
    monkeypatch.setattr(vf, "_request_adapter_reset", lambda root, n: requests.append(n))
    monkeypatch.setitem(vf._STATS, "last_data_at", _t.monotonic())   # worst case: rung 2
    monkeypatch.setitem(vf._RELEASE, "until", _t.monotonic() + 900.0)

    def backoff(args):
        return vf._OUTCOME_BACKOFF

    def charging(args):
        vf._STATS["session_opened"] = True
        return vf._OUTCOME_RELEASED

    modes = _drive(monkeypatch, [backoff] * 8 + [charging] * 8)
    assert not requests, "an adapter reset was requested for a band left alone to charge"
    assert not any("barren" in m for m in _quiet), _quiet
    assert set(modes) == {"auto"}, "a released band drove the transport alternation"


def test_the_real_run_once_reports_a_backoff_instead_of_a_barren_session(monkeypatch):
    import time as _t
    monkeypatch.setitem(vf._RELEASE, "until", _t.monotonic() + 900.0)

    async def nosleep(_s):
        return None
    monkeypatch.setattr(vf.asyncio, "sleep", nosleep)
    assert asyncio.run(vf._run_once(_args(), {})) == vf._OUTCOME_BACKOFF


def test_the_release_backoff_survives_a_restart(monkeypatch, tmp_path):
    """Regression: the backoff lived in process memory, so a bthserv restart (or a deploy) that
    relaunched the forwarder reconnected at once to the band it had just released."""
    import time as _t
    monkeypatch.setitem(vf._RELEASE, "until", 0.0)
    vf._release("charging")
    vf._RELEASE.pop("off_arm", None)
    assert (tmp_path / ".run" / vf._RELEASE_UNTIL_FILE).exists()
    vf._RELEASE["until"] = 0.0                       # a fresh process: memory is gone
    remaining = vf._load_release(tmp_path)
    assert 0.9 * vf._NOT_WORN_BACKOFF_S < remaining <= vf._NOT_WORN_BACKOFF_S
    assert vf._releasing()
    # a deadline already in the past is ignored
    (tmp_path / ".run" / vf._RELEASE_UNTIL_FILE).write_text(f"{_t.time() - 5:.3f}")
    vf._RELEASE["until"] = 0.0
    assert vf._load_release(tmp_path) == 0.0 and not vf._releasing()
    # ...and so is garbage
    (tmp_path / ".run" / vf._RELEASE_UNTIL_FILE).write_text("not a time")
    assert vf._load_release(tmp_path) == 0.0


def test_main_resumes_a_persisted_backoff_without_counting_it(monkeypatch, tmp_path, _quiet):
    import time as _t
    monkeypatch.setitem(vf._RELEASE, "until", 0.0)
    (tmp_path / ".run").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".run" / vf._RELEASE_UNTIL_FILE).write_text(f"{_t.time() + 600:.3f}")
    seen = []

    def backoff(args):
        seen.append(vf._releasing())
        return vf._OUTCOME_BACKOFF
    _drive(monkeypatch, [backoff])
    assert seen == [True]
    assert any("resuming the release backoff" in m for m in _quiet)


def test_status_only_posts_do_not_count_as_data(monkeypatch):
    """Regression: the battery and link posts are accepted by the API, and each one counted as
    a productive POST -- so every connect reset the barren ladder and logged a false
    "recovered"."""
    import io
    import json

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(vf.urllib.request, "urlopen",
                        lambda req, timeout=5: _Resp(json.dumps({"ok": True}).encode()))
    monkeypatch.setitem(vf._STATS, "last_data_at", 0.0)
    vf._post("http://x", {"source": "verity", "battery_pct": 100})
    vf._post("http://x", {"source": "verity", "link": "charging"})
    assert vf._STATS["posts"] == 2                 # accepted...
    assert vf._STATS["data_posts"] == 0            # ...but not data
    assert vf._STATS["last_data_at"] == 0.0
    vf._post("http://x", {"source": "verity", "hr": 58.0})
    vf._post("http://x", {"source": "verity", "acc": {"pim": 1.0}})
    assert vf._STATS["data_posts"] == 2 and vf._STATS["last_data_at"] > 0.0


def test_a_session_that_only_posted_its_battery_is_still_barren(monkeypatch, _quiet):
    def battery_only(args):
        vf._STATS["session_opened"] = True
        vf._STATS["posts"] += 1            # the connect-time battery post, accepted

    modes = _drive(monkeypatch, [battery_only, battery_only, battery_only])
    assert not any("recovered" in m for m in _quiet), _quiet
    assert any("barren streak: 2" in m for m in _quiet), _quiet
    assert modes[:3] == ["auto", "auto", "hr"]    # the ladder escalated as it should


def test_scans_that_never_find_the_band_do_not_spend_the_pmd_retry_budget(monkeypatch):
    """Regression: in HR-led mode the retry was granted before every scan, so scans that never
    found the band spent the budget and the session that finally connected stayed HR-only all
    night."""
    limits = []

    def remember_limit(args):
        limits.append(getattr(args, "hr_session_max_s", None))
        _opened_streamed_hr(args)

    def away(args):
        return None                          # scanned, nothing opened

    _drive(monkeypatch, [_opened_silent, _opened_silent, _not_found, _not_found, away, away,
                         remember_limit])
    assert limits == [vf._FORCED_HR_RETRY_PMD_S]
