"""Binding test against the REAL pyEight (lukas-clarke) library.

Verifies that ``EightSleepClient`` actually binds to the live library's classes — the
right import path, instance ``users`` dict, ``fetch_user_id``, the ``current_*``
properties, and the ``set_heating_level`` signature. No network/device is needed: we
construct real ``EightSleep``/``EightUser`` objects and inject realistic trend data.

Skipped automatically when pyEight is not installed (it is an optional dependency:
``pip install -e ".[eightsleep]"``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("pyeight", reason="pyEight not installed (optional extra)")

from pyeight.eight import EightSleep  # noqa: E402
from pyeight.user import EightUser  # noqa: E402

from sleepctl.adapters.eightsleep_cloud import EightSleepClient, map_stage  # noqa: E402
from sleepctl.models import SleepStage  # noqa: E402


def _client_with_user():
    eight = EightSleep("u@e.com", "pw", "America/New_York")
    user = EightUser(eight, "uid", "left")
    eight.users["uid"] = user
    nowz = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    user.current_side_temp = 75.0
    user.trends = [{
        "presenceStart": nowz,
        "sleepQualityScore": {"hrv": {"current": 71}, "respiratoryRate": {"current": 12.5}},
        "sessions": [{
            "stages": [{"stage": "light"}, {"stage": "asleepDeep"}],
            "timeseries": {"heartRate": [[nowz, 54]], "tempRoomC": [[nowz, 20.0]]},
        }],
    }]
    client = EightSleepClient("u@e.com", "pw", "America/New_York", side="left")
    client._eight, client._user, client._last_update = eight, user, datetime.now()
    return eight, user, client


def test_constructor_and_user_access_bind():
    eight, user, client = _client_with_user()
    # instance-level users dict + fetch_user_id used by the adapter's _select_user
    assert eight.fetch_user_id("left") == "uid"
    assert eight.users["uid"] is user


def test_read_frame_maps_real_properties():
    eight, user, client = _client_with_user()
    frame = client.read_frame()
    assert frame.heart_rate == 54
    assert frame.hrv == 71
    assert frame.respiratory_rate == 12.5
    assert frame.stage is map_stage(user.current_sleep_stage)  # 'asleepDeep' -> DEEP
    assert frame.stage is SleepStage.DEEP
    assert frame.presence is True


def test_setter_signature_matches():
    import inspect

    sig = inspect.signature(EightUser.set_heating_level)
    params = list(sig.parameters)
    # adapter calls set_heating_level(level, duration_s) positionally
    assert params[1] == "level"
    assert "duration" in params[2]
