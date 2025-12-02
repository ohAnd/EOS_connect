"""
Unit tests for the BatteryInterface class in src.interfaces.battery_interface.

This module contains tests for initialization, SOC fetching, error handling,
and control methods of the BatteryInterface.
"""

from unittest.mock import patch, MagicMock
import pytest
import requests
from src.interfaces.battery_interface import BatteryInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


@pytest.fixture
def default_config():
    """
    Returns a default configuration dictionary for BatteryInterface.
    """
    return {
        "source": "default",
        "url": "",
        "soc_sensor": "",
        "max_charge_power_w": 3000,
        "capacity_wh": 10000,
        "min_soc_percentage": 10,
        "max_soc_percentage": 90,
        "charging_curve_enabled": True,
        "discharge_efficiency": 1.0,
        "price_euro_per_wh_accu": 0.0,
        "price_euro_per_wh_source": "config",
        "price_euro_per_wh_sensor": "",
    }


def test_init_sets_attributes(default_config):
    """
    Test that BatteryInterface initialization sets attributes correctly.
    """
    bi = BatteryInterface(default_config)
    assert bi.src == "default"
    assert bi.max_charge_power_fix == 3000
    assert bi.min_soc_set == 10
    assert bi.max_soc_set == 90


def test_default_source_sets_soc_to_5(default_config):
    """
    Test that the default source sets SOC to 5.
    """
    bi = BatteryInterface(default_config)
    soc = bi._BatteryInterface__battery_request_current_soc()
    assert soc == 5


def test_openhab_fetch_success(default_config):
    """
    Test successful SOC fetch from OpenHAB.
    """
    test_config = default_config.copy()
    test_config["source"] = "openhab"
    test_config["url"] = "http://fake"
    test_config["soc_sensor"] = "BatterySOC"
    bi = BatteryInterface(test_config)
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "80"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        soc = bi._BatteryInterface__fetch_soc_data_from_openhab()
        assert soc == 80


def test_openhab_fetch_decimal_format(default_config):
    """
    Test SOC fetch from OpenHAB with decimal format.
    """
    test_config = default_config.copy()
    test_config["source"] = "openhab"
    test_config["url"] = "http://fake"
    test_config["soc_sensor"] = "BatterySOC"
    bi = BatteryInterface(test_config)
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.75"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        soc = bi._BatteryInterface__fetch_soc_data_from_openhab()
        assert soc == 75.0


def test_homeassistant_fetch_success(default_config):
    """
    Test successful SOC fetch from Home Assistant.
    """
    test_config = default_config.copy()
    test_config["source"] = "homeassistant"
    test_config["url"] = "http://fake"
    test_config["soc_sensor"] = "sensor.battery_soc"
    test_config["access_token"] = "token"
    bi = BatteryInterface(test_config)
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "55"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        soc = bi._BatteryInterface__fetch_soc_data_from_homeassistant()
        assert soc == 55.0


def test_homeassistant_price_sensor_success(default_config):
    """
    Ensure the Home Assistant price sensor value is fetched and stored.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "access_token": "token",
            "price_euro_per_wh_source": "homeassistant",
            "price_euro_per_wh_sensor": "sensor.accu_price",
        }
    )
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.002"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        bi = BatteryInterface(test_config)
        # Ensure manual update works and the getter reflects the sensor value
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.002)
        bi.shutdown()


def test_homeassistant_price_sensor_failure_keeps_last_value(default_config):
    """
    Ensure failing sensor updates keep the last configured price.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "access_token": "token",
            "price_euro_per_wh_source": "homeassistant",
            "price_euro_per_wh_sensor": "sensor.accu_price",
            "price_euro_per_wh_accu": 0.001,
        }
    )
    with patch(
        "src.interfaces.battery_interface.requests.get",
        side_effect=requests.exceptions.RequestException("boom"),
    ):
        bi = BatteryInterface(test_config)
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.001)
        bi.shutdown()


def test_openhab_price_sensor_success(default_config):
    """
    Ensure the OpenHAB price item value is fetched and stored.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "price_euro_per_wh_source": "openhab",
            "price_euro_per_wh_sensor": "BatteryPrice",
        }
    )
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.00015"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        bi = BatteryInterface(test_config)
        # Ensure manual update works and the getter reflects the item value
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.00015)
        bi.shutdown()


def test_openhab_price_sensor_with_unit_success(default_config):
    """
    Ensure OpenHAB price item with unit (e.g., "0.00015 €/Wh") is parsed correctly.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "price_euro_per_wh_source": "openhab",
            "price_euro_per_wh_sensor": "BatteryPrice",
        }
    )
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.00015 €/Wh"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        bi = BatteryInterface(test_config)
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.00015)
        bi.shutdown()


def test_openhab_price_sensor_failure_keeps_last_value(default_config):
    """
    Ensure failing OpenHAB item updates keep the last configured price.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "price_euro_per_wh_source": "openhab",
            "price_euro_per_wh_sensor": "BatteryPrice",
            "price_euro_per_wh_accu": 0.0001,
        }
    )
    with patch(
        "src.interfaces.battery_interface.requests.get",
        side_effect=requests.exceptions.RequestException("boom"),
    ):
        bi = BatteryInterface(test_config)
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.0001)
        bi.shutdown()


def test_soc_error_handling(default_config):
    """
    Test SOC error handling and fail count reset.
    """
    bi = BatteryInterface(default_config)
    # Simulate 5 consecutive failures
    for _ in range(5):
        result = bi._handle_soc_error("openhab", "fail", 42)
    assert result == 5
    assert bi.soc_fail_count == 0


def test_set_min_soc_and_max_soc(default_config):
    """
    Test setting minimum and maximum SOC values.
    """
    bi = BatteryInterface(default_config)
    bi.set_min_soc(5)
    assert bi.min_soc_set == 10  # Should be set to configured min
    bi.set_min_soc(95)
    assert bi.min_soc_set == 89  # Should be set to max_soc - 1
    bi.set_max_soc(5)
    assert bi.max_soc_set == 90  # Should be set to configured max
    bi.set_max_soc(95)
    assert bi.max_soc_set == 90  # Should be set to configured max


def test_get_max_charge_power_dyn(default_config):
    """
    Test dynamic calculation of max charge power.
    """
    bi = BatteryInterface(default_config)
    bi.current_soc = 20
    bi._BatteryInterface__get_max_charge_power_dyn()
    assert bi.max_charge_power_dyn > 0
    bi.current_soc = 100
    bi._BatteryInterface__get_max_charge_power_dyn()
    assert bi.max_charge_power_dyn > 0


def test_shutdown_stops_thread(default_config):
    """
    Test that shutdown stops the update thread.
    """
    bi = BatteryInterface(default_config)
    bi.shutdown()
    assert not bi._update_thread.is_alive()
