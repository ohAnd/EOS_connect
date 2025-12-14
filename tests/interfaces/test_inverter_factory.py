import pytest
import pprint
from src.interfaces.inverters import (
    BaseInverter,
    INVERTER_TYPES,
    LEGACY_INVERTER_TYPES,
    create_inverter,
)


@pytest.fixture
def full_config():
    """
    Minimale realistische Inverter-Config, die in jedem Test kopiert wird.
    """
    return {
        "type": "",
        "address": "192.168.0.10",
        "user": "testuser",
        "password": "pw",
        "max_grid_charge_rate": 3000,
        "max_pv_charge_rate": 4000,
    }


@pytest.mark.parametrize(
    "key,cls", list(INVERTER_TYPES.items()) + list(LEGACY_INVERTER_TYPES.items())
)
def test_factory_creates_registered_inverters(full_config, key, cls):
    """
    Testet alle eingetragenen Inverter – automatisch!
    Kein Vergessen von neuen Inverter-Versionen möglich.
    """
    full_config["type"] = key
    inverter = create_inverter(full_config)

    pprint.pprint(inverter.config)

    assert isinstance(inverter, cls)
    assert isinstance(inverter, BaseInverter)
    assert inverter.config["address"] == "192.168.0.10"


def test_factory_unknown_inverter_raises(full_config):
    full_config["type"] = "irgendwas"

    with pytest.raises(ValueError):
        create_inverter(full_config)
