"""Victron inverter interface module.

Provides the VictronInverter class which implements the BaseInverter
interface for Victron devices (Modbus/TCP client integration).
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Union, Sequence, Any
import struct
import math

from ..inverter_base import BaseInverter  # pylint: disable=relative-beyond-top-level

from ..inverters.ccgx_registers import Reg, REGISTERS, RegisterDef


logger = logging.getLogger("__main__").getChild("VictronModbus")
logger.setLevel(logging.INFO)
logger.info("[Inverter] Loading Victron Inverter")

try:
    import pymodbus
    from pymodbus.client import ModbusTcpClient

    logger.info("[Inverter] pymodbus imported successfully")
except ImportError as e:
    logger.warning("[Inverter] pymodbus import failed: %s", e)


class VictronInverter(BaseInverter):
    """Victron inverter interface implementation."""

    supports_extended_monitoring_default = False

    def __init__(self, config):
        """Initialize the Victron inverter interface."""
        super().__init__(config)

        # --- Configuration values ---

        self.initialize()

    def initialize(self):
        self.address = self.config["address"]
        self.port = 502
        self.unit_id = 100

        self.client = ModbusTcpClient(self.address, port=self.port)
        self.connect_inverter()

    def set_mode_avoid_discharge(self):
        """Set the inverter to avoid discharging the battery."""
        logger.info("[VictronModbus] Setting hold mode, avoid discharge")

        # Hold mode = disable discharge (0W), allow PV charging only
        # ESS max discharge current (fractional)
        # Modbus Adress 2702
        # ESS Mode 2 - Max discharge current for ESS control-loop. The control-loop will use this value to limit the multi power setpoint.
        # Currently a value < 50% will disable discharge completely. >=50% allows. Consider using 2704 instead.

        # ESS max charge current (fractional)
        # Modbus Adress 2703
        # ESS Mode 3 - Max charge current for ESS control-loop. The control-loop will use this value to limit the multi power setpoint.
        # Currently a value < 50% will disable charge completely. >=50% allows.
        # self.write_holding_registers(self.unit_id, 2702, 99)
        self.write_register(
            unit=100,
            reg=Reg.SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPERCENTAGE,
            value=49,
        )

    def set_mode_allow_discharge(self):
        """Set the inverter to allo discharging the battery."""
        logger.info("[VictronModbus] Setting discharge mode, allow discharge")

        # Hold mode = disable discharge (0W), allow PV charging only
        # ESS max discharge current (fractional)
        # Modbus Adress 2702
        # ESS Mode 2 - Max discharge current for ESS control-loop. The control-loop will use this value to limit the multi power setpoint.
        # Currently a value < 50% will disable discharge completely. >=50% allows. Consider using 2704 instead.

        # ESS max charge current (fractional)
        # Modbus Adress 2703
        # ESS Mode 3 - Max charge current for ESS control-loop. The control-loop will use this value to limit the multi power setpoint.
        # Currently a value < 50% will disable charge completely. >=50% allows.
        # self.write_holding_registers(self.unit_id, 2702, 99)

        self.write_register(
            unit=100,
            reg=Reg.SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPERCENTAGE,
            value=100,
        )

    def set_allow_grid_charging(self, value: bool):
        """
        Enable or disable grid charging via MultiPlus (ESS).
        value:
        - bool:
            True  -> enable grid charging (default 100%)
            False -> disable grid charging
        """
        if isinstance(value, bool):
            percent = 100 if value else 0
        else:
            raise TypeError("value must be bool")

        logger.info("[VictronModbus] Set allow grid charging")

        self.write_register(
            unit=self.unit_id,
            reg=Reg.SETTINGS_SETTINGS_CGWACS_MAXCHARGEPERCENTAGE,
            value=percent,
        )

    def set_battery_mode(self, mode):
        raise NotImplementedError

    def get_battery_info(self):
        raise NotImplementedError

    def fetch_inverter_data(self):
        """Get inverter data for monitoring (temperatures, fan control, etc.)."""
        logger.info("[VictronModbus] Reading Victron Inverter Values")

        reg = (
            Reg.SYSTEM_DC_BATTERY_SOC
        )  # Beispiel: Name aus dem optimierten File (service+path-basiert)
        rdef = REGISTERS[reg]

        response = self.read_registers(rdef.address, count=rdef.count, unit=100)

        if response.isError():
            logger.error(f"Fehler beim Lesen des Registers {reg.name} @ {rdef.address}")
            value = None
        else:
            # hier passiert: Typ-Decoding + Skalierung
            value = rdef.decode(response.registers)

        logger.info(f"[VictronModbus] {rdef.description}: {value} {rdef.unit}")
        return value

    def set_mode_force_charge(self, charge_power_w):
        """
        Force charging from grid with approx. charge power in W.
        Einfache Variante bei Victron Anlagen: SOC Limit hochsetzen auf den gewünschten Wert... Victron lädt selbständig

        Strategy:
                    1) Enable grid charging (ESS)
                    2) Read current battery voltage
                    3) Convert desired power (W) to DC current (A)
                    4) Apply DVCC max charge current
        """
        # TODO: Unit test fuer set_mode_force_charge erstellen

        if charge_power_w is None:
            raise TypeError("charge_power_w must be a number")

        charge_power_w = float(charge_power_w)

        if charge_power_w <= 0:
            # Disable grid charging + remove DVCC limit (Victron: -1 disables the limit)
            self.set_allow_grid_charging(False)
            self.write_register(
                unit=self.unit_id,
                reg=Reg.SETTINGS_SETTINGS_SYSTEMSETUP_MAXCHARGECURRENT,
                value=-1,  # disables the limit
            )
            logger.info("[VictronModbus] Force grid charge disabled (P<=0).")
            return

        # 1) Allow grid charging via MAXCHARGEPERCENTAGE
        self.set_allow_grid_charging(True)

        # 2) Read battery voltage
        batt_v = self._read_battery_voltage_v()

        # 3) Convert W -> A
        charge_current_a = _power_w_to_target_charge_current_a(charge_power_w, batt_v)

        logger.info(
            "[VictronModbus] Force charge: target %.0f W @ %.2f V -> %d A (set 2705)",
            charge_power_w,
            batt_v,
            charge_current_a,
        )

        # 4) Apply DVCC max charge current (A DC)
        self.write_register(
            unit=self.unit_id,
            reg=Reg.SETTINGS_SETTINGS_SYSTEMSETUP_MAXCHARGECURRENT,
            value=charge_current_a,
        )

    def connect_inverter(self):
        """Connect to Victron Modbus device."""
        if self.client.connect():
            logger.info(
                f"[VictronModbus] Verbunden mit {self.address}:{self.port} "
                f"(Unit {self.unit_id})"
            )
            return True
        else:
            logger.error(
                f"[VictronModbus] Verbindung fehlgeschlagen zu {self.address}:{self.port}"
            )
            return False

    def disconnect_inverter(self):
        self.client.close()
        logger.info("[VictronModbus] Verbindung geschlossen")

    def read_registers(
        self,
        address: Union[int, "Reg", "RegisterDef"],
        count: int = 1,
        unit: int | None = None,
    ):
        """
        Read holding registers (raw). Returns pymodbus response.

        address kann sein:
        - int: direkte Modbus-Adresse
        - Reg: Enum-Key, der in REGISTERS gemappt wird
        - RegisterDef: direkte Register-Definition

        count wird nur genutzt, wenn address=int übergeben wird.
        Bei Reg/RegisterDef wird automatisch regdef.count verwendet.
        """
        slave = int(unit) if unit is not None else int(self.unit_id)

        if not self.client:
            raise RuntimeError("Modbus client not initialized")

        # --- resolve address/count from enum/definition ---
        if isinstance(address, int):
            real_address = int(address)
            real_count = int(count)
            name = str(real_address)

        else:
            # Reg -> RegisterDef
            regdef = (
                REGISTERS[address] if hasattr(address, "name") else address
            )  # Reg oder RegisterDef
            real_address = int(regdef.address)
            real_count = int(regdef.count)
            name = getattr(address, "value", f"addr={real_address}")

        logger.info(
            "[VictronModbus] Reading unit %s address %s count %s",
            slave,
            name,
            real_count,
        )

        return self.client.read_holding_registers(
            address=real_address,
            count=real_count,
            device_id=slave,
        )

    def read_holding_registers(
        self, address: int, count: int = 1, unit: int | None = None
    ):
        """Read holding registers (raw). Returns pymodbus response."""
        slave = int(unit) if unit is not None else int(self.unit_id)

        logger.info(
            "[VictronModbus] Reading unit %s address %s count %s", slave, address, count
        )

        if not self.client:
            raise RuntimeError("Modbus client not initialized")

        return self.client.read_holding_registers(
            address=int(address), count=int(count), device_id=slave
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

    def write_holding_registers_new(self, unit, address, values):
        """Write one or multiple holding registers (Victron Modbus TCP)."""
        slave = int(unit) if unit is not None else 1

        # Für CCGX-Offsets (2600, 2705, 800, ...) ist das bereits korrekt.
        # Falls irgendwann jemand 40001/4xxxx übergibt, macht normalize das richtig:
        reg_address = self._normalize_register_address(address)

        if isinstance(values, (int, float)):
            return self.client.write_register(
                address=reg_address, value=int(values), unit=slave
            )

        if isinstance(values, (list, tuple)):
            return self.client.write_registers(
                address=reg_address,
                values=[int(v) for v in values],
                unit=slave,
            )

        raise TypeError("❌ 'values' must be int, float, list or tuple")

    def write_register(self, unit, reg: Reg, value):
        r = REGISTERS[reg]

        if not r.writable:
            raise ValueError(f"Register {reg.name} is not writable")

        # value ist bereits Rohwert (kein Scaling hier!)
        self.write_holding_registers_new(
            unit=unit,
            address=r.address,
            values=value,
        )

    def _normalize_register_address(self, address: int) -> int:
        """
        Convert Victron Modbus register addresses (e.g. 40001)
        to pymodbus zero-based addresses.
        """
        if address >= 40001:
            return address - 40001
        return address

    def write_reg(self, unit: int | None, reg: Reg, value: Any):
        r = REGISTERS[reg]
        if not r.writable:
            raise ValueError(
                f"Register {reg.name} is marked read-only (writable=False)"
            )

        address = r.address  # <- schon korrekt für CCGX Modbus TCP
        words = _encode_words(r, value)

        # 1 Word => FC06, mehrere => FC10
        if len(words) == 1:
            return self.write_holding_registers(
                unit=unit, address=address, values=words[0]
            )
        return self.write_holding_registers(unit=unit, address=address, values=words)

    def _encode_words(reg: RegisterDef, value: Any) -> list[int]:
        """
        Encode a python value into Modbus register words according to reg.type and reg.scale.
        """
        # inverse scaling (decode() multiplies)
        v = value
        if isinstance(v, (int, float)) and reg.scale not in (0, 1, 1.0):
            v = float(v) / float(reg.scale)

        t = (reg.type or "uint16").strip().lower()

        if t.startswith("string"):
            # simple string packing: UTF-8 bytes, 2 bytes per word, zero padded
            raw = str(v).encode("utf-8")
            raw = raw[: reg.count * 2]
            raw = raw.ljust(reg.count * 2, b"\x00")
            words = [
                int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)
            ]
            return words[: reg.count]

        if t == "uint16":
            return [int(v) & 0xFFFF]

        if t == "int16":
            return [int(v) & 0xFFFF]

        if t == "uint32":
            b = int(v).to_bytes(4, "big", signed=False)
            words = [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]
            return words

        if t == "int32":
            b = int(v).to_bytes(4, "big", signed=True)
            words = [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]
            return words

        if t == "float32":
            b = struct.pack(">f", float(v))
            words = [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]
            return words

        if t == "uint64":
            b = int(v).to_bytes(8, "big", signed=False)
            words = [int.from_bytes(b[i : i + 2], "big") for i in range(0, 8, 2)]
            return words

        if t == "int64":
            b = int(v).to_bytes(8, "big", signed=True)
            words = [int.from_bytes(b[i : i + 2], "big") for i in range(0, 8, 2)]
            return words

        if t == "float64":
            b = struct.pack(">d", float(v))
            words = [int.from_bytes(b[i : i + 2], "big") for i in range(0, 8, 2)]
            return words

        # fallback
        return [int(v) & 0xFFFF]


def api_set_max_grid_charge_rate(self, max_grid_charge_rate: int):
    """Set the maximum power in W that can be used to load the battery from the grid."""
    if max_grid_charge_rate < 0:
        logger.warning(
            "[Inverter] API: Invalid max_grid_charge_rate %sW", max_grid_charge_rate
        )
        return
    logger.info(
        "[Inverter] API: Setting max_grid_charge_rate: %.1fW", max_grid_charge_rate
    )
    self.max_grid_charge_rate = max_grid_charge_rate


def api_set_max_pv_charge_rate(self, max_pv_charge_rate: int):
    """Set the maximum power in W that can be used to load the battery from the PV."""
    if max_pv_charge_rate < 0:
        logger.warning(
            "[Inverter] API: Invalid max_pv_charge_rate %s", max_pv_charge_rate
        )
        return
    logger.info("[Inverter] API: Setting max_pv_charge_rate: %.1fW", max_pv_charge_rate)
    self.max_pv_charge_rate = max_pv_charge_rate


def _read_battery_voltage_v(self) -> float:
    """Read current system battery voltage in V."""
    v_reg = Reg.SYSTEM_DC_BATTERY_VOLTAGE
    vdef = REGISTERS[v_reg]
    v_resp = self.read_registers(vdef.address, count=vdef.count, unit=self.unit_id)

    if v_resp.isError() or not getattr(v_resp, "registers", None):
        raise RuntimeError("Failed to read battery voltage for W->A conversion")

    batt_v = float(vdef.decode(v_resp.registers))
    if batt_v <= 1.0:
        raise RuntimeError(f"Battery voltage looks invalid: {batt_v} V")

    return batt_v


def _power_w_to_target_charge_current_a(self, power_w: float) -> int:
    """Convert power in W to target current in A using current battery voltage."""
    if power_w is None:
        raise TypeError("power_w must be a number")

    power_w = float(power_w)
    if power_w <= 0:
        return 0

    batt_v = self._read_battery_voltage_v()
    target_a = int(math.ceil(power_w / batt_v))
    return max(0, min(target_a, 32767))
