"""`bed_temp_f` has been None on 6835 of 6835 samples across every captured night, which keeps
the thermal loop permanently open. A bare `return None` at five different steps made it
impossible to tell WHY from outside the box -- no trends at all, trends with no session, or a
session whose timeseries lacks tempBedC all want different responses.
"""
from sleepctl.adapters.eightsleep_cloud import EightSleepClient


class _User:
    def __init__(self, trends):
        self.trends = trends


def _client():
    return EightSleepClient(email="e", password="p")


def test_a_reading_is_converted_and_clears_the_reason():
    c = _client()
    u = _User([{"sessions": [{"timeseries": {"tempBedC": [[0, 20.0], [1, 25.0]]}}]}])
    assert c._sensed_bed_temp_f(u) == 77.0        # 25 C
    assert c.last_bed_temp_reason is None


def test_no_trends_is_distinguished_from_every_other_failure():
    c = _client()
    assert c._sensed_bed_temp_f(_User([])) is None
    assert "no trends" in c.last_bed_temp_reason


def test_trends_without_a_session_is_its_own_reason():
    c = _client()
    assert c._sensed_bed_temp_f(_User([{"sessions": []}])) is None
    assert "no sessions" in c.last_bed_temp_reason


def test_a_timeseries_without_tempbedc_names_the_keys_it_does_have():
    """The most useful case: it tells us what the account DOES expose, so the next step is
    evidence rather than a guess at an API surface."""
    c = _client()
    u = _User([{"sessions": [{"timeseries": {"tnt": [], "heartRate": []}}]}])
    assert c._sensed_bed_temp_f(u) is None
    assert "no tempBedC" in c.last_bed_temp_reason
    assert "heartRate" in c.last_bed_temp_reason


def test_a_null_newest_sample_is_not_confused_with_a_missing_series():
    c = _client()
    u = _User([{"sessions": [{"timeseries": {"tempBedC": [[0, 20.0], [1, None]]}}]}])
    assert c._sensed_bed_temp_f(u) is None
    assert "newest sample is null" in c.last_bed_temp_reason


def test_an_exception_is_captured_rather_than_swallowed_silently():
    class Exploding:
        @property
        def trends(self):
            raise RuntimeError("boom")
    c = _client()
    assert c._sensed_bed_temp_f(Exploding()) is None
    assert "RuntimeError" in c.last_bed_temp_reason and "boom" in c.last_bed_temp_reason


def test_the_circular_level_derived_temperature_is_never_returned():
    """`current_bed_temp` is derived from the commanded level, so closing the loop on it would
    make the controller read its own command back as a measurement."""
    class WithCircular:
        trends = []
        current_bed_temp = 999.0
    c = _client()
    assert c._sensed_bed_temp_f(WithCircular()) is None
