"""CCGX Modbus register definitions (filtered subset).

Auto-generated from CCGX-Modbus-TCP-register-list-3.60(1).xlsx.
Filtered for: dbus-service-name in
  - com.victronenergy.system
  - com.victronenergy.grid
  - com.victronenergy.settings

This module provides register definitions and decoder functions for the filtered set of
CCGX Modbus registers relevant to system, grid, and settings services.
"""

# pylint: disable=duplicate-code

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence, Any
import struct

WORD_ORDER: str = "big"  # 'big' (default) or 'little' for multi-register values


def _words_to_bytes(words: Sequence[int]) -> bytes:
    """Convert a sequence of 16-bit words to bytes, handling word order."""
    w = [int(x) & 0xFFFF for x in words]
    if WORD_ORDER == "little" and len(w) > 1:
        w = list(reversed(w))
    return b"".join(struct.pack(">H", x) for x in w)


def decode_uint16(words: Sequence[int]) -> int:
    """Decode a 16-bit unsigned integer from a sequence of words."""
    return int(words[0]) & 0xFFFF


def decode_int16(words: Sequence[int]) -> int:
    """Decode a 16-bit signed integer from a sequence of words."""
    v = int(words[0]) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def decode_uint32(words: Sequence[int]) -> int:
    """Decode a 32-bit unsigned integer from a sequence of words."""
    b = _words_to_bytes(words[:2])
    return int.from_bytes(b, "big", signed=False)


def decode_int32(words: Sequence[int]) -> int:
    """Decode a 32-bit signed integer from a sequence of words."""
    b = _words_to_bytes(words[:2])
    return int.from_bytes(b, "big", signed=True)


def decode_float32(words: Sequence[int]) -> float:
    """Decode a 32-bit floating-point value from a sequence of words."""
    b = _words_to_bytes(words[:2])
    return struct.unpack(">f", b)[0]


def decode_uint64(words: Sequence[int]) -> int:
    """Decode a 64-bit unsigned integer from a sequence of words."""
    b = _words_to_bytes(words[:4])
    return int.from_bytes(b, "big", signed=False)


def decode_int64(words: Sequence[int]) -> int:
    """Decode a 64-bit signed integer from a sequence of words."""
    b = _words_to_bytes(words[:4])
    return int.from_bytes(b, "big", signed=True)


def decode_float64(words: Sequence[int]) -> float:
    """Decode a 64-bit floating-point value from a sequence of words."""
    b = _words_to_bytes(words[:4])
    return struct.unpack(">d", b)[0]


def decode_string(words: Sequence[int]) -> str:
    """Decode a null-terminated string from a sequence of words."""
    b = _words_to_bytes(words)
    b = b.split(b"\x00", 1)[0]
    return b.decode("utf-8", errors="replace")


TYPE_DECODERS: dict[str, Callable[[Sequence[int]], Any]] = {
    "uint16": decode_uint16,
    "int16": decode_int16,
    "uint32": decode_uint32,
    "int32": decode_int32,
    "float32": decode_float32,
    "uint64": decode_uint64,
    "int64": decode_int64,
    "float64": decode_float64,
}


@dataclass(frozen=True)
class RegisterDef:
    """Definition of a CCGX Modbus register.

    Attributes:
        address: Modbus register starting address.
        count: Number of 16-bit words for this register (default 1).
        scale: Scaling factor to apply to decoded value (default 1.0).
        unit: Unit of measurement for this register.
        type: Data type ('uint16', 'int16', 'uint32', 'int32', 'float32', 'float64', 'string').
        writable: Whether this register can be written to.
        service: Service path identifier.
        path: Full path to this register.
        description: Human-readable description of the register.
        range: Valid range for this register value.
        remarks: Additional remarks or notes about the register.
        decoder: Optional custom decoder function (overrides type-based decoder).
    """

    address: int
    count: int = 1
    scale: float = 1.0
    unit: str = ""
    type: str = "uint16"
    writable: bool = False
    service: str = ""
    path: str = ""
    description: str = ""
    range: str = ""
    remarks: str = ""
    decoder: Optional[Callable[[Sequence[int]], Any]] = None

    def decode(self, words: Sequence[int]) -> Any:
        """Decode register value from words using the appropriate decoder."""
        t = (self.type or "uint16").strip().lower()
        dec = self.decoder
        if dec is None:
            if t.startswith("string"):
                dec = decode_string
            else:
                dec = TYPE_DECODERS.get(t, decode_uint16)
        val = dec(words)
        if isinstance(val, (int, float)):
            return val * float(self.scale)
        return val


class Reg(Enum):
    """Enumeration of filtered CCGX Modbus registers.

    Contains register definitions for grid, settings, and system services:
    - Grid: Electrical grid parameters (voltage, current, power, energy measurements)
    - Settings: System configuration parameters (battery limits, capabilities, modes)
    - System: Overall system status and power distribution measurements
    """

    GRID_AC_L1_POWER = "com.victronenergy.grid:/Ac/L1/Power"
    GRID_AC_L2_POWER = "com.victronenergy.grid:/Ac/L2/Power"
    GRID_AC_L3_POWER = "com.victronenergy.grid:/Ac/L3/Power"
    GRID_AC_L1_ENERGY_FORWARD = "com.victronenergy.grid:/Ac/L1/Energy/Forward"
    GRID_AC_L2_ENERGY_FORWARD = "com.victronenergy.grid:/Ac/L2/Energy/Forward"
    GRID_AC_L3_ENERGY_FORWARD = "com.victronenergy.grid:/Ac/L3/Energy/Forward"
    GRID_AC_L1_ENERGY_REVERSE = "com.victronenergy.grid:/Ac/L1/Energy/Reverse"
    GRID_AC_L2_ENERGY_REVERSE = "com.victronenergy.grid:/Ac/L2/Energy/Reverse"
    GRID_AC_L3_ENERGY_REVERSE = "com.victronenergy.grid:/Ac/L3/Energy/Reverse"
    GRID_SERIAL = "com.victronenergy.grid:/Serial"
    GRID_AC_L1_VOLTAGE = "com.victronenergy.grid:/Ac/L1/Voltage"
    GRID_AC_L1_CURRENT = "com.victronenergy.grid:/Ac/L1/Current"
    GRID_AC_L2_VOLTAGE = "com.victronenergy.grid:/Ac/L2/Voltage"
    GRID_AC_L2_CURRENT = "com.victronenergy.grid:/Ac/L2/Current"
    GRID_AC_L3_VOLTAGE = "com.victronenergy.grid:/Ac/L3/Voltage"
    GRID_AC_L3_CURRENT = "com.victronenergy.grid:/Ac/L3/Current"
    GRID_AC_L1_ENERGY_FORWARD_2622 = "com.victronenergy.grid:/Ac/L1/Energy/Forward"
    GRID_AC_L2_ENERGY_FORWARD_2624 = "com.victronenergy.grid:/Ac/L2/Energy/Forward"
    GRID_AC_L3_ENERGY_FORWARD_2626 = "com.victronenergy.grid:/Ac/L3/Energy/Forward"
    GRID_AC_L1_ENERGY_REVERSE_2628 = "com.victronenergy.grid:/Ac/L1/Energy/Reverse"
    GRID_AC_L2_ENERGY_REVERSE_2630 = "com.victronenergy.grid:/Ac/L2/Energy/Reverse"
    GRID_AC_L3_ENERGY_REVERSE_2632 = "com.victronenergy.grid:/Ac/L3/Energy/Reverse"
    GRID_AC_ENERGY_FORWARD = "com.victronenergy.grid:/Ac/Energy/Forward"
    GRID_AC_ENERGY_REVERSE = "com.victronenergy.grid:/Ac/Energy/Reverse"
    GRID_AC_L1_POWER_2638 = "com.victronenergy.grid:/Ac/L1/Power"
    GRID_AC_L2_POWER_2640 = "com.victronenergy.grid:/Ac/L2/Power"
    GRID_AC_L3_POWER_2642 = "com.victronenergy.grid:/Ac/L3/Power"
    GRID_AC_FREQUENCY = "com.victronenergy.grid:/Ac/Frequency"
    GRID_AC_L1_POWERFACTOR = "com.victronenergy.grid:/Ac/L1/PowerFactor"
    GRID_AC_L2_POWERFACTOR = "com.victronenergy.grid:/Ac/L2/PowerFactor"
    GRID_AC_L3_POWERFACTOR = "com.victronenergy.grid:/Ac/L3/PowerFactor"
    GRID_AC_POWERFACTOR = "com.victronenergy.grid:/Ac/PowerFactor"
    SETTINGS_SETTINGS_CGWACS_ACPOWERSETPOINT = (
        "com.victronenergy.settings:/Settings/Cgwacs/AcPowerSetPoint"
    )
    SETTINGS_SETTINGS_CGWACS_MAXCHARGEPERCENTAGE = (
        "com.victronenergy.settings:/Settings/Cgwacs/MaxChargePercentage"
    )
    SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPERCENTAGE = (
        "com.victronenergy.settings:/Settings/Cgwacs/MaxDischargePercentage"
    )
    SETTINGS_SETTINGS_CGWACS_ACPOWERSETPOINT_2703 = (
        "com.victronenergy.settings:/Settings/Cgwacs/AcPowerSetPoint"
    )
    SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPOWER = (
        "com.victronenergy.settings:/Settings/Cgwacs/MaxDischargePower"
    )
    SETTINGS_SETTINGS_SYSTEMSETUP_MAXCHARGECURRENT = (
        "com.victronenergy.settings:/Settings/SystemSetup/MaxChargeCurrent"
    )
    SETTINGS_SETTINGS_CGWACS_MAXFEEDINPOWER = (
        "com.victronenergy.settings:/Settings/Cgwacs/MaxFeedInPower"
    )
    SETTINGS_SETTINGS_CGWACS_OVERVOLTAGEFEEDIN = (
        "com.victronenergy.settings:/Settings/Cgwacs/OvervoltageFeedIn"
    )
    SETTINGS_SETTINGS_CGWACS_PREVENTFEEDBACK = (
        "com.victronenergy.settings:/Settings/Cgwacs/PreventFeedback"
    )
    SETTINGS_SETTINGS_SYSTEMSETUP_MAXCHARGEVOLTAGE = (
        "com.victronenergy.settings:/Settings/SystemSetup/MaxChargeVoltage"
    )
    SETTINGS_SETTINGS_SYSTEMSETUP_ACINPUT1 = (
        "com.victronenergy.settings:/Settings/SystemSetup/AcInput1"
    )
    SETTINGS_SETTINGS_SYSTEMSETUP_ACINPUT2 = (
        "com.victronenergy.settings:/Settings/SystemSetup/AcInput2"
    )
    SETTINGS_SETTINGS_CGWACS_ACEXPORTLIMIT = (
        "com.victronenergy.settings:/Settings/CGwacs/AcExportLimit"
    )
    SETTINGS_SETTINGS_CGWACS_ACINPUTLIMIT = (
        "com.victronenergy.settings:/Settings/Cgwacs/AcInputLimit"
    )
    SETTINGS_SETTINGS_CGWACS_ALWAYSPEAKSHAVE = (
        "com.victronenergy.settings:/Settings/CGwacs/AlwaysPeakShave"
    )
    SETTINGS_SETTINGS_CGWACS_RUNWITHOUTGRIDMETER = (
        "com.victronenergy.settings:/Settings/CGwacs/RunWithoutGridMeter"
    )
    SETTINGS_SETTINGS_CGWACS_BATTERYLIFE_STATE = (
        "com.victronenergy.settings:/Settings/CGwacs/BatteryLife/State"
    )
    SETTINGS_SETTINGS_CGWACS_BATTERYLIFE_MINIMUMSOCLIMIT = (
        "com.victronenergy.settings:/Settings/CGwacs/BatteryLife/MinimumSocLimit"
    )
    SETTINGS_SETTINGS_CGWACS_HUB4MODE = (
        "com.victronenergy.settings:/Settings/Cgwacs/Hub4Mode"
    )
    SETTINGS_SETTINGS_CGWACS_BATTERYLIFE_SOCLIMIT = (
        "com.victronenergy.settings:/Settings/Cgwacs/BatteryLife/SocLimit"
    )
    SETTINGS_SETTINGS_PUMP0_AUTOSTARTENABLED = (
        "com.victronenergy.settings:/Settings/Pump0/AutoStartEnabled"
    )
    SETTINGS_SETTINGS_PUMP0_MODE = "com.victronenergy.settings:/Settings/Pump0/Mode"
    SETTINGS_SETTINGS_PUMP0_STARTVALUE = (
        "com.victronenergy.settings:/Settings/Pump0/StartValue"
    )
    SETTINGS_SETTINGS_PUMP0_STOPVALUE = (
        "com.victronenergy.settings:/Settings/Pump0/StopValue"
    )
    SETTINGS_SETTINGS_DYNAMICESS_BATTERYCAPACITY = (
        "com.victronenergy.settings:/Settings/DynamicEss/BatteryCapacity"
    )
    SETTINGS_SETTINGS_DYNAMICESS_FULLCHARGEDURATION = (
        "com.victronenergy.settings:/Settings/DynamicEss/FullChargeDuration"
    )
    SETTINGS_SETTINGS_DYNAMICESS_FULLCHARGEINTERVAL = (
        "com.victronenergy.settings:/Settings/DynamicEss/FullChargeInterval"
    )
    SETTINGS_SETTINGS_DYNAMICESS_MODE = (
        "com.victronenergy.settings:/Settings/DynamicEss/Mode"
    )
    SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_ALLOWGRIDFEEDIN = (
        "com.victronenergy.settings:/Settings/DynamicEss/Schedule/0/AllowGridFeedIn"
    )
    SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_DURATION = (
        "com.victronenergy.settings:/Settings/DynamicEss/Schedule/0/Duration"
    )
    SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_RESTRICTIONS = (
        "com.victronenergy.settings:/Settings/DynamicEss/Schedule/0/Restrictions"
    )
    SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_SOC = (
        "com.victronenergy.settings:/Settings/DynamicEss/Schedule/0/Soc"
    )
    SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_START = (
        "com.victronenergy.settings:/Settings/DynamicEss/Schedule/0/Start"
    )
    SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_STRATEGY = (
        "com.victronenergy.settings:/Settings/DynamicEss/Schedule/0/Strategy"
    )
    SYSTEM_SERIAL = "com.victronenergy.system:/Serial"
    SYSTEM_RELAY_0_STATE = "com.victronenergy.system:/Relay/0/State"
    SYSTEM_RELAY_1_STATE = "com.victronenergy.system:/Relay/1/State"
    SYSTEM_AC_PVONOUTPUT_L1_POWER = "com.victronenergy.system:/Ac/PvOnOutput/L1/Power"
    SYSTEM_AC_PVONOUTPUT_L2_POWER = "com.victronenergy.system:/Ac/PvOnOutput/L2/Power"
    SYSTEM_AC_PVONOUTPUT_L3_POWER = "com.victronenergy.system:/Ac/PvOnOutput/L3/Power"
    SYSTEM_AC_PVONGRID_L1_POWER = "com.victronenergy.system:/Ac/PvOnGrid/L1/Power"
    SYSTEM_AC_PVONGRID_L2_POWER = "com.victronenergy.system:/Ac/PvOnGrid/L2/Power"
    SYSTEM_AC_PVONGRID_L3_POWER = "com.victronenergy.system:/Ac/PvOnGrid/L3/Power"
    SYSTEM_AC_PVONGENSET_L1_POWER = "com.victronenergy.system:/Ac/PvOnGenset/L1/Power"
    SYSTEM_AC_PVONGENSET_L2_POWER = "com.victronenergy.system:/Ac/PvOnGenset/L2/Power"
    SYSTEM_AC_PVONGENSET_L3_POWER = "com.victronenergy.system:/Ac/PvOnGenset/L3/Power"
    SYSTEM_AC_CONSUMPTION_L1_POWER = "com.victronenergy.system:/Ac/Consumption/L1/Power"
    SYSTEM_AC_CONSUMPTION_L2_POWER = "com.victronenergy.system:/Ac/Consumption/L2/Power"
    SYSTEM_AC_CONSUMPTION_L3_POWER = "com.victronenergy.system:/Ac/Consumption/L3/Power"
    SYSTEM_AC_GRID_L1_POWER = "com.victronenergy.system:/Ac/Grid/L1/Power"
    SYSTEM_AC_GRID_L2_POWER = "com.victronenergy.system:/Ac/Grid/L2/Power"
    SYSTEM_AC_GRID_L3_POWER = "com.victronenergy.system:/Ac/Grid/L3/Power"
    SYSTEM_AC_GENSET_L1_POWER = "com.victronenergy.system:/Ac/Genset/L1/Power"
    SYSTEM_AC_GENSET_L2_POWER = "com.victronenergy.system:/Ac/Genset/L2/Power"
    SYSTEM_AC_GENSET_L3_POWER = "com.victronenergy.system:/Ac/Genset/L3/Power"
    SYSTEM_AC_ACTIVEIN_SOURCE = "com.victronenergy.system:/Ac/ActiveIn/Source"
    SYSTEM_INTERNAL = "com.victronenergy.system:INTERNAL"
    SYSTEM_DC_BATTERY_VOLTAGE = "com.victronenergy.system:/Dc/Battery/Voltage"
    SYSTEM_DC_BATTERY_CURRENT = "com.victronenergy.system:/Dc/Battery/Current"
    SYSTEM_DC_BATTERY_POWER = "com.victronenergy.system:/Dc/Battery/Power"
    SYSTEM_DC_BATTERY_SOC = "com.victronenergy.system:/Dc/Battery/Soc"
    SYSTEM_DC_BATTERY_STATE = "com.victronenergy.system:/Dc/Battery/State"
    SYSTEM_DC_BATTERY_CONSUMEDAMPHOURS = (
        "com.victronenergy.system:/Dc/Battery/ConsumedAmphours"
    )
    SYSTEM_DC_BATTERY_TIMETOGO = "com.victronenergy.system:/Dc/Battery/TimeToGo"
    SYSTEM_DC_PV_POWER = "com.victronenergy.system:/Dc/Pv/Power"
    SYSTEM_DC_PV_CURRENT = "com.victronenergy.system:/Dc/Pv/Current"
    SYSTEM_DC_CHARGER_POWER = "com.victronenergy.system:/Dc/Charger/Power"
    SYSTEM_DC_SYSTEM_POWER = "com.victronenergy.system:/Dc/System/Power"
    SYSTEM_DC_VEBUS_CURRENT = "com.victronenergy.system:/Dc/Vebus/Current"
    SYSTEM_DC_VEBUS_POWER = "com.victronenergy.system:/Dc/Vebus/Power"
    SYSTEM_DC_INVERTERCHARGER_CURRENT = (
        "com.victronenergy.system:/Dc/InverterCharger/Current"
    )
    SYSTEM_DC_INVERTERCHARGER_POWER = (
        "com.victronenergy.system:/Dc/InverterCharger/Power"
    )
    SYSTEM_AC_CONSUMPTIONONINPUT_L1_POWER = (
        "com.victronenergy.system:/Ac/ConsumptionOnInput/L1/Power"
    )
    SYSTEM_AC_CONSUMPTIONONINPUT_L2_POWER = (
        "com.victronenergy.system:/Ac/ConsumptionOnInput/L2/Power"
    )
    SYSTEM_AC_CONSUMPTIONONINPUT_L3_POWER = (
        "com.victronenergy.system:/Ac/ConsumptionOnInput/L3/Power"
    )
    SYSTEM_AC_CONSUMPTIONONOUTPUT_L1_POWER = (
        "com.victronenergy.system:/Ac/ConsumptionOnOutput/L1/Power"
    )
    SYSTEM_AC_CONSUMPTIONONOUTPUT_L2_POWER = (
        "com.victronenergy.system:/Ac/ConsumptionOnOutput/L2/Power"
    )
    SYSTEM_AC_CONSUMPTIONONOUTPUT_L3_POWER = (
        "com.victronenergy.system:/Ac/ConsumptionOnOutput/L3/Power"
    )
    SYSTEM_DYNAMICESS_ACTIVE = "com.victronenergy.system:/DynamicEss/Active"
    SYSTEM_DYNAMICESS_ALLOWGRIDFEEDIN = (
        "com.victronenergy.system:/DynamicEss/AllowGridFeedIn"
    )
    SYSTEM_DYNAMICESS_AVAILABLE = "com.victronenergy.system:/DynamicEss/Available"
    SYSTEM_DYNAMICESS_CHARGERATE = "com.victronenergy.system:/DynamicEss/ChargeRate"
    SYSTEM_DYNAMICESS_ERRORCODE = "com.victronenergy.system:/DynamicEss/ErrorCode"
    SYSTEM_DYNAMICESS_RESTRICTIONS = "com.victronenergy.system:/DynamicEss/Restrictions"
    SYSTEM_DYNAMICESS_STRATEGY = "com.victronenergy.system:/DynamicEss/Strategy"
    SYSTEM_DYNAMICESS_TARGETSOC = "com.victronenergy.system:/DynamicEss/TargetSoc"


REGISTERS: dict[Reg, RegisterDef] = {
    Reg.GRID_AC_L1_POWER: RegisterDef(
        address=2600,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Power",
        description="Grid L1 - Power",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.GRID_AC_L2_POWER: RegisterDef(
        address=2601,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Power",
        description="Grid L2 - Power",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.GRID_AC_L3_POWER: RegisterDef(
        address=2602,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Power",
        description="Grid L3 - Power",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.GRID_AC_L1_ENERGY_FORWARD: RegisterDef(
        address=2603,
        count=1,
        scale=100.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Energy/Forward",
        description="Grid L1 - Energy from net",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_AC_L2_ENERGY_FORWARD: RegisterDef(
        address=2604,
        count=1,
        scale=100.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Energy/Forward",
        description="Grid L2 - Energy from net",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_AC_L3_ENERGY_FORWARD: RegisterDef(
        address=2605,
        count=1,
        scale=100.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Energy/Forward",
        description="Grid L3 - Energy from net",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_AC_L1_ENERGY_REVERSE: RegisterDef(
        address=2606,
        count=1,
        scale=100.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Energy/Reverse",
        description="Grid L1 - Energy to net",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_AC_L2_ENERGY_REVERSE: RegisterDef(
        address=2607,
        count=1,
        scale=100.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Energy/Reverse",
        description="Grid L2 - Energy to net",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_AC_L3_ENERGY_REVERSE: RegisterDef(
        address=2608,
        count=1,
        scale=100.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Energy/Reverse",
        description="Grid L3 - Energy to net",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_SERIAL: RegisterDef(
        address=2609,
        count=4,
        scale=1.0,
        unit="",
        type="string[7]",
        writable=True,
        service="com.victronenergy.grid",
        path="/Serial",
        description="Serial",
        range="14 characters",
        remarks="Grid meter serial as string.",
    ),
    Reg.GRID_AC_L1_VOLTAGE: RegisterDef(
        address=2616,
        count=1,
        scale=10.0,
        unit="V AC",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Voltage",
        description="Grid L1 – Voltage",
        range="0 to 6553.5",
        remarks="",
    ),
    Reg.GRID_AC_L1_CURRENT: RegisterDef(
        address=2617,
        count=1,
        scale=10.0,
        unit="A AC",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Current",
        description="Grid L1 – Current",
        range="-3276.8 to 3276.7",
        remarks="",
    ),
    Reg.GRID_AC_L2_VOLTAGE: RegisterDef(
        address=2618,
        count=1,
        scale=10.0,
        unit="V AC",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Voltage",
        description="Grid L2 – Voltage",
        range="0 to 6553.5",
        remarks="",
    ),
    Reg.GRID_AC_L2_CURRENT: RegisterDef(
        address=2619,
        count=1,
        scale=10.0,
        unit="A AC",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Current",
        description="Grid L2 – Current",
        range="-3276.8 to 3276.7",
        remarks="",
    ),
    Reg.GRID_AC_L3_VOLTAGE: RegisterDef(
        address=2620,
        count=1,
        scale=10.0,
        unit="V AC",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Voltage",
        description="Grid L3 – Voltage",
        range="0 to 6553.5",
        remarks="",
    ),
    Reg.GRID_AC_L3_CURRENT: RegisterDef(
        address=2621,
        count=1,
        scale=10.0,
        unit="A AC",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Current",
        description="Grid L3 – Current",
        range="-3276.8 to 3276.7",
        remarks="",
    ),
    Reg.GRID_AC_L1_ENERGY_FORWARD_2622: RegisterDef(
        address=2622,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Energy/Forward",
        description="Grid L1 - Energy from net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_L2_ENERGY_FORWARD_2624: RegisterDef(
        address=2624,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Energy/Forward",
        description="Grid L2 - Energy from net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_L3_ENERGY_FORWARD_2626: RegisterDef(
        address=2626,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Energy/Forward",
        description="Grid L3 - Energy from net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_L1_ENERGY_REVERSE_2628: RegisterDef(
        address=2628,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Energy/Reverse",
        description="Grid L1 - Energy to net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_L2_ENERGY_REVERSE_2630: RegisterDef(
        address=2630,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Energy/Reverse",
        description="Grid L2 - Energy to net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_L3_ENERGY_REVERSE_2632: RegisterDef(
        address=2632,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Energy/Reverse",
        description="Grid L3 - Energy to net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_ENERGY_FORWARD: RegisterDef(
        address=2634,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/Energy/Forward",
        description="Total Energy from net",
        range="0 to 42949672.96",
        remarks=(
            "Depending on the energy summation method used by the meter, "
            "this may be different to the sum of the individual counters"
        ),
    ),
    Reg.GRID_AC_ENERGY_REVERSE: RegisterDef(
        address=2636,
        count=2,
        scale=100.0,
        unit="kWh",
        type="uint32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/Energy/Reverse",
        description="Total Energy to net",
        range="0 to 42949672.96",
        remarks="",
    ),
    Reg.GRID_AC_L1_POWER_2638: RegisterDef(
        address=2638,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/Power",
        description="Grid L1 - Power",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.GRID_AC_L2_POWER_2640: RegisterDef(
        address=2640,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/Power",
        description="Grid L2 - Power",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.GRID_AC_L3_POWER_2642: RegisterDef(
        address=2642,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/Power",
        description="Grid L3 - Power",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.GRID_AC_FREQUENCY: RegisterDef(
        address=2644,
        count=1,
        scale=100.0,
        unit="Hz",
        type="uint16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/Frequency",
        description="AC Frequency",
        range="0 to 655.35",
        remarks="",
    ),
    Reg.GRID_AC_L1_POWERFACTOR: RegisterDef(
        address=2645,
        count=1,
        scale=1000.0,
        unit="",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L1/PowerFactor",
        description="L1 Power Factor",
        range="-32.768 to 32.767",
        remarks="",
    ),
    Reg.GRID_AC_L2_POWERFACTOR: RegisterDef(
        address=2646,
        count=1,
        scale=1000.0,
        unit="",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L2/PowerFactor",
        description="L2 Power Factor",
        range="-32.768 to 32.767",
        remarks="",
    ),
    Reg.GRID_AC_L3_POWERFACTOR: RegisterDef(
        address=2647,
        count=1,
        scale=1000.0,
        unit="",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/L3/PowerFactor",
        description="L3 Power Factor",
        range="-32.768 to 32.767",
        remarks="",
    ),
    Reg.GRID_AC_POWERFACTOR: RegisterDef(
        address=2648,
        count=1,
        scale=1000.0,
        unit="",
        type="int16",
        writable=True,
        service="com.victronenergy.grid",
        path="/Ac/PowerFactor",
        description="Total Power Factor",
        range="-32.768 to 32.767",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_ACPOWERSETPOINT: RegisterDef(
        address=2700,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/AcPowerSetPoint",
        description="ESS control loop setpoint",
        range="-32768 to 32767",
        remarks=(
            "ESS Mode 2 - Setpoint for the ESS control-loop in the CCGX. "
            "The control-loop will increase/decrease the Multi charge/discharge "
            "power to get the grid reading to this setpoint"
        ),
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_MAXCHARGEPERCENTAGE: RegisterDef(
        address=2701,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/MaxChargePercentage",
        description="ESS max charge current (fractional)",
        range="0 to 100",
        remarks=(
            "ESS Mode 2 - Max charge current for ESS control-loop. "
            "The control-loop will use this value to limit the multi power setpoint. "
            "For DVCC, use 2705 instead."
        ),
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPERCENTAGE: RegisterDef(
        address=2702,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/MaxDischargePercentage",
        description="ESS max discharge current (fractional)",
        range="0 to 100",
        remarks=(
            "ESS Mode 2 - Max discharge current for ESS control-loop. "
            "The control-loop will use this value to limit the multi power setpoint. "
            "Currently a value < 50% will disable discharge completely. "
            ">=50% allows. Consider using 2704 instead."
        ),
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_ACPOWERSETPOINT_2703: RegisterDef(
        address=2703,
        count=1,
        scale=0.01,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/AcPowerSetPoint",
        description="ESS control loop setpoint",
        range="-3276800 to 3276700",
        remarks=(
            "ESS Mode 2 – Same as 2700, but with a different scale factor. "
            "Meant for values larger than +-32kW."
        ),
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPOWER: RegisterDef(
        address=2704,
        count=1,
        scale=0.1,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/MaxDischargePower",
        description="ESS max discharge current",
        range="-327680 to 327670",
        remarks="ESS Mode 2 – similar to 2702, but as an absolute value instead of a percentage.",
    ),
    Reg.SETTINGS_SETTINGS_SYSTEMSETUP_MAXCHARGECURRENT: RegisterDef(
        address=2705,
        count=1,
        scale=1.0,
        unit="A DC",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/SystemSetup/MaxChargeCurrent",
        description="DVCC system max charge current",
        range="-32768 to 32767",
        remarks="ESS Mode 2 with DVCC – Maximum system charge current. -1 Disables.",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_MAXFEEDINPOWER: RegisterDef(
        address=2706,
        count=1,
        scale=0.01,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/MaxFeedInPower",
        description="Maximum System Grid Feed In",
        range="-3276800 to 3276700",
        remarks=(
            "-1: No limit, >=0: limited system feed-in. "
            "Applies to DC-coupled and AC-coupled feed-in."
        ),
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_OVERVOLTAGEFEEDIN: RegisterDef(
        address=2707,
        count=1,
        scale=1.0,
        unit="0=Don’t feed excess DC-tied PV into grid; 1=Feed excess DC-tied PV into the grid",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/OvervoltageFeedIn",
        description="Feed excess DC-coupled PV into grid",
        range="",
        remarks="Also known as Overvoltage Feed-in",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_PREVENTFEEDBACK: RegisterDef(
        address=2708,
        count=1,
        scale=1.0,
        unit="0=Feed excess AC-tied PV into grid; 1=Don’t feed excess AC-tied PV into the grid",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/PreventFeedback",
        description="Don’t feed excess AC-coupled PV into grid",
        range="",
        remarks="Formerly  called Fronius Zero-Feedin",
    ),
    Reg.SETTINGS_SETTINGS_SYSTEMSETUP_MAXCHARGEVOLTAGE: RegisterDef(
        address=2710,
        count=1,
        scale=10.0,
        unit="V DC",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/SystemSetup/MaxChargeVoltage",
        description="Limit managed battery voltage",
        range="0 to 6553.5",
        remarks="Only used if there is a managed battery in the system",
    ),
    Reg.SETTINGS_SETTINGS_SYSTEMSETUP_ACINPUT1: RegisterDef(
        address=2711,
        count=1,
        scale=1.0,
        unit="0=Unused;1=Grid;2=Genset;3=Shore",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/SystemSetup/AcInput1",
        description="AC input 1 source (for VE.Bus inverter/chargers)",
        range="0 to 65535",
        remarks="For Multi-RS, this is configured on the Inverter/Charger with VictronConnect",
    ),
    Reg.SETTINGS_SETTINGS_SYSTEMSETUP_ACINPUT2: RegisterDef(
        address=2712,
        count=1,
        scale=1.0,
        unit="0=Unused;1=Grid;2=Genset;3=Shore",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/SystemSetup/AcInput2",
        description="AC input 2 source (for VE.Bus inverter/chargers)",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_ACEXPORTLIMIT: RegisterDef(
        address=2713,
        count=1,
        scale=1.0,
        unit="",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/CGwacs/AcExportLimit",
        description="AC export limit when peakshaving",
        range="-32768 to 32767",
        remarks="-1: Disabled",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_ACINPUTLIMIT: RegisterDef(
        address=2714,
        count=1,
        scale=1.0,
        unit="",
        type="int16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/AcInputLimit",
        description="AC import limit when peakshaving",
        range="-32768 to 32767",
        remarks="-1: Disabled",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_ALWAYSPEAKSHAVE: RegisterDef(
        address=2715,
        count=1,
        scale=1.0,
        unit="0=Above minimum SOC only;1=Always",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/CGwacs/AlwaysPeakShave",
        description="Mode for peakshaving",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_RUNWITHOUTGRIDMETER: RegisterDef(
        address=2717,
        count=1,
        scale=1.0,
        unit="0=External meter;1=Inverter/Charger",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/CGwacs/RunWithoutGridMeter",
        description="Grid metering",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_BATTERYLIFE_STATE: RegisterDef(
        address=2900,
        count=1,
        scale=1.0,
        unit=(
            "0=Unused, BL disabled;1=Restarting;2=Self-consumption;"
            "3=Self-consumption;4=Self-consumption;5=Discharge disabled;"
            "6=Force charge;7=Sustain;8=Low Soc Recharge;"
            "9=Keep batteries charged;10=BL Disabled;11=BL Disabled (Low SoC);"
            "12=BL Disabled (Low SOC recharge)"
        ),
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/CGwacs/BatteryLife/State",
        description="ESS BatteryLife state",
        range="",
        remarks="Use value 0 (disable) and 1(enable) for writing only",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_BATTERYLIFE_MINIMUMSOCLIMIT: RegisterDef(
        address=2901,
        count=1,
        scale=10.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/CGwacs/BatteryLife/MinimumSocLimit",
        description="ESS Minimum SoC (unless grid fails)",
        range="",
        remarks="Same as the setting in the GUI",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_HUB4MODE: RegisterDef(
        address=2902,
        count=1,
        scale=1.0,
        unit=(
            "1=ESS with Phase Compensation;2=ESS without phase compensation;"
            "3=Disabled/External Control"
        ),
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/Hub4Mode",
        description="ESS Mode",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_CGWACS_BATTERYLIFE_SOCLIMIT: RegisterDef(
        address=2903,
        count=1,
        scale=10.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Cgwacs/BatteryLife/SocLimit",
        description="ESS BatteryLife SoC limit (read only)",
        range="",
        remarks=(
            "This value is maintained by BatteryLife. "
            "The Active SOC limit is the lower of this value, and register 2901. "
            "Also see "
            "https://www.victronenergy.com/media/pg/Energy_Storage_System/en/"
            "controlling-depth-of-discharge.html#UUID-af4a7478-4b75-68ac-cf3c-"
            "16c381335d1e"
        ),
    ),
    Reg.SETTINGS_SETTINGS_PUMP0_AUTOSTARTENABLED: RegisterDef(
        address=4701,
        count=1,
        scale=1.0,
        unit="0=Disabled;1=Enabled",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Pump0/AutoStartEnabled",
        description="Auto start enabled",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_PUMP0_MODE: RegisterDef(
        address=4702,
        count=1,
        scale=1.0,
        unit="0=Auto;1=On;2=Off",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Pump0/Mode",
        description="Mode",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_PUMP0_STARTVALUE: RegisterDef(
        address=4703,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Pump0/StartValue",
        description="Start value",
        range="0 to 100",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_PUMP0_STOPVALUE: RegisterDef(
        address=4704,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/Pump0/StopValue",
        description="Stop value",
        range="0 to 100",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_BATTERYCAPACITY: RegisterDef(
        address=5420,
        count=1,
        scale=10.0,
        unit="kWh",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/BatteryCapacity",
        description="Battery capacity",
        range="0 to 6553.5",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_FULLCHARGEDURATION: RegisterDef(
        address=5421,
        count=1,
        scale=1.0,
        unit="hour",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/FullChargeDuration",
        description="Full battery charge duration",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_FULLCHARGEINTERVAL: RegisterDef(
        address=5422,
        count=1,
        scale=1.0,
        unit="day",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/FullChargeInterval",
        description="Full battery charge interval",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_MODE: RegisterDef(
        address=5423,
        count=1,
        scale=1.0,
        unit="0=Off;1=Auto;4=Node-RED",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Mode",
        description="Dynamic ESS mode",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_ALLOWGRIDFEEDIN: RegisterDef(
        address=5424,
        count=1,
        scale=1.0,
        unit="0=Not allowed;1=Allowed",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Schedule/0/AllowGridFeedIn",
        description="Allow grid feed-in during this schedule",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_DURATION: RegisterDef(
        address=5425,
        count=1,
        scale=1.0,
        unit="Seconds",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Schedule/0/Duration",
        description="Duration for this schedule",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_RESTRICTIONS: RegisterDef(
        address=5426,
        count=1,
        scale=1.0,
        unit=(
            "0=No restrictions between battery and the grid;"
            "1=Grid to battery energy flow is restricted;"
            "2=Battery to grid energy flow is restricted;"
            "3=No energy flow between battery and grid"
        ),
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Schedule/0/Restrictions",
        description="Active restrictions for this schedule",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_SOC: RegisterDef(
        address=5427,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Schedule/0/Soc",
        description="Target SOC for this schedule",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_START: RegisterDef(
        address=5428,
        count=2,
        scale=1.0,
        unit="",
        type="int32",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Schedule/0/Start",
        description="Unix timestamp when this schedule starts",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SETTINGS_SETTINGS_DYNAMICESS_SCHEDULE_0_STRATEGY: RegisterDef(
        address=5429,
        count=1,
        scale=1.0,
        unit="0=Target SOC;1=Self-consumption;2=Pro battery;3=Pro grid",
        type="uint16",
        writable=True,
        service="com.victronenergy.settings",
        path="/Settings/DynamicEss/Schedule/0/Strategy",
        description="Used strategy for this schedule",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_SERIAL: RegisterDef(
        address=800,
        count=3,
        scale=1.0,
        unit="",
        type="string[6]",
        writable=True,
        service="com.victronenergy.system",
        path="/Serial",
        description="Serial (System)",
        range="12 characters",
        remarks="System value -> MAC address of CCGX (represented as string)",
    ),
    Reg.SYSTEM_RELAY_0_STATE: RegisterDef(
        address=806,
        count=1,
        scale=1.0,
        unit="0=Open;1=Closed",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Relay/0/State",
        description="CCGX Relay 1 state",
        range="0 to 1",
        remarks="",
    ),
    Reg.SYSTEM_RELAY_1_STATE: RegisterDef(
        address=807,
        count=1,
        scale=1.0,
        unit="0=Open;1=Closed",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Relay/1/State",
        description="CCGX Relay 2 state",
        range="0 to 1",
        remarks="Relay 1 is available on Venus GX only.",
    ),
    Reg.SYSTEM_AC_PVONOUTPUT_L1_POWER: RegisterDef(
        address=808,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnOutput/L1/Power",
        description="PV - AC-coupled on output L1",
        range="0 to 65536",
        remarks="Summation of all AC-Coupled PV Inverters on the output",
    ),
    Reg.SYSTEM_AC_PVONOUTPUT_L2_POWER: RegisterDef(
        address=809,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnOutput/L2/Power",
        description="PV - AC-coupled on output L2",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_PVONOUTPUT_L3_POWER: RegisterDef(
        address=810,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnOutput/L3/Power",
        description="PV - AC-coupled on output L3",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_PVONGRID_L1_POWER: RegisterDef(
        address=811,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnGrid/L1/Power",
        description="PV - AC-coupled on input L1",
        range="0 to 65536",
        remarks="Summation of all AC-Coupled PV Inverters on the input",
    ),
    Reg.SYSTEM_AC_PVONGRID_L2_POWER: RegisterDef(
        address=812,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnGrid/L2/Power",
        description="PV - AC-coupled on input L2",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_PVONGRID_L3_POWER: RegisterDef(
        address=813,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnGrid/L3/Power",
        description="PV - AC-coupled on input L3",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_PVONGENSET_L1_POWER: RegisterDef(
        address=814,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnGenset/L1/Power",
        description="PV - AC-coupled on generator L1",
        range="0 to 65536",
        remarks=(
            "Summation of all AC-Coupled PV Inverters on a generator. "
            "Bit theoretic; this will never be used."
        ),
    ),
    Reg.SYSTEM_AC_PVONGENSET_L2_POWER: RegisterDef(
        address=815,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnGenset/L2/Power",
        description="PV - AC-coupled on generator L2",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_PVONGENSET_L3_POWER: RegisterDef(
        address=816,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/PvOnGenset/L3/Power",
        description="PV - AC-coupled on generator L3",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTION_L1_POWER: RegisterDef(
        address=817,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Consumption/L1/Power",
        description="AC Consumption L1",
        range="0 to 65536",
        remarks="Power supplied by system to loads.",
    ),
    Reg.SYSTEM_AC_CONSUMPTION_L2_POWER: RegisterDef(
        address=818,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Consumption/L2/Power",
        description="AC Consumption L2",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTION_L3_POWER: RegisterDef(
        address=819,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Consumption/L3/Power",
        description="AC Consumption L3",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_AC_GRID_L1_POWER: RegisterDef(
        address=820,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Grid/L1/Power",
        description="Grid L1",
        range="-32768 to 32767",
        remarks="Power supplied by Grid to system.",
    ),
    Reg.SYSTEM_AC_GRID_L2_POWER: RegisterDef(
        address=821,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Grid/L2/Power",
        description="Grid L2",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.SYSTEM_AC_GRID_L3_POWER: RegisterDef(
        address=822,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Grid/L3/Power",
        description="Grid L3",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.SYSTEM_AC_GENSET_L1_POWER: RegisterDef(
        address=823,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Genset/L1/Power",
        description="Genset L1",
        range="-32768 to 32767",
        remarks="Power supplied by Genset tot system.",
    ),
    Reg.SYSTEM_AC_GENSET_L2_POWER: RegisterDef(
        address=824,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Genset/L2/Power",
        description="Genset L2",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.SYSTEM_AC_GENSET_L3_POWER: RegisterDef(
        address=825,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/Genset/L3/Power",
        description="Genset L3",
        range="-32768 to 32767",
        remarks="",
    ),
    Reg.SYSTEM_AC_ACTIVEIN_SOURCE: RegisterDef(
        address=826,
        count=1,
        scale=1.0,
        unit="0=Unknown;1=Grid;2=Generator;3=Shore power;240=Not connected",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ActiveIn/Source",
        description="Active input source",
        range="0 to 32768",
        remarks=(
            "0 indicates that there is an active input, but it is not "
            "configured under Settings → System setup."
        ),
    ),
    Reg.SYSTEM_INTERNAL: RegisterDef(
        address=830,
        count=4,
        scale=1.0,
        unit="seconds",
        type="uint64",
        writable=True,
        service="com.victronenergy.system",
        path="INTERNAL",
        description="System time in UTC",
        range="0 to 18446744073709551615",
        remarks="",
    ),
    Reg.SYSTEM_DC_BATTERY_VOLTAGE: RegisterDef(
        address=840,
        count=1,
        scale=10.0,
        unit="V DC",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/Voltage",
        description="Battery Voltage (System)",
        range="0 to 6553.5",
        remarks=(
            "Battery Voltage determined from different measurements. "
            "In order of preference: BMV-voltage (V), Multi-DC-Voltage (CV), "
            "MPPT-DC-Voltage (ScV), Charger voltage"
        ),
    ),
    Reg.SYSTEM_DC_BATTERY_CURRENT: RegisterDef(
        address=841,
        count=1,
        scale=10.0,
        unit="A DC",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/Current",
        description="Battery Current (System)",
        range="-3276.8 to 3276.7",
        remarks="Postive: battery begin charged. Negative: battery being discharged",
    ),
    Reg.SYSTEM_DC_BATTERY_POWER: RegisterDef(
        address=842,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/Power",
        description="Battery Power (System)",
        range="-32768 to 32767",
        remarks="Postive: battery begin charged. Negative: battery being discharged",
    ),
    Reg.SYSTEM_DC_BATTERY_SOC: RegisterDef(
        address=843,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/Soc",
        description="Battery State of Charge (System)",
        range="0 to 100",
        remarks="Best battery state of charge, determined from different measurements.",
    ),
    Reg.SYSTEM_DC_BATTERY_STATE: RegisterDef(
        address=844,
        count=1,
        scale=1.0,
        unit="0=idle;1=charging;2=discharging",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/State",
        description="Battery state (System)",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_DC_BATTERY_CONSUMEDAMPHOURS: RegisterDef(
        address=845,
        count=1,
        scale=-10.0,
        unit="Ah",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/ConsumedAmphours",
        description="Battery Consumed Amphours (System)",
        range="0 to -6553.6",
        remarks="",
    ),
    Reg.SYSTEM_DC_BATTERY_TIMETOGO: RegisterDef(
        address=846,
        count=1,
        scale=0.01,
        unit="s",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Battery/TimeToGo",
        description="Battery Time to Go (System)",
        range="0 to 6553600",
        remarks="Special value: 0 = charging",
    ),
    Reg.SYSTEM_DC_PV_POWER: RegisterDef(
        address=850,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Pv/Power",
        description="PV - DC-coupled power",
        range="0 to 65536",
        remarks="Summation of output power of all connected Solar Chargers",
    ),
    Reg.SYSTEM_DC_PV_CURRENT: RegisterDef(
        address=851,
        count=1,
        scale=10.0,
        unit="A DC",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Pv/Current",
        description="PV - DC-coupled current",
        range="-3276.8 to 3276.7",
        remarks="Summation of output current of all connected Solar Chargers",
    ),
    Reg.SYSTEM_DC_CHARGER_POWER: RegisterDef(
        address=855,
        count=1,
        scale=1.0,
        unit="W",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Charger/Power",
        description="Charger power",
        range="0 to 65536",
        remarks="",
    ),
    Reg.SYSTEM_DC_SYSTEM_POWER: RegisterDef(
        address=860,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/System/Power",
        description="DC System Power",
        range="-32768 to 32767",
        remarks="Power supplied by Battery to system.",
    ),
    Reg.SYSTEM_DC_VEBUS_CURRENT: RegisterDef(
        address=865,
        count=1,
        scale=10.0,
        unit="A DC",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Vebus/Current",
        description="VE.Bus charge current (System)",
        range="-3276.8 to 3276.7",
        remarks="Current flowing from the Multi to the dc system. Negative: the other way around.",
    ),
    Reg.SYSTEM_DC_VEBUS_POWER: RegisterDef(
        address=866,
        count=1,
        scale=1.0,
        unit="W",
        type="int16",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/Vebus/Power",
        description="VE.Bus charge power (System)",
        range="-32768 to 32767",
        remarks=(
            "System value. Positive: power flowing from the Multi to the dc system. "
            "Negative: the other way around."
        ),
    ),
    Reg.SYSTEM_DC_INVERTERCHARGER_CURRENT: RegisterDef(
        address=868,
        count=2,
        scale=10.0,
        unit="",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/InverterCharger/Current",
        description="Inverter/Charger current",
        range="-214748364.8 to 214748364.8",
        remarks="",
    ),
    Reg.SYSTEM_DC_INVERTERCHARGER_POWER: RegisterDef(
        address=870,
        count=2,
        scale=1.0,
        unit="",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Dc/InverterCharger/Power",
        description="Inverter/Charger power",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTIONONINPUT_L1_POWER: RegisterDef(
        address=872,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ConsumptionOnInput/L1/Power",
        description="Power between meter and inverter/charger, L1",
        range="-2147483648 to 2147483648",
        remarks="This is the power shown on the overview in the Loads box",
    ),
    Reg.SYSTEM_AC_CONSUMPTIONONINPUT_L2_POWER: RegisterDef(
        address=874,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ConsumptionOnInput/L2/Power",
        description="Power between meter and inverter/charger, L2",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTIONONINPUT_L3_POWER: RegisterDef(
        address=876,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ConsumptionOnInput/L3/Power",
        description="Power between meter and inverter/charger, L3",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTIONONOUTPUT_L1_POWER: RegisterDef(
        address=878,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ConsumptionOnOutput/L1/Power",
        description="Power on output of inverter/charger, L1",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTIONONOUTPUT_L2_POWER: RegisterDef(
        address=880,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ConsumptionOnOutput/L2/Power",
        description="Power on output of inverter/charger, L2",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SYSTEM_AC_CONSUMPTIONONOUTPUT_L3_POWER: RegisterDef(
        address=882,
        count=2,
        scale=1.0,
        unit="W",
        type="int32",
        writable=True,
        service="com.victronenergy.system",
        path="/Ac/ConsumptionOnOutput/L3/Power",
        description="Power on output of inverter/charger, L3",
        range="-2147483648 to 2147483648",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_ACTIVE: RegisterDef(
        address=5400,
        count=1,
        scale=1.0,
        unit="0=Off;1=Active",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/Active",
        description="Dynamic ESS state",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_ALLOWGRIDFEEDIN: RegisterDef(
        address=5401,
        count=1,
        scale=1.0,
        unit="0=Not allowed;1=Allowed",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/AllowGridFeedIn",
        description="Allow grid feed-in",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_AVAILABLE: RegisterDef(
        address=5402,
        count=1,
        scale=1.0,
        unit="0=System is not capable of doing DynamicEss;1=System is capable of doing DynamicEss",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/Available",
        description="Is Dynamic ESS available",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_CHARGERATE: RegisterDef(
        address=5403,
        count=1,
        scale=0.1,
        unit="",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/ChargeRate",
        description="Calculated rate of charge/discharge",
        range="0 to 655350",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_ERRORCODE: RegisterDef(
        address=5404,
        count=1,
        scale=1.0,
        unit=(
            "0=No error;1=No ESS;2=ESS mode;3=No matching schedule;"
            "4=SOC low;5=Battery capacity not configured"
        ),
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/ErrorCode",
        description="Error",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_RESTRICTIONS: RegisterDef(
        address=5405,
        count=1,
        scale=1.0,
        unit=(
            "0=No restrictions between battery and the grid;"
            "1=Grid to battery energy flow is restricted;"
            "2=Battery to grid energy flow is restricted;"
            "3=No energy flow between battery and grid"
        ),
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/Restrictions",
        description="Active restrictions",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_STRATEGY: RegisterDef(
        address=5406,
        count=1,
        scale=1.0,
        unit="0=Target SOC;1=Self-consumption;2=Pro battery;3=Pro grid",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/Strategy",
        description="Used strategy for current time slot",
        range="0 to 65535",
        remarks="",
    ),
    Reg.SYSTEM_DYNAMICESS_TARGETSOC: RegisterDef(
        address=5407,
        count=1,
        scale=1.0,
        unit="%",
        type="uint16",
        writable=True,
        service="com.victronenergy.system",
        path="/DynamicEss/TargetSoc",
        description="The set target SOC for this time slot",
        range="0 to 65535",
        remarks="",
    ),
}
