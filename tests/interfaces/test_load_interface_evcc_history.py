"""
The EVCC history branch of the load interface.

EVCC's energy history is the third source beside Home Assistant and openHAB: one
`/api/history/energy` request answers a whole day per virtual entity
(``group:home``, ``group:loadpoint:Carport``), and every slot is then cut out of
that series locally - the same day-cache contract the Home Assistant path
observes since issue #302.

The numbers this source produces have their own pinned expectations: kWh slots
integrated to average W, clipped to the requested range, never negative, and the
virtual-entity grammar rejected where it is malformed. The tests patch the
interface's clock where a profile build reads "now", because the profile looks
back exactly 7/14 days and a drifting wall clock would make the fixture miss.
"""

# The behaviour under test lives behind private names - the day cache, the
# request plumbing. Reaching for them is the point, not an accident.
# pylint: disable=duplicate-code,protected-access

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.interfaces.load_interface import LoadInterface

EVCC_CONFIG = {
    "source": "evcc",
    "url": "http://evcc:7070",
    "load_sensor": "group:home",
    "max_retries": 2,
    "retry_backoff": 0,
    "warning_threshold": 1,
}

DAY_START = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
WATTS = [200 + (index % 7) * 130 for index in range(96)]


class FakeEvccHistory:
    """`/api/history/energy` answered from a repeating W-per-15-min fixture.

    The real endpoint serves any range its database holds and a profile build asks
    for four different days (7/14 days back, plus tomorrow); the fake answers every
    requested range with the same repeating day, so what gets tested is the
    one-request-per-entity contract rather than the fixture's coverage.
    """

    def __init__(self, watts):
        self.watts = list(watts)
        self.calls = []

    def __call__(self, url, params=None, **kwargs):
        # pylint: disable=unused-argument
        assert "/api/history/energy" in url
        self.calls.append(dict(params or {}))
        response = MagicMock()
        response.status_code = 200

        # The interface filters client-side by group and title, so whatever the
        # endpoint serves is passed through as the requested series.
        frm = datetime.fromisoformat(params["from"])
        to = datetime.fromisoformat(params["to"])
        each = timedelta(minutes=15)
        cursor = frm.replace(minute=(frm.minute // 15) * 15, second=0, microsecond=0)
        data = []
        value_index = 0
        while cursor < to:
            watt = self.watts[value_index % len(self.watts)] if self.watts else 0.0
            data.append(
                {
                    "start": cursor.isoformat(),
                    "end": (cursor + each).isoformat(),
                    "energy": watt * 0.25 / 1000.0,
                }
            )
            cursor += each
            value_index += 1
        response.json.return_value = [{"group": "home", "title": None, "data": data}]
        return response


@contextmanager
def _connected(fake):
    with patch("src.interfaces.load_interface.requests.get", fake), patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        yield


def _interface(**overrides):
    return LoadInterface({**EVCC_CONFIG, **overrides}, 3600, "UTC")


def _pinned_to(day_start):
    """The interface clock pinned to `day_start`'s evening, so its four profile
    days (7/14 days back, tomorrow) all land on the fixture's repeating answer."""
    return patch.object(
        LoadInterface,
        "_LoadInterface__now",
        return_value=day_start + timedelta(days=21) + timedelta(hours=12),
    )


def test_home_group_profile_matches_measured_watts():
    """96 quarter-slots of group:home become 24 hourly Wh values."""
    fake = FakeEvccHistory(WATTS)
    interface = _interface()
    with _connected(fake), _pinned_to(DAY_START):
        profile = interface.get_load_profile_for_day(
            DAY_START + timedelta(days=21), DAY_START + timedelta(days=22)
        )

    assert len(profile) == 24
    assert profile[0] == pytest.approx(sum(WATTS[0:4]) / 4, rel=1e-6)
    assert all(value >= 0 for value in profile)


def test_kilowatt_hours_are_integrated_not_taken_as_watts():
    """A kWh slot must become power first; taking the number raw is 1000x off."""
    fake = FakeEvccHistory([500] * 96)
    interface = _interface()
    with _connected(fake):
        samples = interface.fetch_historical_energy_data(
            "group:home", DAY_START, DAY_START + timedelta(minutes=15)
        )
    values = [entry["state"] for entry in samples]
    # 0.125 kWh over 15 min = 500 W
    assert values and all(value == pytest.approx(500.0) for value in values)


def test_virtual_entity_grammar_is_enforced():
    """A malformed entity answers empty and is rejected before any request."""
    fake = FakeEvccHistory(WATTS)
    interface = _interface()
    with _connected(fake):
        # A bare group name without the "group:" prefix and an unknown group both
        # fail the grammar and never reach the endpoint...
        assert (
            interface.fetch_historical_energy_data(
                "home", DAY_START, DAY_START + timedelta(hours=1)
            )
            == []
        )
        assert (
            interface.fetch_historical_energy_data(
                "group:nosuch", DAY_START, DAY_START + timedelta(hours=1)
            )
            == []
        )
        # ...while a valid virtual entity does.
        valid = interface.fetch_historical_energy_data(
            "group:home", DAY_START, DAY_START + timedelta(hours=1)
        )
    assert fake.calls, "valid entities reach the endpoint"
    assert valid, "the group:home series answers"


def test_two_edge_samples_per_slot():
    """Each slot is stamped twice, opening and closing, like an HA history period.

    The slot integration downstream (__process_energy_data) needs duration
    between samples; a single stamp per slot would leave every slot 0 Wh.
    """
    fake = FakeEvccHistory([500] * 96)
    interface = _interface()
    with _connected(fake):
        samples = interface.fetch_historical_energy_data(
            "group:home", DAY_START, DAY_START + timedelta(minutes=15)
        )
    stamps = [entry["last_updated"] for entry in samples]
    assert len(samples) == 2
    assert stamps[0] == DAY_START.isoformat()
    assert stamps[1] == (DAY_START + timedelta(minutes=15)).isoformat()


def test_day_build_makes_one_request_per_entity():
    """The issue-#302 contract on this source too: one request per day, not per slot.

    A profile build reads four historical days; at the 24-requests-per-sensor-per-
    slot behaviour this replaced, the same build would issue 96. The build clears
    the caches when it finishes, so the window to assert in is exactly one build.
    """
    fake = FakeEvccHistory(WATTS)
    interface = _interface()
    with _connected(fake), _pinned_to(DAY_START):
        interface.refresh_load_profile()
        # 4 historical days x 1 entity = 4 requests; 96 would be one per slot.
        assert len(fake.calls) == 4


def test_rolling_window_bypasses_day_cache():
    """The price handler's rolling lookback must re-request, not read a frozen day."""
    fake = FakeEvccHistory([500] * 96)
    interface = _interface()
    near_now = datetime.now(timezone.utc) - timedelta(hours=2)
    with _connected(fake):
        for _ in range(3):
            interface.fetch_historical_energy_data(
                "group:home", near_now - timedelta(hours=2), near_now
            )
    assert len(fake.calls) == 3


def test_slots_are_clipped_to_the_requested_range():
    """A request starting mid-slot keeps the slot's rate, bounded at its edges.

    EVCC answers whole 15-minute slots; a rolling window from :07 gets the slot
    clipped to the 8 minutes actually requested. The stamps bound that span and
    the rate stays the slot's own average (0.9 kWh over 15 min = 3600 W), which
    is what the integration downstream turns back into the clipped energy.

    The rate is the point of this test. Integrating the whole slot's energy over
    only the requested 8 minutes reported 6750 W for a 3600 W quarter-hour, and
    the price handler's window - which never opens on a slot boundary - took
    that inflated first sample into its charging-event detection.
    """
    slot_start = DAY_START + timedelta(hours=6)
    fake = FakeEvccHistory([3600])
    interface = _interface()
    with _connected(fake):
        samples = interface.fetch_historical_energy_data(
            "group:home",
            slot_start + timedelta(minutes=7),
            slot_start + timedelta(minutes=15),
        )
    values = [entry["state"] for entry in samples]
    stamps = [datetime.fromisoformat(entry["last_updated"]) for entry in samples]
    assert values, "the clipped slot is answered"
    # 0.9 kWh over the slot's own 15 minutes, whatever slice was asked for.
    assert all(value == pytest.approx(3600.0) for value in values)
    # The stamps are the requested window's edges, not the slot's.
    assert stamps[0] == slot_start + timedelta(minutes=7)
    assert stamps[-1] == slot_start + timedelta(minutes=15)


def test_the_open_slot_divides_by_the_time_that_has_elapsed():
    """The slot still being filled carries only the energy accumulated so far.

    Its nominal end lies in the future, so dividing by the full 15 minutes would
    under-report the current power by whatever fraction of the slot is still to
    come. The divisor has to be the elapsed part - which is why the fix for the
    clipped-range case above bounds the integration span at "now" rather than at
    the slot's end. The clock is pinned five minutes into the slot.
    """
    slot_start = DAY_START + timedelta(hours=6)
    pinned_now = slot_start + timedelta(minutes=5)
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter, patch(
        "src.interfaces.load_interface.time.sleep"
    ), patch.object(LoadInterface, "_LoadInterface__now", return_value=pinned_now):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {
                "group": "home",
                "title": None,
                "data": [
                    {
                        "start": slot_start.isoformat(),
                        "end": (slot_start + timedelta(minutes=15)).isoformat(),
                        # 5 of 15 minutes elapsed, 0.1 kWh so far = 1200 W
                        "energy": 0.1,
                    }
                ],
            }
        ]
        getter.return_value = response
        samples = interface.fetch_historical_energy_data(
            "group:home", slot_start, pinned_now
        )
    values = [entry["state"] for entry in samples]
    assert values, "the open slot is answered"
    assert all(value == pytest.approx(1200.0) for value in values)


def test_a_whole_group_asks_evcc_to_sum_it():
    """A group reference without a title must ask EVCC to do the summing.

    EVCC's handler reads ``grouped := q.Get("grouped") == "true"``, so the
    parameter cannot simply be left out: its default is per-entity series, and a
    group with two meters then arrives as two of them. The group filter rides
    along so EVCC narrows the query instead of answering with every meter it has
    and letting this module throw most of it away.
    """
    fake = FakeEvccHistory(WATTS)
    interface = _interface()
    with _connected(fake):
        interface.fetch_historical_energy_data(
            "group:pv", DAY_START, DAY_START + timedelta(hours=1)
        )
    params = fake.calls[0]
    assert params["grouped"] == "true"
    assert params["group"] == "pv"
    assert "title" not in params


def test_a_titled_reference_asks_for_that_one_series():
    """With a title the per-entity answer is the point, so no grouped=true."""
    fake = FakeEvccHistory(WATTS)
    interface = _interface()
    with _connected(fake):
        interface.fetch_historical_energy_data(
            "group:loadpoint:Carport", DAY_START, DAY_START + timedelta(hours=1)
        )
    params = fake.calls[0]
    assert params["group"] == "loadpoint"
    assert params["title"] == "Carport"
    assert params.get("grouped") != "true"


def test_two_series_of_one_group_are_added_not_dropped():
    """Should a site answer per entity anyway, its meters still add up.

    Two batteries measured in the same slot are the group's power together, not
    whichever series the merge happened to write last. Keeping one of them was a
    silent undercount feeding straight into the battery price calculation.
    """
    slot_start = DAY_START
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter, patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {
                "group": "battery",
                "title": "Battery 1",
                "data": [
                    {
                        "start": slot_start.isoformat(),
                        "end": (slot_start + timedelta(minutes=15)).isoformat(),
                        "energy": 0.25,  # 1000 W
                    }
                ],
            },
            {
                "group": "battery",
                "title": "Battery 2",
                "data": [
                    {
                        "start": slot_start.isoformat(),
                        "end": (slot_start + timedelta(minutes=15)).isoformat(),
                        "energy": 0.1,  # 400 W
                    }
                ],
            },
        ]
        getter.return_value = response
        samples = interface.fetch_historical_energy_data(
            "group:battery", slot_start, slot_start + timedelta(minutes=15)
        )
    values = [entry["state"] for entry in samples]
    assert values, "the group answers"
    assert all(value == pytest.approx(1400.0) for value in values)


def test_return_energy_is_subtracted():
    """Grid export (returnEnergy) leaves the grid import, or feed-in reads as load."""
    slot_start = DAY_START
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter, patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {
                "group": "grid",
                "title": None,
                "data": [
                    {
                        "start": slot_start.isoformat(),
                        "end": (slot_start + timedelta(minutes=15)).isoformat(),
                        "energy": 1.0,
                        "returnEnergy": 0.6,
                    }
                ],
            }
        ]
        getter.return_value = response
        samples = interface.fetch_historical_energy_data(
            "group:grid", slot_start, slot_start + timedelta(minutes=15)
        )
    values = [entry["state"] for entry in samples]
    # 0.4 kWh net over 15 min = 1600 W, import positive
    assert values and all(value == pytest.approx(1600.0) for value in values)


def test_battery_discharge_is_signed_not_clamped():
    """A discharging slot must stay negative - it is the discharge signal itself.

    The price calculation distinguishes PV charging from discharging by the
    sign; clamping negative slots to zero turned every discharge into a no-op
    and left the calculation with "no energy charged in identified events" - the
    live-site failure these EVCC branches were verified against.
    """
    slot_start = DAY_START
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter, patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {
                "group": "battery",
                "title": None,
                "data": [
                    {
                        # discharge: nothing charged, 0.18 kWh returned
                        "start": slot_start.isoformat(),
                        "end": (slot_start + timedelta(minutes=15)).isoformat(),
                        "energy": 0.0,
                        "returnEnergy": 0.18,
                    }
                ],
            }
        ]
        getter.return_value = response
        samples = interface.fetch_historical_energy_data(
            "group:battery", slot_start, slot_start + timedelta(minutes=15)
        )
    values = [entry["state"] for entry in samples]
    # -0.18 kWh over 15 min = -720 W, discharge negative (charging positive)
    assert values and all(value == pytest.approx(-720.0) for value in values)


def test_get_load_profile_hands_to_the_day_builder():
    """The profile is built once per day and held, same as the other sources."""
    interface = _interface()
    with patch.object(LoadInterface, "refresh_load_profile", return_value=[1.0] * 48):
        assert interface.get_load_profile(48) == [1.0] * 48


def test_tariff_grid_answers_price_history():
    """``tariff:grid`` reads the persisted tariff slots, in EUR/kWh point samples.

    The price handler's converter divides values above 1.0 by 100 (its ct/kWh
    heuristic); EVCC's currency/kWh sits below that line, so the values must
    pass through unchanged.
    """
    slot_start = DAY_START
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter, patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"start": slot_start.isoformat(), "grid": 0.27, "feedin": 0.08},
            {"start": (slot_start + timedelta(minutes=15)).isoformat(), "grid": 0.31},
        ]
        getter.return_value = response
        samples = interface.fetch_historical_energy_data(
            "tariff:grid", slot_start, slot_start + timedelta(minutes=30)
        )
    values = [entry["state"] for entry in samples]
    assert values == [0.27, 0.31], "EUR/kWh values pass through unconverted"
    # feedin is not leaked into a grid request
    assert all(0.08 != value for value in values)


def test_tariff_feedin_answers_separately():
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter, patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [
            {"start": DAY_START.isoformat(), "grid": 0.27, "feedin": 0.08}
        ]
        getter.return_value = response
        samples = interface.fetch_historical_energy_data(
            "tariff:feedin", DAY_START, DAY_START + timedelta(minutes=15)
        )
    assert [entry["state"] for entry in samples] == [0.08]


def test_tariff_unknown_name_is_rejected():
    interface = _interface()
    with patch("src.interfaces.load_interface.requests.get") as getter:
        assert (
            interface.fetch_historical_energy_data(
                "tariff:co2", DAY_START, DAY_START + timedelta(minutes=15)
            )
            == []
        )
    getter.assert_not_called()


def test_an_unreadable_name_is_reported_once_not_per_slot(caplog):
    """A profile build asks per slot; the answer does not change between them.

    A managed load carrying a Home Assistant sensor name needs the second
    connection, and with none configured there is nothing to read it with. That
    produced one line per slot per day - around a hundred identical ones for a
    single appliance, which buried everything else. Once per name now, naming
    the thing that is missing.
    """
    interface = _interface()
    with caplog.at_level(logging.WARNING, logger="__main__"):
        for _ in range(5):
            assert (
                interface.fetch_historical_energy_data(
                    "sensor.heatpump_power", DAY_START, DAY_START + timedelta(hours=1)
                )
                == []
            )
    reported = [r for r in caplog.records if "sensor.heatpump_power" in r.getMessage()]
    assert len(reported) == 1, [r.getMessage() for r in reported]
    assert "supplementary data source" in reported[0].getMessage()


def test_each_unreadable_name_is_reported_on_its_own():
    """Reporting once must not mean reporting only the first one."""
    interface = _interface()
    seen = []
    with patch.object(
        LoadInterface, "_LoadInterface__now", return_value=DAY_START
    ), patch(
        "src.interfaces.load_interface.logger.warning",
        side_effect=lambda *a: seen.append(a[1]),
    ):
        for name in ("sensor.one", "sensor.two", "sensor.one"):
            interface.fetch_historical_energy_data(
                name, DAY_START, DAY_START + timedelta(hours=1)
            )
    assert seen == ["sensor.one", "sensor.two"]


# ---------------------------------------------------------------------------
# The second connection: EVCC meters what it meters
# ---------------------------------------------------------------------------

SUPPLEMENTARY = {
    "supplementary_type": "homeassistant",
    "supplementary_url": "http://ha.local:8123",
    "supplementary_access_token": "tok",
}


def _capture():
    """A requests.get stand-in that records where it was pointed."""
    calls = []

    def getter(url, params=None, **kwargs):  # pylint: disable=unused-argument
        calls.append(url)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = []
        return response

    return calls, getter


def test_an_evcc_series_goes_to_evcc():
    calls, getter = _capture()
    interface = _interface(**SUPPLEMENTARY)
    with patch("src.interfaces.load_interface.requests.get", getter), patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        interface.fetch_historical_energy_data(
            "group:consumer:Sauna", DAY_START, DAY_START + timedelta(hours=1)
        )
    assert calls and all("evcc:7070" in url for url in calls), calls


def test_an_entity_id_goes_to_the_second_connection():
    """EVCC cannot answer an appliance it does not meter - but it exists.

    The name is the discriminator, the same rule the battery interface uses for
    its temperature. Without it a pool heat pump that EVCC has no consumer
    series for was simply unreadable, however well Home Assistant knew it.
    """
    calls, getter = _capture()
    interface = _interface(**SUPPLEMENTARY)
    with patch("src.interfaces.load_interface.requests.get", getter), patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        interface.fetch_historical_energy_data(
            "sensor.heatpump_power", DAY_START, DAY_START + timedelta(hours=1)
        )
    assert calls and all("ha.local:8123" in url for url in calls), calls


def test_without_a_second_connection_nothing_is_read_and_it_is_said_once(caplog):
    """No connection is a configuration gap, not a broken name."""
    calls, getter = _capture()
    interface = _interface()  # no supplementary configured
    with patch("src.interfaces.load_interface.requests.get", getter), patch(
        "src.interfaces.load_interface.time.sleep"
    ), caplog.at_level(logging.WARNING, logger="__main__"):
        for _ in range(3):
            assert (
                interface.fetch_historical_energy_data(
                    "sensor.heatpump_power", DAY_START, DAY_START + timedelta(hours=1)
                )
                == []
            )
    assert not calls, "nothing is requested when there is nowhere to ask"
    said = [r for r in caplog.records if "sensor.heatpump_power" in r.getMessage()]
    assert len(said) == 1
    assert "supplementary data source" in said[0].getMessage()


def test_a_non_evcc_source_is_unaffected():
    """One source answers everything; there is nothing to decide."""
    calls, getter = _capture()
    interface = LoadInterface(
        {
            "source": "homeassistant",
            "url": "http://ha.local:8123",
            "access_token": "tok",
            "load_sensor": "sensor.house",
            "max_retries": 1,
            "retry_backoff": 0,
        },
        3600,
        "UTC",
    )
    with patch("src.interfaces.load_interface.requests.get", getter), patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        interface.fetch_historical_energy_data(
            "sensor.house", DAY_START, DAY_START + timedelta(hours=1)
        )
    assert calls and all("ha.local:8123" in url for url in calls), calls
