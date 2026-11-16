"""What Eight Sleep's sleep-data feed returns, recorded so a subscription change is visible."""
from sleepctl.adapters.eightsleep_direct import EightSleepDirectClient


def _days(ts_keys=(), stages=False, presence=False):
    session = {"timeseries": {k: [["2026-11-15T03:00:00Z", 1.0]] for k in ts_keys}}
    session["timeseries"]["empty"] = []
    if stages:
        session["stages"] = [{"stage": "deep", "duration": 60}]
    if presence:
        session["presenceStart"] = "2026-11-14T23:00:00Z"
    return [{"sessions": [session]}]


def test_inventory_lists_only_keys_that_carry_data():
    inv = EightSleepDirectClient._inventory(_days(("heartRate", "tempBedC", "tempRoomC"),
                                                 stages=True, presence=True))
    assert inv == {"trends_keys": ["heartRate", "tempBedC", "tempRoomC"], "stages": True,
                   "presence": True}


def test_an_empty_feed_reports_nothing():
    assert EightSleepDirectClient._inventory([]) == {"trends_keys": [], "stages": False,
                                                     "presence": False}


def test_history_reports_nights_side_and_away():
    import asyncio
    c = EightSleepDirectClient.__new__(EightSleepDirectClient)
    c.sensor_inventory = {}
    c.timezone, c._user_id, c.side = "America/New_York", "u1", "right"
    c._device = {"leftUserId": "u2", "rightUserId": "u1"}

    async def _req(method, url, **kw):
        if url.endswith("/trends"):
            return {"days": [{"day": "2026-11-14", "sessions": []},
                             {"day": "2026-11-15", "sessions": [{}], "score": 80}]}
        return {"isAway": False}
    c._request = _req
    from datetime import datetime
    asyncio.new_event_loop().run_until_complete(c._refresh_history(datetime(2026, 11, 16, 9)))
    inv = c.sensor_inventory
    assert inv["nights_7d"] == [{"day": "2026-11-14", "sessions": 0},
                                {"day": "2026-11-15", "sessions": 1}]
    assert inv["assigned_side"] == "right" and inv["reading_side"] == "right"
    assert inv["away"] is False and "score" in inv["day_keys"]


def test_a_solo_pod_listing_you_on_both_sides_says_both():
    import asyncio
    from datetime import datetime
    c = EightSleepDirectClient.__new__(EightSleepDirectClient)
    c.sensor_inventory, c.timezone, c._user_id, c.side = {}, "UTC", "u1", "right"
    c._device = {"leftUserId": "u1", "rightUserId": "u1"}

    async def _req(method, url, **kw):
        return {"days": []} if url.endswith("/trends") else {"isAway": False}
    c._request = _req
    asyncio.new_event_loop().run_until_complete(c._refresh_history(datetime(2026, 11, 16)))
    assert c.sensor_inventory["assigned_side"] == "both"
