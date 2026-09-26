"""The phone alarm: a notification that keeps going until you answer it.

The Pod's vibration alarm is refused on this account (subscription-gated; the daemon latches
``_alarm_write_denied``), and the single web push at the wake moment is a notification, not an
alarm -- one buzz, easily slept through. This rings the phone properly, on one of two backends:

* **ntfy** (default; free, no account). An urgent (priority 5) message to a topic on ntfy.sh or
  a self-hosted server, re-sent every minute up to 15 times. The topic is a long random string
  the box generates, and it is the whole credential: anyone who knows it can read and post to
  it. It is never logged, never published, and handed back only to the one setup view.
* **Pushover** (optional). One emergency-priority message that Pushover itself repeats until it
  is acknowledged or expires. The receipt is kept so "I'm awake" can cancel it.

Configuration lives in settings_kv (``phone_alarm_config``) so it can be set up from the phone,
like the wake plug. Every HTTP call here is small, time-limited and returns a result rather than
raising; the daemon also runs them off its event loop (see ``PhoneAlarmRun``).
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

CONFIG_KEY = "phone_alarm_config"
MASK = "***"
NTFY_DEFAULT_SERVER = "https://ntfy.sh"
PUSHOVER_API = "https://api.pushover.net/1"
HTTP_TIMEOUT_S = 8.0

#: ntfy has no server-side repeat, so the box re-sends: every minute, 15 times (a quarter hour).
NTFY_REPEAT_S = 60.0
NTFY_MAX_SENDS = 15
#: Pushover emergency priority: re-alerts every 60 s for up to 30 min, until acknowledged.
PUSHOVER_RETRY_S = 60
PUSHOVER_EXPIRE_S = 1800
PUSHOVER_SOUND = "siren"   # a built-in Pushover sound; loud and long enough to wake to

BACKENDS = ("ntfy", "pushover")


# ------------------------------------------------------------------ configuration
def generate_topic() -> str:
    """A fresh, unguessable ntfy topic (24 url-safe random characters after the prefix)."""
    return "sleepctl-" + secrets.token_urlsafe(18)


def _load(repo) -> dict:
    row = repo.conn.execute(
        "SELECT value FROM settings_kv WHERE key=?", (CONFIG_KEY,)).fetchone()
    try:
        return json.loads(row["value"]) if row else {}
    except Exception:
        return {}


def get_config(repo) -> dict:
    """The stored config, normalised. Holds the SECRETS -- never return this to a client."""
    d = _load(repo)
    ntfy = d.get("ntfy") if isinstance(d.get("ntfy"), dict) else {}
    po = d.get("pushover") if isinstance(d.get("pushover"), dict) else {}
    backend = d.get("backend") if d.get("backend") in BACKENDS else "ntfy"
    return {
        "enabled": bool(d.get("enabled", False)),
        "backend": backend,
        "ntfy": {"server": (ntfy.get("server") or NTFY_DEFAULT_SERVER).rstrip("/"),
                 "topic": ntfy.get("topic") or ""},
        "pushover": {"user_key": po.get("user_key") or "", "app_token": po.get("app_token") or ""},
        "click_url": d.get("click_url") or "",
    }


def is_configured(cfg: dict) -> bool:
    """Whether the chosen backend has what it needs to ring."""
    if cfg.get("backend") == "pushover":
        po = cfg.get("pushover") or {}
        return bool(po.get("user_key") and po.get("app_token"))
    return bool((cfg.get("ntfy") or {}).get("topic"))


def is_ready(cfg: dict) -> bool:
    return bool(cfg.get("enabled")) and is_configured(cfg)


def _masked(v: str) -> str:
    return MASK if v else ""


def config_view(repo) -> dict:
    """What the dashboard sees: every secret masked."""
    c = get_config(repo)
    return {
        "enabled": c["enabled"], "backend": c["backend"], "configured": is_configured(c),
        "ntfy": {"server": c["ntfy"]["server"], "topic": _masked(c["ntfy"]["topic"])},
        "pushover": {"user_key": _masked(c["pushover"]["user_key"]),
                     "app_token": _masked(c["pushover"]["app_token"])},
        "click_url": c["click_url"],
    }


def setup_view(repo) -> dict:
    """The ONE view that shows the ntfy topic, for the signed-in user to subscribe with.
    Pushover keys are never handed back: the user already has them."""
    c = get_config(repo)
    topic = c["ntfy"]["topic"]
    server = c["ntfy"]["server"]
    return {"backend": c["backend"], "server": server, "topic": topic,
            "subscribe_url": f"{server}/{topic}" if topic else ""}


def public_summary(repo) -> dict:
    """For the public health snapshot: whether it is set up, never with what."""
    c = get_config(repo)
    return {"configured": is_configured(c), "enabled": c["enabled"], "backend": c["backend"]}


def config_update(repo, values: dict) -> dict:
    """Merge ``values`` into the stored config. A masked secret echoed back ("***") keeps the
    stored one; ``generate_topic`` replaces the ntfy topic with a fresh random one."""
    cur = get_config(repo)
    values = dict(values or {})
    if values.get("backend") is not None:
        if values["backend"] not in BACKENDS:
            raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
        cur["backend"] = values["backend"]
    if values.get("enabled") is not None:
        cur["enabled"] = bool(values["enabled"])
    if values.get("click_url") is not None:
        cur["click_url"] = str(values["click_url"]).strip()
    ntfy = values.get("ntfy")
    if isinstance(ntfy, dict):
        if ntfy.get("server"):
            server = str(ntfy["server"]).strip().rstrip("/")
            if not server.startswith(("https://", "http://")):
                raise ValueError("the ntfy server must be an http(s) URL")
            cur["ntfy"]["server"] = server
        topic = ntfy.get("topic")
        if topic not in (None, MASK):
            cur["ntfy"]["topic"] = str(topic).strip()
    if values.get("generate_topic"):
        cur["ntfy"]["topic"] = generate_topic()
    po = values.get("pushover")
    if isinstance(po, dict):
        for k in ("user_key", "app_token"):
            if po.get(k) not in (None, MASK):
                cur["pushover"][k] = str(po[k]).strip()
    repo.conn.execute(
        "INSERT INTO settings_kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CONFIG_KEY, json.dumps(cur)))
    repo.conn.commit()
    return config_view(repo)


def click_url(cfg: dict) -> str | None:
    """Where tapping the alarm goes: a configured URL, else the dashboard's LAN address as the
    watchdog last recorded it (a private address, not a secret)."""
    if cfg.get("click_url"):
        return cfg["click_url"]
    try:
        from app.health_snapshot import _lan_url
        url = _lan_url(None)
        return url.rstrip("/") + "/tonight" if url else None
    except Exception:
        return None


# ------------------------------------------------------------------ transport
def _secrets_of(cfg: dict) -> list[str]:
    return [s for s in ((cfg.get("ntfy") or {}).get("topic"),
                        (cfg.get("pushover") or {}).get("user_key"),
                        (cfg.get("pushover") or {}).get("app_token")) if s]


def _clean(text: str, cfg: dict, extra: tuple = ()) -> str:
    """An error string safe for logs and events: every secret in it replaced by the mask."""
    out = str(text)[:300]
    for s in [*_secrets_of(cfg), *[e for e in extra if e]]:
        out = out.replace(s, MASK)
    return out


def _post(url: str, data: bytes, headers: dict, timeout: float = HTTP_TIMEOUT_S) -> tuple:
    """POST and return ``(status, body_text)``. Raises on transport failure (callers catch)."""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": "sleepctl/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body


def send_ntfy(cfg: dict, title: str, message: str, *, priority: int = 5,
              click: str | None = None) -> dict:
    n = cfg.get("ntfy") or {}
    if not n.get("topic"):
        return {"ok": False, "error": "no ntfy topic"}
    headers = {"Title": title.encode("ascii", "replace").decode("ascii"),
               "Priority": str(int(priority)), "Tags": "alarm_clock"}
    if click:
        headers["Click"] = click
    try:
        status, body = _post(f"{n.get('server') or NTFY_DEFAULT_SERVER}/{n['topic']}",
                             message.encode("utf-8"), headers)
    except Exception as exc:
        return {"ok": False, "error": _clean(f"{type(exc).__name__}: {exc}", cfg)}
    if 200 <= status < 300:
        return {"ok": True, "status": status}
    return {"ok": False, "status": status, "error": _clean(f"ntfy HTTP {status}: {body}", cfg)}


def send_pushover(cfg: dict, title: str, message: str, *, priority: int = 2,
                  click: str | None = None) -> dict:
    """Priority 2 (emergency) returns a receipt and repeats server-side until acknowledged;
    priority 1 is a single high-priority alert that bypasses quiet hours (used for the test)."""
    po = cfg.get("pushover") or {}
    if not (po.get("user_key") and po.get("app_token")):
        return {"ok": False, "error": "Pushover user key / app token not set"}
    form = {"token": po["app_token"], "user": po["user_key"], "title": title,
            "message": message, "priority": str(int(priority)), "sound": PUSHOVER_SOUND}
    if priority >= 2:
        form.update(retry=str(PUSHOVER_RETRY_S), expire=str(PUSHOVER_EXPIRE_S))
    if click:
        form.update(url=click, url_title="Open SleepCtl")
    try:
        status, body = _post(f"{PUSHOVER_API}/messages.json",
                             urllib.parse.urlencode(form).encode("utf-8"),
                             {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:
        return {"ok": False, "error": _clean(f"{type(exc).__name__}: {exc}", cfg)}
    try:
        data = json.loads(body or "{}")
    except Exception:
        data = {}
    if 200 <= status < 300 and data.get("status") == 1:
        return {"ok": True, "status": status, "receipt": data.get("receipt")}
    errs = "; ".join(str(e) for e in (data.get("errors") or [])) or body
    return {"ok": False, "status": status, "error": _clean(f"Pushover HTTP {status}: {errs}", cfg)}


def cancel_pushover(cfg: dict, receipt: str) -> dict:
    po = cfg.get("pushover") or {}
    if not (receipt and po.get("app_token")):
        return {"ok": False, "error": "nothing to cancel"}
    try:
        status, body = _post(f"{PUSHOVER_API}/receipts/{urllib.parse.quote(receipt)}/cancel.json",
                             urllib.parse.urlencode({"token": po["app_token"]}).encode("utf-8"),
                             {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:
        return {"ok": False, "error": _clean(f"{type(exc).__name__}: {exc}", cfg, (receipt,))}
    if 200 <= status < 300:
        return {"ok": True, "status": status}
    return {"ok": False, "status": status,
            "error": _clean(f"Pushover cancel HTTP {status}: {body}", cfg, (receipt,))}


def send_test_alarm(repo) -> dict:
    """One short alarm right now, so setup can be checked without waiting for a morning. Never
    loops: ntfy gets a single urgent message, Pushover a priority-1 (not emergency) one."""
    c = get_config(repo)
    if not is_configured(c):
        return {"ok": False, "backend": c["backend"],
                "error": ("generate a topic first" if c["backend"] == "ntfy"
                          else "enter your Pushover user key and app token first")}
    title, msg = "SleepCtl test alarm", "This is how your wake alarm will look and sound."
    if c["backend"] == "pushover":
        res = send_pushover(c, title, msg, priority=1, click=click_url(c))
    else:
        res = send_ntfy(c, title, msg, priority=5, click=click_url(c))
    return {"ok": bool(res.get("ok")), "backend": c["backend"], "error": res.get("error")}


# ------------------------------------------------------------------ one alarm run
class PhoneAlarmRun:
    """One morning's alarm, run on a background thread so no network call can hold the control
    loop. ntfy re-sends every ``interval_s`` up to ``max_sends`` times; Pushover sends one
    emergency message and remembers the receipt so ``stop`` can cancel it.

    Holds the config (with its secrets) in memory only. ``status()`` is secret-free and safe to
    publish; errors are scrubbed of every secret before they are stored.
    """

    def __init__(self, cfg: dict, *, title: str, message: str, click: str | None = None,
                 interval_s: float = NTFY_REPEAT_S, max_sends: int = NTFY_MAX_SENDS) -> None:
        self.cfg = cfg
        self.backend = cfg.get("backend") or "ntfy"
        self.title, self.message, self.click = title, message, click
        self.interval_s, self.max_sends = float(interval_s), int(max_sends)
        self.started_at = datetime.now()
        self.sends = 0
        self.failures = 0
        self.last_error: str | None = None
        self.stop_reason: str | None = None
        self.cancelled: bool | None = None     # Pushover receipt cancel outcome, once tried
        self._receipt: str | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel_thread: threading.Thread | None = None
        self._started = False

    @classmethod
    def resume_pushover(cls, cfg: dict, receipt: str, started_at: datetime) -> "PhoneAlarmRun":
        """A Pushover emergency sent before a daemon restart: still repeating on Pushover's side
        until it expires, and still cancellable by its receipt."""
        run = cls(dict(cfg, backend="pushover"), title="", message="")
        run._receipt, run.started_at, run.sends, run._started = receipt, started_at, 1, True
        return run

    # -- lifecycle
    def start(self) -> "PhoneAlarmRun":
        self._started = True
        self._thread = threading.Thread(target=self._run, name="phone-alarm", daemon=True)
        self._thread.start()
        return self

    def stop(self, reason: str) -> None:
        """Stop re-sending and cancel a Pushover emergency. Returns at once; the cancel runs on
        its own thread."""
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            self._stop.set()
            receipt = self._receipt
        if receipt:
            self._cancel_async(receipt)

    def join(self, timeout: float | None = None) -> None:
        for t in (self._thread, self._cancel_thread):
            if t is not None:
                t.join(timeout)

    @property
    def active(self) -> bool:
        """Still ringing: re-sends left (ntfy), or an emergency Pushover still repeating on
        Pushover's side (until acknowledged there, cancelled here, or expired)."""
        if self._stop.is_set() or not self._started:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        return (self.backend == "pushover" and self._receipt is not None
                and (datetime.now() - self.started_at).total_seconds() < PUSHOVER_EXPIRE_S)

    @property
    def finished(self) -> bool:
        """Ran its course without being stopped (every re-send made / the emergency expired)."""
        return self._started and not self._stop.is_set() and not self.active

    @property
    def receipt(self) -> str | None:
        """The Pushover receipt -- a secret, for the daemon's own store only; never published."""
        return self._receipt

    def status(self) -> dict:
        return {"ringing": self.active, "backend": self.backend, "sends": self.sends,
                "failures": self.failures, "started_at": self.started_at.isoformat(),
                "stop_reason": self.stop_reason, "cancelled": self.cancelled}

    # -- worker
    def _record(self, res: dict) -> None:
        with self._lock:
            if res.get("ok"):
                self.sends += 1
            else:
                self.failures += 1
                self.last_error = res.get("error") or "send failed"

    def _run(self) -> None:
        try:
            if self.backend == "pushover":
                res = send_pushover(self.cfg, self.title, self.message, priority=2,
                                    click=self.click)
                self._record(res)
                with self._lock:
                    receipt = res.get("receipt") if res.get("ok") else None
                    self._receipt = receipt
                    stopped = self._stop.is_set()
                if receipt and stopped:        # "I'm awake" landed while the send was in flight
                    self._cancel_async(receipt)
                return
            for _ in range(self.max_sends):
                if self._stop.is_set():
                    return
                self._record(send_ntfy(self.cfg, self.title, self.message, priority=5,
                                       click=self.click))
                if self._stop.wait(self.interval_s):
                    return
        except Exception as exc:                # a worker must never take anything down
            with self._lock:
                self.failures += 1
                self.last_error = _clean(f"{type(exc).__name__}: {exc}", self.cfg)

    def _cancel_async(self, receipt: str) -> None:
        with self._lock:
            if self._cancel_thread is not None:
                return

            def _go():
                res = cancel_pushover(self.cfg, receipt)
                with self._lock:
                    self.cancelled = bool(res.get("ok"))
                    if not res.get("ok"):
                        self.last_error = res.get("error")

            self._cancel_thread = threading.Thread(target=_go, name="phone-alarm-cancel",
                                                   daemon=True)
            self._cancel_thread.start()
