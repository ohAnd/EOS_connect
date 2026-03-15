"""Victron inverter interface module.

Provides the VictronInverter class which implements the BaseInverter
interface for Victron devices (Modbus/TCP client integration).

ESS Control Logic via EOS Connect

In the "Discharge Allowed" mode, the Victron ESS system remains in its
normal operating state. This requires that ESS is enabled and operating in
either "Optimized with BatteryLife" or "Optimized without BatteryLife" mode.
In this state, the ESS continues to operate according to Victron's internal
control logic.
When EOS Connect activates the "Avoid Discharge" mode, the ESS is switched to
External Control. EOS Connect then writes a power setpoint of 0 W to the
MultiPlus on all three phases via the VE.Bus registers. This prevents both
battery discharge and grid import, effectively keeping the system neutral with
respect to the grid.
This functionality requires a three-phase Victron MultiPlus system. Support for
single-phase systems still needs to be evaluated. If "Charge from Grid" is
activated by EOS Connect, a positive grid power setpoint corresponding to the
desired charging power is written to the MultiPlus. The inverter then regulates
the grid import accordingly and uses the imported energy to charge the
batteries.
When EOS Connect is closed or terminated, the original ESS mode is automatically restored.
This ensures that the Victron system returns to its normal ESS operation.
"""

import logging
from typing import Union, Any
import struct
import math
import time
import threading

from ..base_inverter import BaseInverter  # pylint: disable=relative-beyond-top-level

from .ccgx_registers_all import Reg, REGISTERS, RegisterDef

# pylint: disable=duplicate-code

logger = logging.getLogger("__main__").getChild("VictronModbus")
logger.setLevel(logging.INFO)
logger.info("[Inverter] Loading Victron Inverter")

# Import ModbusTcpClient - make it available at module level for testing/mocking
try:
    from pymodbus.client import ModbusTcpClient

    logger.info("[Inverter] pymodbus imported successfully")
except ImportError as e:
    ModbusTcpClient = None  # type: ignore
    logger.warning("[Inverter] pymodbus import failed: %s", e)


class VictronInverter(BaseInverter):
    """Victron inverter interface implementation."""

    supports_extended_monitoring_default = False
    max_grid_charge_rate: int | None
    max_pv_charge_rate: int | None

    def __init__(self, config):
        """Initialize the Victron inverter interface."""
        super().__init__(config)

        # --- Configuration values ---

        self.initialize()

        self._vebus_keepalive_thread: threading.Thread | None = None
        self._vebus_keepalive_stop = threading.Event()
        self._vebus_keepalive_lock = threading.Lock()
        self._vebus_keepalive_unit: int = 227

        # welche Regs zyklisch geschrieben werden + ihre aktuellen Zielwerte (W)
        self._vebus_targets: dict[Reg, int] = {}
        self._vebus_interval_s: float = 1.0  # 500ms

        # ESS state management
        self._saved_ess_state: dict[Reg, int] | None = None

    def start_vebus_keepalive(
        self, targets: dict[Reg, int], interval_s: float = 1.0, unit: int = 227
    ):
        """Start or update a VE.Bus keepalive thread that cyclically writes setpoints."""

        # sanitize
        interval_s = float(interval_s)
        if interval_s <= 0:
            interval_s = 1.0

        # targets/interval immer aktualisieren
        with self._vebus_keepalive_lock:
            self._vebus_targets = {k: int(v) for k, v in targets.items()}
            self._vebus_interval_s = interval_s
            self._vebus_keepalive_unit = int(unit)

        # if thread already running: only update
        t = self._vebus_keepalive_thread
        if t and t.is_alive():
            logger.info(
                "[VictronModbus] VE.Bus keepalive updated: %d targets "
                "(interval %.2fs, unit %d)",
                len(targets),
                self._vebus_interval_s,
                self._vebus_keepalive_unit,
            )
            return

        # ensure stop flag is cleared before starting
        self._vebus_keepalive_stop.clear()

        def _loop():
            logger.info("[VictronModbus] VE.Bus keepalive loop entered")
            while not self._vebus_keepalive_stop.is_set():
                with self._vebus_keepalive_lock:
                    snapshot = dict(self._vebus_targets)
                    interval = float(self._vebus_interval_s)
                    u = int(self._vebus_keepalive_unit)

                for reg, watts in snapshot.items():
                    try:
                        rdef = REGISTERS[reg]
                        self.write_holding_registers(
                            unit=u, address=rdef.address, values=int(watts)
                        )
                        # logger.info(
                        #   "[VictronModbus] VE.Bus keepalive write: unit=%d "
                        #   "reg=%s addr=%d value=%dW",
                        #  u,
                        # reg.name,
                        # rdef.address,
                        # int(watts),
                        # )
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        logger.warning(
                            "[VictronModbus] VE.Bus keepalive write failed for %s (unit %d): %s",
                            getattr(reg, "name", str(reg)),
                            u,
                            e,
                        )

                    if self._vebus_keepalive_stop.is_set():
                        break

                # wait() reacts immediately when stop is set (better than sleep)
                self._vebus_keepalive_stop.wait(timeout=interval)

            logger.info("[VictronModbus] VE.Bus keepalive loop exited")

        self._vebus_keepalive_thread = threading.Thread(
            target=_loop,
            daemon=True,
            name="VictronVeBusKeepalive",
        )
        self._vebus_keepalive_thread.start()

        logger.info(
            "[VictronModbus] VE.Bus keepalive started (%d regs, interval %.2fs, unit %d)",
            len(targets),
            self._vebus_interval_s,
            self._vebus_keepalive_unit,
        )

    def update_vebus_keepalive_targets(self, targets: dict[Reg, int]):
        """Aktualisiert die Zielwerte, Thread läuft weiter."""
        with self._vebus_keepalive_lock:
            for reg, watts in targets.items():
                self._vebus_targets[reg] = int(watts)

    def stop_vebus_keepalive(self, write_zero: bool = True):
        """Stop keepalive thread and optionally write 0W to all targets (fail-safe)."""
        self._vebus_keepalive_stop.set()

        t = self._vebus_keepalive_thread
        if t and t.is_alive():
            t.join(timeout=2.0)

        # which unit to use for fail-safe writes
        unit = getattr(self, "_vebus_keepalive_unit", 227)

        if write_zero:
            with self._vebus_keepalive_lock:
                regs = list(self._vebus_targets.keys())

            for reg in regs:
                try:
                    rdef = REGISTERS[reg]
                    self.write_holding_registers(
                        unit=unit, address=rdef.address, values=0
                    )
                except Exception as e:  # pylint: disable=broad-exception-caught
                    # Fail-safe cleanup: log but continue if write fails
                    logger.warning(
                        "[VictronModbus] Failed to write 0W for %s: %s", reg.name, e
                    )

        # reset state so a future start works cleanly
        self._vebus_keepalive_thread = None
        self._vebus_keepalive_stop.clear()

        logger.info("[VictronModbus] VE.Bus keepalive stopped")

    def initialize(self):
        """Initialize Modbus connection parameters and client."""
        self.address = self.config["address"]
        self.port = 502
        self.unit_id = 100

        self.client = ModbusTcpClient(self.address, port=self.port)
        self.connect_inverter()

    def set_mode_avoid_discharge(self):
        """Set the inverter to avoid discharging the battery.

        Modes:
            avoid_discharge: Battery NOT discharging
            Battery may charge from PV
            Grid absorbs difference
        """
        logger.info("[VictronModbus] Setting hold mode, avoid discharge")

        # ESS Externe Regelung ES Mode auf 3 und setzen dann ID 37,41 und 42
        # auf 0W -> es werden 0 W aus oder in richtung Grid geschoben kein Laden
        # kein Entladen

        # TODO: Read old state of battery life modus
        # Save current ESS state
        self.save_ess_state(unit=100)
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
        """Set the inverter back to normal ESS operation (allow discharge)."""
        logger.info("[VictronModbus] Setting discharge mode, allow discharge")

        # Aktuellen Hub4Mode lesen
        reg = Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        rdef = REGISTERS[reg]
        resp = self.read_registers(reg, unit=100)

        if resp is None or resp.isError():
            raise RuntimeError(
                f"Failed to read {reg.name} @ {rdef.address} before restoring ESS mode"
            )

        current_mode = int(rdef.decode(resp.registers))

        keepalive_running = (
            self._vebus_keepalive_thread is not None
            and self._vebus_keepalive_thread.is_alive()
        )

        logger.info(
            "[VictronModbus] Current Hub4Mode=%d, keepalive_running=%s",
            current_mode,
            keepalive_running,
        )

        # If neither External Control is active nor Keepalive is running, do nothing
        if current_mode != 3 and not keepalive_running:
            logger.info(
                "[VictronModbus] ESS already in normal mode, nothing to restore"
            )
            return

        # VE.Bus Keepalive stoppen + 0W schreiben
        self.stop_vebus_keepalive(write_zero=True)

        # Gespeicherten ESS Zustand wiederherstellen
        self.restore_ess_state(unit=100, retries=8, delay_s=0.2)

        logger.info("[VictronModbus] Discharge mode active (normal ESS restored)")

    def set_mode_allow_discharge_old(self):
        """Set the inverter back to normal ESS operation (allow discharge)."""
        logger.info("[VictronModbus] Setting discharge mode, allow discharge")

        # 1) VE.Bus Keepalive stoppen + 0W schreiben
        self.stop_vebus_keepalive(write_zero=True)

        # 2) Vorher gespeicherten ESS Zustand wiederherstellen (Hub4Mode + BatteryLife)
        #    Fallbacks sind in restore_ess_state() drin, falls nie gespeichert wurde
        self.restore_ess_state(unit=100, retries=8, delay_s=0.2)

        logger.info("[VictronModbus] Discharge mode active (normal ESS restored)")

    def set_allow_grid_charging(self, value: bool):
        """Set grid charging mode.

        Args:
            value: True to allow grid charging, False otherwise
        """
        logger.info("[VictronModbus] Allow Gridcharging")

    def set_battery_mode(self, mode):
        """Set battery operating mode.

        Args:
            mode: Battery mode to set

        Raises:
            NotImplementedError: This method is not implemented
        """
        raise NotImplementedError

    def get_battery_info(self):
        """Get battery information.

        Returns:
            Battery information dict

        Raises:
            NotImplementedError: This method is not implemented
        """
        raise NotImplementedError

    def fetch_inverter_data(self):
        """Get inverter data for monitoring (temperatures, fan control, etc.)."""
        logger.info("[VictronModbus] Reading Victron Inverter Values")

        reg = (
            Reg.SYSTEM_Dc_Battery_Soc
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

    def set_mode_force_charge(self, charge_power_w: float):
        """
        Force charging from grid with approx. charge power in W (3-phase).
        Uses ESS External Control + VE.Bus per-phase AcPowerSetpoints with keepalive.
        """
        if charge_power_w is None:
            raise TypeError("charge_power_w must be a number")

        charge_power_w = float(charge_power_w)

        if charge_power_w <= 0:
            logger.info("[VictronModbus] Force grid charge disabled (P<=0).")
            # optional: keepalive stoppen + setpoints auf 0
            # self.stop_vebus_keepalive(unit=227, write_zero=True)
            return

        # Save current ESS state
        self.save_ess_state(unit=100)

        # 1) ESS External Control
        reg = Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        hub4mode = 3
        rdef = REGISTERS[reg]

        logger.info(
            "[VictronModbus] Setting ESS Mode to External Control: %s=%d @ %d (%s)",
            reg.name,
            hub4mode,
            rdef.address,
            rdef.description,
        )

        self.write_register_verified(reg, hub4mode, retries=8, delay_s=0.2)

        # 2) Power -> per phase setpoint (positiv = import/charge)
        # charge_power_w = 500.0  # for testing
        per_phase_w = int(math.ceil(charge_power_w / 3.0))
        setpoint_w = +per_phase_w  # import from grid

        targets = {
            Reg.VEBUS_Hub4_L1_AcPowerSetpoint_37: setpoint_w,
            Reg.VEBUS_Hub4_L2_AcPowerSetpoint_40: setpoint_w,
            Reg.VEBUS_Hub4_L3_AcPowerSetpoint_41: setpoint_w,
        }

        # 3) initial write once
        for r, v in targets.items():
            rd = REGISTERS[r]
            logger.info(
                "[VictronModbus] VE.Bus ID %d (%s) -> %dW", rd.address, r.name, v
            )
            self.write_holding_registers(unit=227, address=rd.address, values=v)

        # 4) keepalive (important!)
        self.start_vebus_keepalive(targets, interval_s=1.0, unit=227)

        logger.info(
            "[VictronModbus] Force charge active: total=%dW, per_phase=%dW (setpoint=%dW each)",
            int(charge_power_w),
            per_phase_w,
            setpoint_w,
        )

    def save_ess_state(self, unit: int = 100, overwrite: bool = False) -> None:
        """
        Save current ESS state (Hub4Mode + BatteryLife) only once by default.
        Will NOT save if currently already in External Control (Hub4Mode=3),
        unless overwrite=True.
        """
        if not overwrite and getattr(self, "_saved_ess_state", None):
            logger.info("[VictronModbus] ESS state already saved -> skip")
            return

        # read current Hub4Mode first
        hub_reg = Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        hub_def = REGISTERS[hub_reg]
        hub_resp = self.read_registers(hub_reg, unit=unit)

        if hub_resp is None or hub_resp.isError():
            logger.warning(
                "[VictronModbus] Could not read Hub4Mode -> not saving ESS state"
            )
            return

        hub4mode = int(round(float(hub_def.decode(hub_resp.registers))))

        # if already External Control, don't save (unless overwrite explicitly)
        if hub4mode == 3 and not overwrite:
            logger.info(
                "[VictronModbus] Hub4Mode=3 (External Control) -> skip saving ESS state"
            )
            return

        state: dict[Reg, int] = {hub_reg: hub4mode}

        # read BatteryLife state
        bl_reg = Reg.SETTINGS_Settings_CGwacs_BatteryLife_State
        bl_def = REGISTERS[bl_reg]
        bl_resp = self.read_registers(bl_reg, unit=unit)

        if bl_resp is not None and not bl_resp.isError():
            state[bl_reg] = int(round(float(bl_def.decode(bl_resp.registers))))
        else:
            logger.warning(
                "[VictronModbus] Could not read BatteryLife state -> saving Hub4Mode only"
            )

        self._saved_ess_state = state
        logger.info(
            "[VictronModbus] Saved ESS baseline: %s",
            {k.name: v for k, v in state.items()},
        )

    def restore_ess_state(
        self, unit: int = 100, retries: int = 8, delay_s: float = 0.2
    ) -> None:
        """Restore previously saved ESS-related settings."""
        state = getattr(self, "_saved_ess_state", None)

        if not state:
            logger.warning("[VictronModbus] No saved ESS state found -> using defaults")
            state = {
                Reg.SETTINGS_Settings_Cgwacs_Hub4Mode: 1,  # normal ESS
                Reg.SETTINGS_Settings_CGwacs_BatteryLife_State: 10,  # without Batterylife
            }

        # stop keepalive first so it can't fight the restore
        try:
            self.stop_vebus_keepalive(write_zero=True)
        except Exception:  # pylint: disable=broad-exception-caught
            # Defensive error handling: continue with restore even if keepalive stop fails
            pass

        # restore in order: Hub4Mode first, then BatteryLife
        for reg in (
            Reg.SETTINGS_Settings_Cgwacs_Hub4Mode,
            Reg.SETTINGS_Settings_CGwacs_BatteryLife_State,
        ):
            if reg not in state:
                continue

            val = int(state[reg])
            rdef = REGISTERS[reg]

            logger.info(
                "[VictronModbus] Restoring %s=%d @ %d (%s)",
                reg.name,
                val,
                rdef.address,
                rdef.description,
            )
            self.write_register_verified(reg, val, retries=retries, delay_s=delay_s)

        logger.info("[VictronModbus] ESS state restored")

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
        """Close the Modbus TCP client connection."""
        # close modbus socket connection
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
            # Check if address is a Reg enum (has 'name' attribute) or RegisterDef directly
            if isinstance(address, Reg):
                regdef = REGISTERS[address]
            else:
                regdef = address  # Already a RegisterDef
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
        # logger.info(
        #     "[VictronModbus] Writing unit %s address %s values %s",
        #     unit,
        #     address,
        #     values,
        # )

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

        # For CCGX offsets (2600, 2705, 800, ...), this is already correct.
        # If someone ever passes 40001/4xxxx, normalize will handle it correctly:
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
        """Write a register value to the device.

        Args:
            unit: Modbus unit ID
            reg: Register enum to write to
            value: Raw register value to write

        Raises:
            ValueError: If register is not writable
        """
        r = REGISTERS[reg]

        if not r.writable:
            raise ValueError(f"Register {reg.name} is not writable")

        # value ist bereits Rohwert (kein Scaling hier!)
        return self.write_holding_registers_new(
            unit=unit,
            address=r.address,
            values=value,
        )

    def _read_reg_value(self, reg: Reg) -> int:
        """Read and decode a register value from the device.

        Args:
            reg: Register enum to read

        Returns:
            Decoded register value as integer

        Raises:
            RuntimeError: If read operation fails
        """
        rdef = REGISTERS[reg]
        resp = self.read_registers(rdef.address, rdef.count, unit=self.unit_id)
        if resp.isError():
            raise RuntimeError(f"Read error for {reg.name} @ {rdef.address}")
        return int(rdef.decode(resp.registers))

    def write_register_verified(
        self, reg: Reg, value: int, retries: int = 8, delay_s: float = 0.2
    ) -> None:
        """Write a register and verify the value was written correctly.

        Args:
            reg: Register enum to write
            value: Value to write
            retries: Number of retry attempts for verification
            delay_s: Delay in seconds between retries

        Raises:
            RuntimeError: If write or verification fails
        """
        # 1) write
        res = self.write_register(unit=self.unit_id, reg=reg, value=value)

        # optional: if pymodbus response supports it
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
        """Write a register value with type encoding.

        Args:
            unit: Modbus unit ID
            reg: Register enum to write
            value: Value to write (will be encoded according to register type)

        Raises:
            ValueError: If register is not writable
        """
        r = REGISTERS[reg]
        if not r.writable:
            raise ValueError(
                f"Register {reg.name} is marked read-only (writable=False)"
            )

        address = r.address  # <- already correct for CCGX Modbus TCP
        words = self.encode_words(r, value)

        # 1 Word => FC06, multiple => FC10
        if len(words) == 1:
            return self.write_holding_registers(
                unit=unit, address=address, values=words[0]
            )
        return self.write_holding_registers(unit=unit, address=address, values=words)

    @staticmethod
    def encode_words(reg: RegisterDef, value: Any) -> list[int]:
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
        v_reg = Reg.SYSTEM_Dc_Battery_Voltage
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

    def disconnect(self):
        """Session closes itself."""
        self.disconnect_inverter()
        logger.info(f"[{self.inverter_type}] Session closed")

    def shutdown(self):
        """Restore ESS state and close modbus session on shutdown."""
        # Vorher gespeicherten ESS Zustand wiederherstellen (Hub4Mode + BatteryLife)
        # Fallbacks sind in restore_ess_state() drin, falls nie gespeichert wurde
        logger.info("[VictronModbus] Restoring ESS state before shutdown...")
        self.restore_ess_state(unit=100, retries=8, delay_s=0.2)
        # Close modbus session
        self.disconnect()
