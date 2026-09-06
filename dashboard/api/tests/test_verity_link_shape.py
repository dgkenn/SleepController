"""Cannot see the band, versus can see it and it refuses -- different faults, different remedies.

On 2026-09-05, an hour before bed, the log read

    found 'Polar Sense 16961D33' at 24:AC:AC:16:96:1D
    connecting to 24:AC:AC:16:96:1D ...
    session error (TimeoutError: ); reconnecting in 25s (consecutive failures: 153)

over and over -- the band advertising healthily, on the wrist, every connect timing out. That is
another central holding it, which a Bluetooth reset on this side cannot fix. Every check on the
page was green, because the last sample was only a couple of hours old.
"""

import os

import app.diagnostics as diag


def _log(tmp_path, lines):
    with open(os.path.join(tmp_path, "verity.log"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return str(tmp_path)


_REFUSING = ["found 'Polar Sense 16961D33' at 24:AC:AC:16:96:1D",
             "connecting to 24:AC:AC:16:96:1D ...",
             "session error (TimeoutError: ); reconnecting in 25s (consecutive failures: 153)"] * 4

_ABSENT = ["scanning for a Polar/BLE heart-rate sensor (20s)...",
           "no match among 7 advertising device(s): ?@D8:0F:99:2D:8A:10",
           "session error (BleakDeviceNotFoundError: ...); reconnecting in 25s "
           "(consecutive failures: 90)"] * 4

_HEALTHY = _REFUSING[:3] + ["connecting to 24:AC:AC:16:96:1D ...", "connected",
                            "PMD: start PPI ok"]


class _Repo:
    conn = None


def test_a_band_that_is_found_but_refuses_is_identified(tmp_path):
    shape, detail = diag._verity_link_shape(_log(tmp_path, _REFUSING))
    assert shape == "refusing"
    assert "Polar app" in detail


def test_a_band_that_is_not_advertising_is_a_different_shape(tmp_path):
    shape, _ = diag._verity_link_shape(_log(tmp_path, _ABSENT))
    assert shape == "absent"


def test_a_recent_success_clears_the_diagnosis(tmp_path):
    assert diag._verity_link_shape(_log(tmp_path, _HEALTHY)) == (None, None)


def test_the_check_warns_even_when_the_last_sample_is_recent(monkeypatch, tmp_path):
    """The whole failure: a green page an hour before bed, and no feed all night."""
    monkeypatch.setattr("app.bridge.read_cardiac_sample", lambda conn: {"age_seconds": 2.8 * 3600})
    r = diag._check_wearable_reachable(_Repo(), _log(tmp_path, _REFUSING))
    assert r["status"] == "warn"
    assert "times out" in r["detail"]
    assert "Polar app" in r["remedy"] or "power-cycle" in r["remedy"]


def test_a_streaming_band_is_still_ok(monkeypatch, tmp_path):
    monkeypatch.setattr("app.bridge.read_cardiac_sample", lambda conn: {"age_seconds": 3.0})
    r = diag._check_wearable_reachable(_Repo(), _log(tmp_path, _HEALTHY))
    assert r["status"] == "ok"


def test_no_log_is_not_a_diagnosis(tmp_path):
    assert diag._verity_link_shape(str(tmp_path)) == (None, None)
