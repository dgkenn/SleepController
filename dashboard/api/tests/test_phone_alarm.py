"""The phone alarm: ntfy / Pushover rings the phone at the wake until "I'm awake".

The Pod's vibration is refused on this account and the web push is a single buzz, so this is the
one cue that keeps going. It must ring only for an armed wake, once a night, stop the moment the
user responds, and never leak its credentials: the ntfy topic IS the secret, and health
snapshots, events and the daemon log are all published.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

from sleepctl.config import AppConfig  # noqa: E402
from sleepctl.loop.live import SimulatedLiveClient  # noqa: E402
from sleepctl.models import ControllerState  # noqa: E402

from app import bridge, phone_alarm  # noqa: E402
from app.db import get_repo  # noqa: E402
from live_daemon import LiveDashboardDaemon  # noqa: E402

_KEYS = ("phone_alarm_config", "phone_alarm_last_run", "phone_alarm_pushover_receipt")
USER_KEY = "uQiRzpo4DXghDmr9QzzfQu27cmVRsG"
APP_TOKEN = "azGDORePK8gMaC0QOYAMyEEuzJnyUi"
RECEIPT = "rLqVuqTRh62UzxtmqiaLznmVn0wFrh"


class _Http:
    """Records every POST instead of sending it."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, data, headers, timeout=phone_alarm.HTTP_TIMEOUT_S):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if url.endswith("/messages.json"):
            return 200, json.dumps({"status": 1, "request": "x", "receipt": RECEIPT})
        return 200, "{}"

    def ntfy(self):
        return [c for c in self.calls if "pushover" not in c["url"]]

    def form(self, i):
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(self.calls[i]["data"].decode()).items()}


@pytest.fixture()
def http(monkeypatch):
    h = _Http()
    monkeypatch.setattr(phone_alarm, "_post", h)
    monkeypatch.setattr(phone_alarm, "click_url", lambda cfg: "http://192.168.1.20:3000/tonight")
    return h


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    r = Repository(str(tmp_path / "pa.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


def _clear(repo):
    repo.conn.execute(f"DELETE FROM settings_kv WHERE key IN ({','.join('?' * len(_KEYS))})",
                      _KEYS)
    repo.conn.commit()


@pytest.fixture()
def shared():
    """The suite's shared DB (the daemon's, and the API's). Always left without a phone alarm,
    so no other test can ever ring one."""
    repo = get_repo()
    _clear(repo)
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    try:
        yield repo
    finally:
        _clear(repo)
        repo.close()


def _ntfy(repo) -> str:
    phone_alarm.config_update(repo, {"backend": "ntfy", "enabled": True, "generate_topic": True})
    return phone_alarm.get_config(repo)["ntfy"]["topic"]


def _pushover(repo) -> None:
    phone_alarm.config_update(repo, {"backend": "pushover", "enabled": True,
                                     "pushover": {"user_key": USER_KEY, "app_token": APP_TOKEN}})


def _daemon(repo, verbose=False):
    d = LiveDashboardDaemon(AppConfig.default(), SimulatedLiveClient(scenario="normal", seed=7),
                            repo, verbose=verbose)
    d.PHONE_ALARM_REPEAT_S = 0.02
    return d


def _arm(d, wake: datetime) -> None:
    d.wake = {"wake_time": wake.strftime("%H:%M")}
    d.context.required_wake_time = wake


def _cmd(d, repo, t):
    bridge.enqueue_command(repo.conn, t, {})
    asyncio.new_event_loop().run_until_complete(d._apply_commands())


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------ configuration
def test_the_topic_is_generated_long_and_random(repo):
    a, b = phone_alarm.generate_topic(), phone_alarm.generate_topic()
    assert a != b and a.startswith("sleepctl-") and len(a) == len("sleepctl-") + 24


def test_secrets_are_masked_and_an_echoed_mask_keeps_them(repo):
    topic = _ntfy(repo)
    view = phone_alarm.config_view(repo)
    assert view["ntfy"]["topic"] == "***" and view["configured"] and view["enabled"]
    assert topic not in json.dumps(view)
    # the one setup view shows it
    assert phone_alarm.setup_view(repo)["topic"] == topic
    # echoing the masked view back keeps the topic
    phone_alarm.config_update(repo, {"ntfy": view["ntfy"], "enabled": False})
    assert phone_alarm.get_config(repo)["ntfy"]["topic"] == topic

    _pushover(repo)
    view = phone_alarm.config_view(repo)
    assert view["pushover"] == {"user_key": "***", "app_token": "***"}
    phone_alarm.config_update(repo, {"pushover": view["pushover"]})
    po = phone_alarm.get_config(repo)["pushover"]
    assert po == {"user_key": USER_KEY, "app_token": APP_TOKEN}
    assert USER_KEY not in json.dumps(phone_alarm.setup_view(repo))
    assert phone_alarm.public_summary(repo) == {"configured": True, "enabled": True,
                                                "backend": "pushover"}


def test_an_unknown_backend_is_refused(repo):
    with pytest.raises(ValueError):
        phone_alarm.config_update(repo, {"backend": "sms"})


def test_the_endpoints_mask_the_topic_except_in_setup(shared, auth_client, http):
    r = auth_client.put("/wake/phone-alarm/config",
                        json={"backend": "ntfy", "generate_topic": True, "enabled": True})
    assert r.status_code == 200 and r.json()["ntfy"]["topic"] == "***"
    topic = phone_alarm.get_config(shared)["ntfy"]["topic"]
    assert topic not in r.text
    assert topic not in auth_client.get("/wake/phone-alarm/config").text
    assert topic not in auth_client.get("/settings").text          # the raw settings_kv dump
    assert auth_client.get("/wake/phone-alarm/setup").json()["topic"] == topic
    assert auth_client.put("/wake/phone-alarm/config", json={"backend": "x"}).status_code == 400


def test_the_endpoints_need_a_login(client):
    client.cookies.clear()
    assert client.get("/wake/phone-alarm/setup").status_code == 401
    assert client.post("/wake/phone-alarm/test").status_code == 401


def test_the_test_alarm_is_one_urgent_ntfy_message(shared, auth_client, http):
    topic = _ntfy(shared)
    r = auth_client.post("/wake/phone-alarm/test")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(http.calls) == 1
    c = http.calls[0]
    assert c["url"] == f"https://ntfy.sh/{topic}"
    assert c["headers"]["Priority"] == "5" and c["headers"]["Tags"] == "alarm_clock"
    assert c["headers"]["Click"] == "http://192.168.1.20:3000/tonight"
    assert topic not in r.text


def test_the_pushover_test_alarm_is_not_an_emergency(shared, auth_client, http):
    _pushover(shared)
    r = auth_client.post("/wake/phone-alarm/test")
    assert r.json()["ok"] is True
    form = http.form(0)
    assert form["priority"] == "1" and "retry" not in form and "expire" not in form
    assert form["user"] == USER_KEY and form["token"] == APP_TOKEN


def test_a_test_without_setup_says_what_is_missing(repo, http):
    out = phone_alarm.send_test_alarm(repo)
    assert out["ok"] is False and "topic" in out["error"] and not http.calls


def test_a_failed_send_never_reports_the_topic(repo, monkeypatch):
    topic = _ntfy(repo)

    def boom(url, data, headers, timeout=None):
        raise OSError(f"could not reach {url}")
    monkeypatch.setattr(phone_alarm, "_post", boom)
    out = phone_alarm.send_test_alarm(repo)
    assert out["ok"] is False and topic not in out["error"] and "***" in out["error"]


# ------------------------------------------------------------------ the run itself
def test_ntfy_repeats_up_to_the_cap(repo, http):
    _ntfy(repo)
    run = phone_alarm.PhoneAlarmRun(phone_alarm.get_config(repo), title="t", message="m",
                                    interval_s=0.001, max_sends=4).start()
    run.join(5)
    assert run.sends == 4 and len(http.calls) == 4 and run.finished and not run.active
    assert all(c["headers"]["Priority"] == "5" for c in http.calls)


def test_pushover_is_one_emergency_message(repo, http):
    _pushover(repo)
    run = phone_alarm.PhoneAlarmRun(phone_alarm.get_config(repo), title="t", message="m").start()
    run.join(5)
    form = http.form(0)
    assert form["priority"] == "2" and form["retry"] == "60" and form["expire"] == "1800"
    assert form["sound"] and len(http.calls) == 1
    assert run.receipt == RECEIPT and run.active       # Pushover keeps repeating server-side
    assert RECEIPT not in json.dumps(run.status())


# ------------------------------------------------------------------ the daemon
def test_ntfy_rings_repeatedly_and_stops_on_woke_up(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    assert d._maybe_phone_alarm(now) is True
    assert _wait(lambda: len(http.ntfy()) >= 3)
    _cmd(d, shared, "woke_up")
    d._phone_alarm.join(2)
    sent = len(http.ntfy())
    time.sleep(0.1)
    assert len(http.ntfy()) == sent < phone_alarm.NTFY_MAX_SENDS
    assert d._phone_alarm_status()["ringing"] is False
    assert d._phone_alarm.stop_reason == "woke_up"
    codes = [r["code"] for r in shared.conn.execute(
        "SELECT code FROM events WHERE code LIKE 'phone_alarm_%' ORDER BY id").fetchall()]
    assert codes[-2:] == ["phone_alarm_started", "phone_alarm_stopped"]


def test_the_pushover_receipt_is_cancelled_on_woke_up(shared, http):
    _pushover(shared)
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    assert d._maybe_phone_alarm(now) is True
    d._phone_alarm.join(2)
    _cmd(d, shared, "woke_up")
    d._phone_alarm.join(2)
    cancels = [c for c in http.calls if "/receipts/" in c["url"]]
    assert len(cancels) == 1 and cancels[0]["url"].endswith(f"/receipts/{RECEIPT}/cancel.json")
    assert d._phone_alarm.cancelled is True


def test_a_pushover_receipt_survives_a_restart(shared, http):
    _pushover(shared)
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    d._maybe_phone_alarm(now)
    d._phone_alarm.join(2)
    d._tend_phone_alarm()                                  # stores the receipt
    d2 = _daemon(shared)                                   # restart
    assert d2._phone_alarm is not None and d2._phone_alarm.active
    _cmd(d2, shared, "woke_up")
    d2._phone_alarm.join(2)
    assert any(c["url"].endswith(f"/receipts/{RECEIPT}/cancel.json") for c in http.calls)


def test_bed_exit_stops_it_but_a_nap_ending_at_its_deadline_does_not(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    d._maybe_phone_alarm(now)
    # the nap-deadline path: the session ends under the alarm, which must keep ringing
    d._phone_alarm_hold_idle = d._phone_alarm
    d._prev_state = ControllerState.WAKE_WINDOW
    d._saw_sleep = False
    asyncio.new_event_loop().run_until_complete(
        d._maybe_close_out(SimpleNamespace(state=ControllerState.IDLE), now))
    assert d._phone_alarm.active
    # a real bed exit stops it
    d._prev_state = ControllerState.WAKE_WINDOW
    asyncio.new_event_loop().run_until_complete(
        d._maybe_close_out(SimpleNamespace(state=ControllerState.IDLE), now))
    assert not d._phone_alarm.active and d._phone_alarm.stop_reason == "bed exit"


def test_clearing_the_alarm_stops_it(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    d._maybe_phone_alarm(now)
    _cmd(d, shared, "clear_wake")
    assert d._phone_alarm.stop_reason == "alarm cleared" and not d._phone_alarm.active


def test_nothing_rings_without_an_armed_alarm(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    d.wake = None
    d.context.required_wake_time = None
    assert d._maybe_phone_alarm(datetime.now()) is False
    assert d._start_phone_alarm("smart_wake") is False
    time.sleep(0.05)
    assert http.calls == []


def test_it_rings_only_at_the_deadline_and_only_when_enabled(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    wake = datetime.now() + timedelta(minutes=30)
    _arm(d, wake)
    assert d._maybe_phone_alarm(wake - timedelta(minutes=1)) is False       # not yet
    assert d._maybe_phone_alarm(wake + timedelta(hours=1)) is False         # long past
    phone_alarm.config_update(shared, {"enabled": False})
    assert d._maybe_phone_alarm(wake + timedelta(seconds=30)) is False      # switched off
    assert http.calls == []


def test_at_most_one_run_per_night(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    now = datetime.now()
    wake = now - timedelta(minutes=1)
    _arm(d, wake)
    assert d._start_phone_alarm("smart_wake") is True
    d._stop_phone_alarm("bed exit")
    # the smart wake re-asserting, the deadline backstop, a restart, a re-armed alarm: silent
    assert d._start_phone_alarm("smart_wake") is False
    assert d._maybe_phone_alarm(now) is False
    d2 = _daemon(shared)
    _arm(d2, wake + timedelta(minutes=5))
    assert d2._maybe_phone_alarm(now + timedelta(minutes=6)) is False
    # the next night rings again
    _arm(d2, wake + timedelta(days=1))
    assert d2._maybe_phone_alarm(now + timedelta(days=1)) is True
    d2._stop_phone_alarm("test over")


def test_a_nap_with_a_deadline_rings_too(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    d._start_nap(duration_min=20)
    assert d.nap_deadline is not None and d.context.required_wake_time == d.nap_deadline
    assert d._maybe_phone_alarm(d.nap_deadline + timedelta(seconds=10)) is True
    assert d._phone_alarm_run_key(d.nap_deadline)[0] == "nap"
    d._stop_phone_alarm("test over")
    # the nap's run does not use up the night's
    last = json.loads(shared.conn.execute(
        "SELECT value FROM settings_kv WHERE key='phone_alarm_last_run'").fetchone()["value"])
    assert "nap" in last and "night" not in last


def test_the_smart_wake_decision_rings_it(shared, http):
    _ntfy(shared)
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now + timedelta(minutes=12))
    decision = SimpleNamespace(log_payload={"wake_action": {
        "should_wake": True, "phase": "wake", "target_time": (now + timedelta(minutes=12)).isoformat()}})
    d._capture_wake(decision, SimpleNamespace(stage=SimpleNamespace(value="light")), now)
    assert d._phone_alarm is not None and d._phone_alarm.active
    assert _wait(lambda: len(http.ntfy()) >= 1)
    assert b"12 min early" in http.ntfy()[0]["data"]
    d._stop_phone_alarm("test over")


def test_a_failing_backend_never_raises_into_the_loop(shared, monkeypatch):
    _ntfy(shared)
    monkeypatch.setattr(phone_alarm, "PhoneAlarmRun",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = _daemon(shared)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    assert d._maybe_phone_alarm(now) is False


def test_no_secret_reaches_events_logs_runtime_state_or_the_health_snapshot(shared, http, capsys):
    topic = _ntfy(shared)
    phone_alarm.config_update(shared, {"pushover": {"user_key": USER_KEY, "app_token": APP_TOKEN}})
    d = _daemon(shared, verbose=True)
    now = datetime.now()
    _arm(d, now - timedelta(minutes=1))
    d._maybe_phone_alarm(now)
    assert _wait(lambda: len(http.ntfy()) >= 1)
    d._tend_phone_alarm()
    _cmd(d, shared, "woke_up")
    bridge.write_runtime_state(shared.conn, d._snapshot(None, None))

    from app import health_snapshot
    snap = health_snapshot.build_health_snapshot(shared)
    assert snap["phone_alarm"] == {"configured": True, "enabled": True, "backend": "ntfy"}
    events = json.dumps([dict(r) for r in shared.conn.execute("SELECT * FROM events").fetchall()])
    runtime = json.dumps(bridge.read_runtime_state(shared.conn, 10**6))
    logs = capsys.readouterr().out
    assert "phone_alarm_started" in events and "phone alarm ringing" in logs
    for secret in (topic, USER_KEY, APP_TOKEN, RECEIPT):
        for where, text in (("snapshot", json.dumps(snap)), ("events", events),
                            ("runtime", runtime), ("log", logs)):
            assert secret not in text, f"secret leaked into {where}"


def test_the_snapshot_scrub_catches_a_stray_topic():
    from app import health_snapshot
    topic = phone_alarm.generate_topic()
    out = health_snapshot.scrub({"note": f"posted to https://ntfy.sh/{topic}",
                                 "ntfy_topic": "anything", "receipt": "r"})
    assert topic not in json.dumps(out)
    assert out["ntfy_topic"] == "[redacted]" and out["receipt"] == "[redacted]"
