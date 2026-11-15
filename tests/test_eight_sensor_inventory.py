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
