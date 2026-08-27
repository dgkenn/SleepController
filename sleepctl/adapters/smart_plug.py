"""Generic Wi-Fi smart plug -> wake-therapy lamp.

The wake orchestrator already drives a "therapy plug": a bright lamp that snaps ON at the wake
moment (see ``adapters/hue.py``'s ``HueDawnDriver.set_therapy``). That path assumes a **Hue** plug
on a Hue bridge, so it cannot drive an ordinary Wi-Fi plug. This module provides the same
``set_therapy(on)`` interface for one that isn't Hue, so both plug into the identical daemon call
site and the orchestrator's wake logic stays the single source of truth for WHEN to fire.

Two backends, because the label on a no-name plug does not reliably tell you its protocol:

  * ``tuya``  -- local LAN control via ``tinytuya``. Most generic plugs (SmartLife / Tuya app)
    speak this. Local, so it keeps working when the internet or the vendor cloud is down --
    which matters for something whose only job is to fire at 05:30.
  * ``http``  -- an on-URL and an off-URL. The universal escape hatch: Home Assistant, Tasmota,
    ESPHome, Shelly, or any plug fronted by something that can expose two URLs. Use this when
    the plug turns out not to be Tuya.

SAFETY -- this is deliberately not a general-purpose switch:

  * **Hard maximum on-time.** The driver turns the plug OFF once ``max_on_min`` has elapsed since
    it turned it on, no matter what the caller says. A therapy lamp is a high-output device
    (and if it is genuinely a UV source, an exposure hazard); a stuck ``should_wake``, a daemon
    that wedges, or a missed state change must not leave it energised all day.
  * **Fail-safe toward OFF.** Every failure path leaves the driver believing the plug may be on,
    so the next tick retries the OFF. Turning a lamp off is always safe; leaving it on is not.
  * **Never fires on its own.** It only reflects what the wake orchestrator asks for, so the
    lamp can never come on mid-sleep -- the one behaviour that would actively harm the thing
    this whole system exists to protect.

Best-effort throughout, in the same spirit as every other adapter here: a missing dependency, an
unreachable plug or a malformed config degrades to "the lamp did not fire", never to an exception
that could take the control loop down.
"""

from __future__ import annotations

import time
import urllib.request
from typing import Optional

#: Default ceiling on how long the lamp may stay energised in one go. Comfortably longer than the
#: orchestrator's post-wake bright-light dose (``post_wake_light_min``, 20 min) so it never cuts a
#: legitimate dose short, but far short of "all day" if something upstream gets stuck on.
DEFAULT_MAX_ON_MIN = 45.0

#: Network timeout. Short on purpose: this runs on the control tick, and a plug that is slow to
#: answer must not stall the loop that is steering the bed.
_TIMEOUT_S = 4.0


def _http_switch(url: str) -> bool:
    """Fire one on/off URL. True on any 2xx."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:
        return False


def _tuya_switch(cfg: dict, on: bool) -> bool:
    """Switch a Tuya plug over the LAN via tinytuya. False if unavailable or it refuses."""
    device_id = (cfg or {}).get("device_id")
    ip = (cfg or {}).get("ip")
    local_key = (cfg or {}).get("local_key")
    if not (device_id and ip and local_key):
        return False
    try:
        import tinytuya  # lazy: only needed when this backend is actually configured
    except Exception:
        return False
    try:
        d = tinytuya.OutletDevice(device_id, ip, local_key)
        d.set_version(float((cfg or {}).get("version") or 3.3))
        d.set_socketTimeout(_TIMEOUT_S)
        switch_dp = int((cfg or {}).get("switch_dp") or 1)
        res = d.set_status(bool(on), switch_dp)
        # tinytuya returns a dict; an "Error" key means the device refused or did not answer.
        return not (isinstance(res, dict) and res.get("Error"))
    except Exception:
        return False


def switch(backend: str, cfg: dict, on: bool) -> bool:
    """Turn the configured plug on/off. Returns True only if the plug confirmed the command."""
    backend = (backend or "").strip().lower()
    if backend == "tuya":
        return _tuya_switch(cfg or {}, on)
    if backend == "http":
        c = cfg or {}
        return _http_switch(c.get("on_url") if on else c.get("off_url"))
    return False


class SmartPlugTherapyDriver:
    """Same ``set_therapy(on)`` contract as ``HueDawnDriver``, for a non-Hue Wi-Fi plug.

    Only acts on a CHANGE of state, so a plug that is already on is not re-commanded every tick
    -- except for the safety paths below, which deliberately retry.
    """

    def __init__(self, backend: str, cfg: dict, max_on_min: float = DEFAULT_MAX_ON_MIN,
                 clock=time.monotonic) -> None:
        self.backend = backend
        self.cfg = cfg or {}
        self.max_on_min = float(max_on_min or 0.0)
        self._clock = clock
        self._on: Optional[bool] = None      # None = unknown (startup): first command always sent
        self._on_since: Optional[float] = None

    # -- internals ---------------------------------------------------------------------------
    def _command(self, on: bool) -> bool:
        ok = switch(self.backend, self.cfg, on)
        if ok:
            self._on = on
            self._on_since = self._clock() if on else None
        elif on:
            # Failed to turn ON: nothing is energised, so record OFF and let the caller retry.
            self._on = False
            self._on_since = None
        else:
            # Failed to turn OFF -- the dangerous direction. Leave the state as "possibly on" so
            # the next tick tries again rather than concluding the job is done.
            self._on = True
        return ok

    # -- public ------------------------------------------------------------------------------
    def set_therapy(self, on: bool) -> None:
        """Reflect the orchestrator's wake decision onto the plug, with the on-time cap applied."""
        try:
            on = bool(on)
            if on and self.max_on_min > 0 and self._on and self._on_since is not None:
                elapsed_min = (self._clock() - self._on_since) / 60.0
                if elapsed_min >= self.max_on_min:
                    # Cap reached. Force OFF and STAY off for the rest of this wake -- do not let
                    # the next tick's still-true should_wake immediately re-energise it.
                    self._command(False)
                    self._on_since = None
                    self._capped = True
                    return
            if on and getattr(self, "_capped", False):
                return                      # cap latched; cleared when the caller asks for OFF
            if not on:
                self._capped = False
            if on == self._on:
                return
            self._command(on)
        except Exception:
            pass                            # never raise into the control loop

    def off(self) -> None:
        """Explicit fail-safe used on shutdown/session end."""
        self.set_therapy(False)
