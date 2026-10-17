"""
SOC reads from EVCC's live state as percent.

EVCC reports ``battery.soc`` in percent, weighted across battery meters - there
is no decimal (0-1) format to guess, so the EVCC branch pins the reading
directly instead of running the auto-detection the Home Assistant path needs.
"""

# pylint: disable=protected-access
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
