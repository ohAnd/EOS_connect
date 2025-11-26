from src.interfaces.base_inverter import BaseInverter

import time
import os
import logging
import json
import hashlib
import re
import requests

logger = logging.getLogger("__main__").getChild("FroniusV2")
logger.setLevel(logging.INFO)
logger.info("[InverterV2] Loading Fronius GEN24 V2 with updated authentication")


class FroniusInverterV2(BaseInverter):

    def __init__(self, config):
        """Initialize the Fronius V2 interface with updated authentication."""
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

    def authenticate(self):
        raise NotImplementedError
