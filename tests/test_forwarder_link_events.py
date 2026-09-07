"""The forwarder tells the API when the link opens or closes, and a failed status post never
takes down the session it describes."""

import importlib.util
import sys
import types
from pathlib import Path


def _load_forwarder():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verity_forwarder.py"
    spec = importlib.util.spec_from_file_location("verity_forwarder_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _args():
    return types.SimpleNamespace(source="verity", url="http://localhost:8000/hr/ingest?token=x")


def test_connected_carries_the_streams(monkeypatch):
    fw = _load_forwarder()
    posted = []
    monkeypatch.setattr(fw, "_post", lambda url, payload, timeout=5.0: posted.append(payload))
    fw._post_link(_args(), "connected", ["ACC@52Hz", "PPI"])
    assert posted == [{"source": "verity", "link": "connected", "streams": ["ACC@52Hz", "PPI"]}]


def test_lost_carries_no_streams(monkeypatch):
    fw = _load_forwarder()
    posted = []
    monkeypatch.setattr(fw, "_post", lambda url, payload, timeout=5.0: posted.append(payload))
    fw._post_link(_args(), "lost")
    assert posted == [{"source": "verity", "link": "lost"}]


def test_a_failed_status_post_is_swallowed(monkeypatch):
    fw = _load_forwarder()

    def _boom(url, payload, timeout=5.0):
        raise OSError("api down")

    monkeypatch.setattr(fw, "_post", _boom)
    fw._post_link(_args(), "connected", ["PPI"])   # must not raise
