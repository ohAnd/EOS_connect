"""Victron inverter interface module.

Provides the VictronInverter class which implements the BaseInverter
interface for Victron devices (Modbus/TCP client integration).
"""

import logging
from typing import Union, Any
import struct
import math
import time
import threading

from ..inverter_base import BaseInverter  # pylint: disable=relative-beyond-top-level

from ..inverters.ccgx_registers_all import Reg, REGISTERS, RegisterDef


logger = logging.getLogger("__main__").getChild("VictronModbus")
logger.setLevel(logging.INFO)
logger.info("[Inverter] Loading Victron Inverter")

try:
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

        self._vebus_keepalive_thread: threading.Thread | None = None
        self._vebus_keepalive_stop = threading.Event()
        self._vebus_keepalive_lock = threading.Lock()

        # welche Regs zyklisch geschrieben werden + ihre aktuellen Zielwerte (W)
        self._vebus_targets: dict[Reg, int] = {}
        self._vebus_interval_s: float = 1.0  # 500ms

    def start_vebus_keepalive(
        self,
        targets: dict[Reg, int],
        interval_s: float = 0.5,
        unit: int = 227,
    ):
        """
        Startet einen Thread, der die angegebenen VE.Bus Setpoints zyklisch schreibt.
        targets: z.B. {Reg.VEBUS_Hub4_L1_AcPowerSetpoint_37: 0, ...}
        """
        self._vebus_interval_s = float(interval_s)

        with self._vebus_keepalive_lock:
            self._vebus_targets = {k: int(v) for k, v in targets.items()}

        if self._vebus_keepalive_thread and self._vebus_keepalive_thread.is_alive():
            logger.info(
                "[VictronModbus] VE.Bus keepalive already running - targets updated"
            )
            return

        self._vebus_keepalive_stop.clear()

        def _loop():
            while not self._vebus_keepalive_stop.is_set():
                with self._vebus_keepalive_lock:
                    snapshot = dict(self._vebus_targets)

                for reg, watts in snapshot.items():
                    try:
                        rdef = REGISTERS[reg]
                        # Wichtig: kein verify, nur write (Keepalive)
                        self.write_holding_registers(
                            unit=unit,
                            address=rdef.address,
                            values=int(watts),
                        )

                    except Exception as e:
                        logger.warning(
                            "[VictronModbus] VE.Bus keepalive write failed for %s: %s",
                            getattr(reg, "name", str(reg)),
                            e,
                        )

                time.sleep(self._vebus_interval_s)

        self._vebus_keepalive_thread = threading.Thread(target=_loop, daemon=True)
        self._vebus_keepalive_thread.start()

        logger.info(
            "[VictronModbus] VE.Bus keepalive started (%d regs, %.2fs interval)",
            len(targets),
            self._vebus_interval_s,
        )

    def update_vebus_keepalive_targets(self, targets: dict[Reg, int]):
        """Aktualisiert die Zielwerte, Thread läuft weiter."""
        with self._vebus_keepalive_lock:
            for reg, watts in targets.items():
                self._vebus_targets[reg] = int(watts)

    def stop_vebus_keepalive(self, unit: int = 227, write_zero: bool = True):
        """Stoppt Keepalive-Thread und schreibt optional 0W auf alle Targets (Fail-safe)."""
        self._vebus_keepalive_stop.set()
        if self._vebus_keepalive_thread:
            self._vebus_keepalive_thread.join(timeout=2.0)

        if write_zero:
            with self._vebus_keepalive_lock:
                regs = list(self._vebus_targets.keys())

            for reg in regs:
                try:
                    rdef = REGISTERS[reg]
                    self.write_holding_registers(
                        unit=unit, address=rdef.address, values=0
                    )
                except Exception as e:
                    logger.warning(
                        "[VictronModbus] Failed to write 0W for %s: %s", reg.name, e
                    )

        logger.info("[VictronModbus] VE.Bus keepalive stopped")

    def initialize(self):
        self.address = self.config["address"]
        self.port = 502
        self.unit_id = 100

        self.client = ModbusTcpClient(self.address, port=self.port)
        self.connect_inverter()

    def set_mode_avoid_discharge(self):
        """Set the inverter to avoid discharging the battery."""
        logger.info("[VictronModbus] Setting hold mode, avoid discharge")

        # Also wir stellen auf ESS Externe Regelung ES Mode auf 3und setzen dann ID 37,41 und 42 auf 0W -> es werden 0 W aus oder in richtung Grid geschoben kein Laden kein Entladen

        # ESS Mode external controll
        reg = Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        target_value = 3
        rdef = REGISTERS[reg]

        logger.info(
            "[VictronModbus] Setting ESS Mode to External Control: write %s=%d @ %d (%s)",
            reg.name,
            target_value,
            rdef.address,
            rdef.description,
        )

        self.write_register_verified(reg, target_value, retries=8, delay_s=0.2)

        # 2) VE.Bus Setpoints zyklisch halten (unit 227)
        targets = {
            Reg.VEBUS_Hub4_L1_AcPowerSetpoint_37: 0,
            Reg.VEBUS_Hub4_L2_AcPowerSetpoint_40: 0,
            Reg.VEBUS_Hub4_L3_AcPowerSetpoint_41: 0,
        }

        # optional: einmalig initial schreiben (kurz)
        for r, v in targets.items():
            rdef = REGISTERS[r]
            logger.info(
                "[VictronModbus] VE.Bus ID %d (%s) -> %dW",
                rdef.address,
                r.name,
                v,
            )
            self.write_holding_registers(unit=227, address=rdef.address, values=v)

        # dann Keepalive starten
        self.start_vebus_keepalive(targets, interval_s=1.0, unit=227)

        logger.info(
            "[VictronModbus] Hold mode active (setpoints kept at 0W on L1/L2/L3)"
        )

        logger.info(
            "[VictronModbus] Hold mode active (discharge disabled, %s=%s)",
            reg.name,
            target_value,
        )

    def set_mode_allow_discharge(self):
        """Set the inverter to allow discharging the battery."""
        logger.info("[VictronModbus] Setting discharge mode, allow discharge")

        # VE.Bus Keepalive stoppen
        self.stop_vebus_keepalive()

        # ESS Mode reset external control to ESS with Phase Compensation
        reg = Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        target_value = 1
        rdef = REGISTERS[reg]

        logger.info(
            "[VictronModbus] Setting ESS Mode to 1=ESS with Phase Compensation: write %s=%d @ %d (%s)",
            reg.name,
            target_value,
            rdef.address,
            rdef.description,
        )

        self.write_register_verified(reg, target_value, retries=8, delay_s=0.2)

        logger.info(
            "[VictronModbus] Discharge mode active (discharge enabled, %s=%s)",
            reg.name,
            target_value,
        )

    def set_allow_grid_charging(self, value: bool):
        logger.info("[VictronModbus] Allow Gridcharging")

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
        2902 = 1 oder 2   # ESS an
        2900 = 9          # Keep batteries charged (typisch)
        2705 = sinnvoller Wert (z. B. 30–60 A)  # Lade-Limit
        """
        # TODO: Unit test fuer set_mode_force_charge erstellen
        if charge_power_w is None:
            raise TypeError("charge_power_w must be a number")

        if charge_power_w <= 0:
            # Disable grid charging + remove DVCC limit (Victron: -1 disables the limit)
            logger.info("[VictronModbus] Force grid charge disabled (P<=0).")
            return

        # ESS Mode external controll
        reg = Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        target_value = 3
        rdef = REGISTERS[reg]

        logger.info(
            "[VictronModbus] Setting ESS Mode to External Control: write %s=%d @ %d (%s)",
            reg.name,
            target_value,
            rdef.address,
            rdef.description,
        )

        self.write_register_verified(reg, target_value, retries=8, delay_s=0.2)

        logger.info(
            "[VictronModbus] charging from grid active (charge from grid enabled, %s=%s)",
            reg.name,
            target_value,
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
                address=reg_address, value=int(values), device_id=slave
            )

        if isinstance(values, (list, tuple)):
            return self.client.write_registers(
                address=reg_address,
                values=[int(v) for v in values],
                device_id=slave,
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

    def _read_reg_value(self, reg: Reg) -> int:
        rdef = REGISTERS[reg]
        resp = self.read_registers(rdef.address, rdef.count, unit=self.unit_id)
        if resp.isError():
            raise RuntimeError(f"Read error for {reg.name} @ {rdef.address}")
        return int(rdef.decode(resp.registers))

    def write_register_verified(
        self, reg: Reg, value: int, retries: int = 8, delay_s: float = 0.2
    ) -> None:
        # 1) write
        res = self.write_register(unit=self.unit_id, reg=reg, value=value)

        # optional: falls pymodbus response unterstützt
        if hasattr(res, "isError") and res.isError():
            raise RuntimeError(f"Write failed for {reg.name}")

        # 2) readback with retry
        last = None
        for i in range(retries):
            time.sleep(delay_s)
            last = self._read_reg_value(reg)
            if last == value:
                logger.info(
                    "[VictronModbus] Verified write %s=%s after %d attempt(s)",
                    reg.name,
                    value,
                    i + 1,
                )
                return

        raise RuntimeError(
            f"Write verification failed for {reg.name}: expected {value}, got {last}"
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
        words = self._encode_words(r, value)

        # 1 Word => FC06, mehrere => FC10
        if len(words) == 1:
            return self.write_holding_registers(
                unit=unit, address=address, values=words[0]
            )
        return self.write_holding_registers(unit=unit, address=address, values=words)

    @staticmethod
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
        logger.info(
            "[Inverter] API: Setting max_pv_charge_rate: %.1fW", max_pv_charge_rate
        )
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

    def _power_w_to_target_charge_current_a(self, power_w: float, batt_v: float) -> int:
        """Convert power in W to target current in A using battery voltage."""
        if power_w is None:
            raise TypeError("power_w must be a number")

        power_w = float(power_w)
        if power_w <= 0:
            return 0

        target_a = int(math.ceil(power_w / batt_v))
        return max(0, min(target_a, 32767))
