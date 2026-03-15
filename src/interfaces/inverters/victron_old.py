""" """

import logging
import hashlib

# pylint: disable=duplicate-code

logger = logging.getLogger("__main__").getChild("VictronModbus")
logger.setLevel(logging.INFO)
logger.info("[Inverter] Loading Victron Inverter")


try:
    import pymodbus
    from pymodbus.client import ModbusTcpClient

    logger.info("pymodbus Import erfolgreich!")
except ImportError as e:
    logger.info("pymodbus Import fehlgeschlagen:", e)


def hash_utf8_md5(x):
    """Hash a string or bytes object with MD5 (legacy support)."""
    if isinstance(x, str):
        x = x.encode("utf-8")
    return hashlib.md5(x).hexdigest()


def hash_utf8_sha256(x):
    """Hash a string or bytes object with SHA256 (new firmware)."""
    if isinstance(x, str):
        x = x.encode("utf-8")
    return hashlib.sha256(x).hexdigest()


def strip_dict(original):
    """Strip all keys starting with '_' from a dictionary."""
    if not isinstance(original, dict):
        return original
    stripped_copy = {}
    for key in original.keys():
        if not key.startswith("_"):
            stripped_copy[key] = original[key]
    return stripped_copy


class VictronModbus:
    """
    Universelle Modbus-Schnittstelle für Victron MultiPlus / Quattro Geräte.
    - Kommunikation über Modbus TCP
    - Registeradressen kannst du selbst anpassen
    - Enthält Basis-Methoden für Status, SOC, Lade-/Entlade-Steuerung usw.
    """

    def __init__(self, config):
        """
        Beispiel-Konfig:
        config = {
            "host": "192.168.1.100",
            "port": 502,
            "unit_id": 100,
            "max_grid_charge_rate": 3000,
            "min_soc": 20,
            "max_soc": 100,
        }
        """

        logger.info(f"[VictronModbus] PyModbus Version: {pymodbus.__version__}")

        self.host = config.get("host", "192.168.178.75")
        self.port = config.get("port", 502)
        self.unit_id = config.get("unit_id", 100)

        self.max_grid_charge_rate = config.get("max_grid_charge_rate", 1500)
        self.max_pv_charge_rate = config.get("max_pv_charge_rate", 8000)
        self.min_soc = config.get("min_soc", 20)
        self.max_soc = config.get("max_soc", 100)

        self.client = ModbusTcpClient(self.host, port=self.port)
        if self.client.connect():
            logger.info(
                f"[VictronModbus] Verbunden mit {self.host}:{self.port} (Unit {self.unit_id})"
            )
        else:
            logger.error(
                f"[VictronModbus] Verbindung fehlgeschlagen zu {self.host}:{self.port}"
            )

    # -----------------------------------------------------
    # Grundfunktionen
    # -----------------------------------------------------

    # Battery mode control methods (same interface as evcc)

    def set_battery_mode(self, mode):
        """
        Set battery mode (evcc-compatible).

        Args:
            mode (str): "normal", "hold", "charge"

        Returns:
            bool: True if successful
        """
        logger.info(f"[VictronModbus] Setting battery mode: {mode}")

        if mode == "normal":
            return self._set_mode_normal()
        if mode == "hold":
            return self._set_mode_hold()
        if mode == "charge":
            return self._set_mode_charge()
        logger.error(f"[VictronModbus] Invalid mode: {mode}")
        return False

    def _set_mode_normal(self):
        """Set normal battery operation (allow discharge)."""
        logger.info("[VictronModbus] Setting normal mode")
        # Baterieentladun zulassen

    def _set_mode_hold(self):
        """Set hold mode (avoid discharge)."""
        logger.info("[VictronModbus] Setting hold mode")

        # Hold mode = disable discharge (0W), allow PV charging only
        # ESS max discharge current (fractional)
        # Modbus Adress 2702
        # ESS Mode 2 - Max discharge current for ESS control-loop. The control-loop will
        # use this value to limit the multi power setpoint.
        # Currently a value < 50% will disable discharge completely. >=50% allows.
        # Consider using 2704 instead.

        # ESS max charge current (fractional)
        # Modbus Adress 2703
        # ESS Mode 3 - Max charge current for ESS control-loop. The control-loop will
        # use this value to limit the multi power setpoint.
        # Currently a value < 50% will disable charge completely. >=50% allows.
        self.write_holding_registers(self.unit_id, 2702, 99)

    # EOS Connect compatibility layer

    def set_mode_force_charge(self, charge_power_w):
        """EOS Connect compatibility: Force charge mode with specific power."""
        logger.info(f"[VictronModbus] Setting force charge mode with {charge_power_w}W")

    def _set_mode_charge(self):
        """Set charge mode (force charge from grid)."""
        logger.info("[VictronModbus] Setting charge mode")

    def set_mode_avoid_discharge(self):
        """EOS Connect compatibility: Avoid discharge mode."""
        return self.set_battery_mode("hold")

    def set_mode_allow_discharge(self):
        """EOS Connect compatibility: Allow discharge mode."""
        return self.set_battery_mode("normal")

    def close(self):
        """Close the Modbus TCP client connection."""
        self.client.close()
        logger.info("[VictronModbus] Verbindung geschlossen")

    def read_holding_registers(self, unit, address, count):
        """Read holding registers."""
        slave = int(unit) if unit else 1
        logger.info(
            "[VictronModbus]Reading unit %s address %s count %s", unit, address, count
        )
        return self.client.read_holding_registers(
            address=address, count=count, device_id=slave
        )

    def write_holding_registers(self, unit, address, values):
        """Write one or multiple holding registers."""
        slave = int(unit) if unit else 1
        logger.info(
            "[VictronModbus] Writing unit %s address %s values %s",
            unit,
            address,
            values,
        )

        # Einzelwert schreiben (Function Code 0x06)
        if isinstance(values, (int, float)):
            return self.client.write_register(
                address=address, value=int(values), device_id=slave
            )

        # Mehrere Werte schreiben (Function Code 0x10)
        elif isinstance(values, (list, tuple)):
            return self.client.write_registers(
                address=address, values=[int(v) for v in values], device_id=slave
            )

        else:
            raise TypeError("❌ 'values' must be int, float, list or tuple")

    def fetch_inverter_data(self):
        """Get inverter data for monitoring (temperatures, fan control, etc.)."""
        logger.info("[VictronModbus]Reading Victron Inverter Values")
        response = self.read_holding_registers(unit=100, address=843, count=1)

        if response.isError():
            logger.error("Fehler beim Lesen des SOC Registers")
            soc_value = None
        else:
            # 'registers' liefert eine Liste, count=1 -> ersten Wert nehmen
            soc_value = response.registers[0]

        logger.info(f"[VictronModbus] SOC Wert der Batterie: {soc_value}")

    def api_set_max_grid_charge_rate(self, max_grid_charge_rate: int):
        """Set the maximum power in W that can be used to charge the battery from grid.

        Args:
            max_grid_charge_rate: Maximum grid charge power in watts
        """
        if max_grid_charge_rate < 0:
            logger.warning(
                f"[VictronModbus] API: Invalid max_grid_charge_rate {max_grid_charge_rate}W"
            )
            return

        logger.info(
            f"[VictronModbus] API: Setting max_grid_charge_rate: {max_grid_charge_rate}W"
        )
        self.max_grid_charge_rate = max_grid_charge_rate

    def api_set_max_pv_charge_rate(self, max_pv_charge_rate: int):
        """Set the maximum power in W that can be used to charge the battery from PV.

        Args:
            max_pv_charge_rate: Maximum PV charge power in watts
        """
        if max_pv_charge_rate < 0:
            logger.warning(
                f"[InverterV2] API: Invalid max_pv_charge_rate {max_pv_charge_rate}W"
            )
            return

        logger.info(
            f"[InverterV2] API: Setting max_pv_charge_rate: {max_pv_charge_rate}W"
        )
        self.max_pv_charge_rate = max_pv_charge_rate
