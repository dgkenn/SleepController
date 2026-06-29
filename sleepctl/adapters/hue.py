"""Philips Hue dawn-light adapter — sunrise simulation as the silent alarm's strongest cue.

Light is the most potent circadian / alerting signal, and it stays SILENT — perfect for a
"wake without noise" alarm. The wake orchestrator already emits a ``light_level`` (0..1) ramp
through the dawn window; this drives the user's Hue bulbs to match: a warm, dim amber at the
start that brightens and cools toward wake, then off once they're up.

Uses the **local** Hue Bridge REST API over the LAN (stdlib urllib only — no cloud account, no
deps, no paid services). Pairing is the standard press-the-link-button flow. Everything is
best-effort: if the bridge is unreachable the alarm's thermal + vibration channels are unaffected.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

_DISCOVERY = "https://discovery.meethue.com"
_APP_NAME = "sleepctl#dawn"


def _req(method: str, url: str, body: Optional[dict] = None, timeout: float = 4.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def discover_bridges(timeout: float = 4.0) -> list:
    """LAN bridge IPs via Hue's discovery service (returns [{'id','internalipaddress'}, ...])."""
    try:
        return _req("GET", _DISCOVERY, timeout=timeout) or []
    except Exception:
        return []


def create_token(bridge_ip: str) -> str:
    """Create an API token after the user presses the bridge's link button. Raises a friendly
    error if the button hasn't been pressed yet."""
    res = _req("POST", f"http://{bridge_ip}/api", {"devicetype": _APP_NAME})
    if isinstance(res, list) and res:
        if "success" in res[0]:
            return res[0]["success"]["username"]
        if "error" in res[0] and res[0]["error"].get("type") == 101:
            raise RuntimeError("press the link button on the Hue bridge, then try again "
                               "(within 30 seconds)")
    raise RuntimeError(f"unexpected Hue pairing response: {res}")


def list_targets(bridge_ip: str, token: str) -> dict:
    """{'lights': {id: name}, 'groups': {id: name}} — what the dawn can target (a room/group is
    usually the right choice)."""
    out = {"lights": {}, "groups": {}}
    try:
        for i, l in (_req("GET", f"http://{bridge_ip}/api/{token}/lights") or {}).items():
            out["lights"][i] = l.get("name", f"light {i}")
        for i, g in (_req("GET", f"http://{bridge_ip}/api/{token}/groups") or {}).items():
            out["groups"][i] = g.get("name", f"group {i}")
    except Exception:
        pass
    return out


def sunrise_state(level: float, transition_s: float = 8.0) -> dict:
    """Map a 0..1 dawn level to a Hue light state: warm+dim amber -> brighter+cooler toward wake.
    level <= 0 turns the light off (used once you're confirmed up)."""
    level = max(0.0, min(1.0, level))
    if level <= 0.0:
        return {"on": False, "transitiontime": int(transition_s * 10)}
    bri = max(1, round(level * 254))                 # 1..254 brightness ramp
    ct = round(454 - level * (454 - 250))            # mirek: 454 (~2200K warm) -> 250 (~4000K)
    return {"on": True, "bri": bri, "ct": ct, "transitiontime": int(transition_s * 10)}


def _as_list(targets) -> list:
    if targets is None:
        return []
    return list(targets) if isinstance(targets, (list, tuple)) else [targets]


def apply(bridge_ip: str, token: str, targets, level: float,
          kind: str = "group", transition_s: float = 8.0) -> bool:
    """Push a sunrise level to one or more lights, or a room/group (one call lights everything in
    it). ``targets`` is a single id or a list. Returns True if any push succeeded (best-effort)."""
    state = sunrise_state(level, transition_s)
    ok = False
    for tid in _as_list(targets):
        path = (f"http://{bridge_ip}/api/{token}/groups/{tid}/action" if kind == "group"
                else f"http://{bridge_ip}/api/{token}/lights/{tid}/state")
        try:
            _req("PUT", path, state)
            ok = True
        except Exception:
            continue
    return ok


def set_power(bridge_ip: str, token: str, ids, on: bool) -> bool:
    """Turn one or more lights/PLUGS on or off (just ``{"on": ...}`` — a smart plug can't dim, so
    a bright therapy lamp plugged into it is purely binary). Returns True if any succeeded."""
    ok = False
    for tid in _as_list(ids):
        try:
            _req("PUT", f"http://{bridge_ip}/api/{token}/lights/{tid}/state", {"on": bool(on)})
            ok = True
        except Exception:
            continue
    return ok


class HueDawnDriver:
    """Throttled, best-effort driver the daemon calls each tick. Drives TWO roles:
      • the sunrise bulbs (room/group or individual) via a 0..1 ramp (``set_level``)
      • an optional therapy PLUG (a 10k-lux lamp) that snaps ON at the wake moment (``set_therapy``)
    Only pushes on a meaningful change so the bridge isn't spammed."""

    def __init__(self, bridge_ip: str, token: str, targets, kind: str = "group",
                 therapy_ids=None, min_delta: float = 0.05) -> None:
        self.bridge_ip = bridge_ip
        self.token = token
        self.targets = _as_list(targets)
        self.kind = kind
        self.therapy_ids = _as_list(therapy_ids)
        self.min_delta = min_delta
        self._last: Optional[float] = None
        self._therapy_on: Optional[bool] = None

    def set_level(self, level: float) -> None:
        if not self.targets:
            return
        if self._last is not None and abs(level - self._last) < self.min_delta and not (
                level == 0.0 and self._last != 0.0):
            return
        if apply(self.bridge_ip, self.token, self.targets, level, self.kind):
            self._last = level

    def set_therapy(self, on: bool) -> None:
        """Turn the bright therapy lamp on/off (only on a state change)."""
        if not self.therapy_ids or on == self._therapy_on:
            return
        if set_power(self.bridge_ip, self.token, self.therapy_ids, on):
            self._therapy_on = on
