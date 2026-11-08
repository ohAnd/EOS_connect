"""
SOC reads from EVCC's live state as percent.

EVCC reports ``battery.soc`` in percent, weighted across battery meters - there
is no decimal (0-1) format to guess, so the EVCC branch pins the reading
directly instead of running the auto-detection the Home Assistant path needs.
"""

# pylint: disable=protected-access
import logging

import pytest
import requests

from unittest.mock import MagicMock, patch

from src.interfaces.battery_interface import BatteryInterface

EVCC_CONFIG = {
    "source": "evcc",
    "url": "http://evcc:7070",
    "soc_sensor": "battery.soc",
    "capacity_wh": 11000,
    "max_charge_power_w": 5000,
}


def _interface(**overrides):
    config = {**EVCC_CONFIG, **overrides}
    interface = BatteryInterface.__new__(BatteryInterface)
    BatteryInterface.__init__(
        interface,
        config,
        on_bat_max_changed=None,
        load_interface=None,
        timezone=None,
        base_control=None,
    )
    # The constructor starts a poll thread; stop it so a test owns the timing.
    interface.shutdown()
    return interface


def _state(fake):
    with patch("src.interfaces.battery_interface.fetch_remote_state", fake):
        return interface_soc()


def interface_soc():
    raise NotImplementedError  # replaced below


def _soc(fake, interface):
    with patch("src.interfaces.battery_interface.fetch_remote_state", fake) as reader:
        reader.side_effect = lambda source, sensor, **kwargs: fake
        return interface._BatteryInterface__fetch_soc_data_unified()


def _fake_state(value):
    return str(value)


def test_soc_from_evcc_is_percent():
    interface = _interface()
    with patch(
        "src.interfaces.battery_interface.fetch_remote_state",
        return_value=_fake_state(74),
    ):
        soc = interface._BatteryInterface__fetch_soc_data_unified()
    assert soc == 74.0


def test_soc_fifty_percent_is_not_scaled():
    """A 50% reading must not be read as decimal 0.5 and doubled, nor 0.5%."""
    interface = _interface()
    with patch(
        "src.interfaces.battery_interface.fetch_remote_state",
        return_value=_fake_state(50),
    ):
        soc = interface._BatteryInterface__fetch_soc_data_unified()
    assert soc == 50.0


def test_low_soc_is_not_doubled():
    """10% must stay 10%: the EVCC branch skips the <=1.0 auto-detect branch."""
    interface = _interface()
    with patch(
        "src.interfaces.battery_interface.fetch_remote_state",
        return_value=_fake_state(10),
    ):
        soc = interface._BatteryInterface__fetch_soc_data_unified()
    assert soc == 10.0


def test_soc_config_check_accepts_evcc():
    interface = _interface()
    assert interface.configuration_valid
    assert interface.soc_source_usable


def test_soc_config_requires_url():
    """The EVCC placeholder is not a configured server."""
    interface = _interface(url="http:// yourEVCCserver".replace(" ", ""))
    assert not interface.soc_source_usable


class TestWhichConnectionAnswersEachSensor:
    """
    EVCC answers what it meters; everything else needs the second connection.

    This used to be decided for the battery temperature alone, with a
    hand-written ``startswith("sensor.")``. The SOC and the €/Wh price sensor
    went to EVCC whatever they were named - and EVCC does not fail on a name it
    does not know, it resolves it to nothing. An entity id there read as an
    empty string, not as an error anyone could act on.
    """

    SUPPLEMENTED = {
        "supplementary_type": "homeassistant",
        "supplementary_url": "http://ha.local:8123",
        "supplementary_access_token": "supp-token",
        "supplementary_ssl_ignore": True,
    }

    def _read(self, interface, sensor):
        """Return the (source, url, token) the interface would read *sensor* with."""
        seen = {}

        def record(source, name, **kwargs):
            seen.update(source=source, sensor=name, **kwargs)
            return "42"

        with patch("src.interfaces.battery_interface.fetch_remote_state", record):
            interface._BatteryInterface__fetch_remote_state(sensor)
        return seen

    def test_evccs_own_state_keys_stay_with_evcc(self):
        interface = _interface(**self.SUPPLEMENTED)
        seen = self._read(interface, "battery.soc")
        assert seen["source"] == "evcc"
        assert seen["url"] == "http://evcc:7070"

    def test_an_entity_id_goes_to_the_supplementary_connection(self):
        """The battery temperature is the standing case."""
        interface = _interface(
            sensor_battery_temperature="sensor.akku_temp", **self.SUPPLEMENTED
        )
        seen = self._read(interface, "sensor.akku_temp")
        assert seen["source"] == "homeassistant"
        assert seen["url"] == "http://ha.local:8123"
        assert seen["access_token"] == "supp-token"
        assert seen["ssl_ignore"] is True

    def test_the_price_sensor_is_routed_too(self):
        """Not only the temperature - this one used to be asked of EVCC."""
        interface = _interface(
            price_euro_per_wh_sensor="sensor.akku_preis", **self.SUPPLEMENTED
        )
        seen = self._read(interface, "sensor.akku_preis")
        assert seen["source"] == "homeassistant"
        assert seen["url"] == "http://ha.local:8123"

    def test_an_soc_entity_is_routed_too(self):
        """An expert can point the SOC at a Home Assistant entity under EVCC."""
        interface = _interface(soc_sensor="sensor.akku_soc", **self.SUPPLEMENTED)
        seen = self._read(interface, "sensor.akku_soc")
        assert seen["source"] == "homeassistant"

    def test_without_a_supplementary_the_message_says_what_to_do(self):
        """
        A ValueError, not an empty reading - the caller's failure handling
        already knows what to do with one, and the text names the fix.
        """
        interface = _interface(sensor_battery_temperature="sensor.akku_temp")

        with pytest.raises(ValueError) as excinfo:
            interface._BatteryInterface__fetch_remote_state("sensor.akku_temp")

        message = str(excinfo.value)
        assert "sensor.akku_temp" in message
        assert "supplementary" in message

    def test_a_home_assistant_source_routes_nothing(self):
        """Nothing to decide when one connection answers everything."""
        interface = _interface(
            source="homeassistant", url="http://ha:8123", access_token="tok"
        )
        seen = self._read(interface, "sensor.anything")
        assert seen["source"] == "homeassistant"
        assert seen["url"] == "http://ha:8123"
        assert seen["access_token"] == "tok"


class TestAnUnreadableSensorIsSaidOnce:
    """
    A missing connection is a setting, not a passing failure.

    All three readers ran it through their retry counters, so a measured run
    produced this every thirty seconds, forever:

        Temperature sensor unavailable (5 failures). …
        Temperature sensor unavailable (6 failures). …
        Temperature sensor unavailable (7 failures). …

    - which never says that the fix is a supplementary data source. That
    sentence existed only in the debug lines before it, where nobody running on
    INFO ever sees it.
    """

    def _interface_without_supplementary(self, **overrides):
        return _interface(sensor_battery_temperature="sensor.akku_temp", **overrides)

    @staticmethod
    def _own(caplog, needle):
        """Only the lines about *needle* - the poll thread logs its own."""
        return [r.getMessage() for r in caplog.records if needle in r.getMessage()]

    def test_the_warning_names_the_fix(self, caplog):
        interface = self._interface_without_supplementary()

        with caplog.at_level(logging.WARNING, logger="__main__"):
            interface._BatteryInterface__battery_request_current_temp()

        warnings = self._own(caplog, "sensor.akku_temp")
        assert len(warnings) == 1, warnings
        assert "supplementary" in warnings[0]
        assert "ACTION REQUIRED" in warnings[0]

    def test_it_is_not_repeated_on_every_cycle(self, caplog):
        interface = self._interface_without_supplementary()

        with caplog.at_level(logging.WARNING, logger="__main__"):
            for _ in range(12):
                interface._BatteryInterface__battery_request_current_temp()

        assert len(self._own(caplog, "sensor.akku_temp")) == 1

    def test_the_retry_budget_is_not_spent_on_it(self):
        """
        Five attempts cannot fix a setting. The counter staying at zero is what
        keeps a real outage - later, on the same sensor - reported honestly.
        """
        interface = self._interface_without_supplementary()

        for _ in range(8):
            interface._BatteryInterface__battery_request_current_temp()

        assert interface.temp_fail_count == 0

    def test_temperature_compensation_is_simply_off(self):
        interface = self._interface_without_supplementary()
        assert interface._BatteryInterface__battery_request_current_temp() is None

    def test_a_real_outage_still_escalates(self):
        """The other failures are unchanged - they may succeed next time."""
        interface = _interface(
            sensor_battery_temperature="battery.temp",
            **TestWhichConnectionAnswersEachSensor.SUPPLEMENTED,
        )

        interface.temp_fail_count = 0  # the constructor's own cycle already ran
        with patch(
            "src.interfaces.battery_interface.fetch_remote_state",
            side_effect=requests.exceptions.ConnectionError("down"),
        ):
            for _ in range(3):
                interface._BatteryInterface__battery_request_current_temp()

        assert interface.temp_fail_count == 3

    def test_the_soc_reader_says_it_once_too(self, caplog):
        interface = _interface(soc_sensor="sensor.akku_soc")

        with caplog.at_level(logging.WARNING, logger="__main__"):
            for _ in range(6):
                interface._BatteryInterface__fetch_soc_data_unified()

        warnings = self._own(caplog, "sensor.akku_soc")
        assert len(warnings) == 1, warnings
        assert interface.soc_fail_count == 0

    def test_each_sensor_gets_its_own_sentence(self, caplog):
        """Once per name, not once per interface - two problems, two messages."""
        interface = _interface(
            soc_sensor="sensor.akku_soc", sensor_battery_temperature="sensor.akku_temp"
        )

        with caplog.at_level(logging.WARNING, logger="__main__"):
            for _ in range(4):
                interface._BatteryInterface__fetch_soc_data_unified()
                interface._BatteryInterface__battery_request_current_temp()

        assert len(self._own(caplog, "sensor.akku_soc")) == 1
        assert len(self._own(caplog, "sensor.akku_temp")) == 1
