import logging

from src.interfaces.base_inverter import BaseInverter


logger = logging.getLogger("__main__").getChild("VictronModbus")
logger.setLevel(logging.INFO)
logger.info("[Inverter] Loading Victron Inverter")

try:
    import pymodbus
    from pymodbus.client import ModbusTcpClient

    logger.info("pymodbus Import erfolgreich!")
except ImportError as e:
    logger.info("pymodbus Import fehlgeschlagen:", e)


class VictronInverter(BaseInverter):

    def __init__(self, config):

        # Ruft den Konstruktor der Basisklasse auf
        super().__init__(config)

    def initialize(self):
        raise NotImplementedError

    def set_mode_avoid_discharge(self):
        raise NotImplementedError

    def set_mode_allow_discharge(self):
        raise NotImplementedError

    def set_allow_grid_charging(self, value):
        raise NotImplementedError

    def set_battery_mode(self, mode):
        raise NotImplementedError

    def get_battery_info(self):
        raise NotImplementedError

    def fetch_inverter_data(self):
        raise NotImplementedError

    def set_mode_force_charge(self, charge_power_w):
        raise NotImplementedError

    def connect_inverter(self):
        raise NotImplementedError

    def disconnect_inverter(self):
        raise NotImplementedError
