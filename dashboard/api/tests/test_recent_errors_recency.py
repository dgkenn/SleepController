"""daemon.err is append-only, so "any error ever written" held the report DEGRADED forever.

The one that did it was a HANDLED transient -- "device update failed, using stale data:
EightSleepRequestError(... TimeoutError())" -- an Eight Sleep cloud timeout the controller
recovered from by design, reported for days as the headline finding of the entire system.
"""

import os
import time

import app.diagnostics as diag

_ERR = ("device update failed, using stale data: EightSleepRequestError('GET "
        "https://client-api.8slp.net/v1/devices/370034000650335330373220 failed: TimeoutError()')")


def _run_dir(tmp_path, lines, age_s=0.0):
    path = os.path.join(tmp_path, "daemon.err")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    return str(tmp_path)


def _check(run_dir):
    return diag._check_recent_errors(run_dir, time.time(), daemon_heartbeat_age=5.0)


def test_a_stale_error_with_nothing_since_is_history():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = _check(_run_dir(tmp, [_ERR], age_s=30 * 3600))
    assert r["status"] == "info"
    assert "nothing since" in r["detail"]


def test_a_fresh_error_still_warns():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = _check(_run_dir(tmp, [_ERR], age_s=0.0))
    assert r["status"] == "warn"


def test_a_repeating_error_reports_how_often():
    """A single last line could not distinguish one timeout from a hundred."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = _check(_run_dir(tmp, [_ERR] * 12, age_s=0.0))
    assert r["status"] == "warn"
    assert "x12" in r["detail"]


def test_the_signature_ignores_ids_and_timestamps():
    a = _error_line("2026-09-05 01:02:03 device 370034000650335330373220 failed")
    b = _error_line("2026-09-06 22:11:00 device 370034000650335330374111 failed")
    assert diag._error_signature(a) == diag._error_signature(b)


def _error_line(s):
    return s


def test_different_errors_are_not_collapsed():
    assert (diag._error_signature("TimeoutError on GET devices")
            != diag._error_signature("ConnectionReset on POST temperature"))


def test_an_empty_log_is_ok():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = _check(str(tmp))
    assert r["status"] == "ok"
