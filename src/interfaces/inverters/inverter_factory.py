"""Factory for creating inverter instances based on configuration."""

import logging
from typing import Type
from ..inverter_base import BaseInverter  # pylint: disable=relative-beyond-top-level

# Import all inverter implementations directly from their modules
from .fronius_legacy import FroniusLegacy
from .fronius_v2 import FroniusV2
from .victron import VictronInverter
from .null_inverter import NullInverter


logger = logging.getLogger(__name__)

# Active, modern supported inverters
# Mapping: Config-String → Inverter class
INVERTER_TYPES: dict[str, Type[BaseInverter]] = {
    "victron": VictronInverter,
    "fronius_gen24": FroniusV2,
    "evcc": NullInverter,  # EVCC handles control externally
    "default": NullInverter,  # Display-only mode
}

# Deprecated, technically replaced inverters
# Mapping: Config-String → Inverter class
LEGACY_INVERTER_TYPES: dict[str, Type[BaseInverter]] = {
    "fronius_gen24_legacy": FroniusLegacy,
    "fronius_gen24_v2": FroniusV2,  # Deprecated name, maps to same modern class
}


def create_inverter(config: dict) -> BaseInverter:
    """
    Factory function to create inverter instances based on configuration.

    Args:
        config: Dictionary containing 'type' key and inverter-specific configuration

    Returns:
        Configured inverter instance

    Raises:
        ValueError: If inverter type is unknown
    """
    inverter_type = config.get("type", "").lower()

    # 1) Modern, actively supported inverter
    if inverter_type in INVERTER_TYPES:
        cls = INVERTER_TYPES[inverter_type]
        logger.info(
            "[Factory] Creating modern inverter '%s' (%s)",
            inverter_type,
            cls.__name__,
        )
        return cls(config)

    # 2) Legacy inverter - still supported but deprecated
    if inverter_type in LEGACY_INVERTER_TYPES:
        cls = LEGACY_INVERTER_TYPES[inverter_type]
        logger.warning(
            "[Factory] Creating legacy inverter '%s' (%s). "
            "Consider updating to a modern type for future compatibility.",
            inverter_type,
            cls.__name__,
        )
        return cls(config)

    # 3) Unknown type
    supported = list(INVERTER_TYPES.keys()) + list(LEGACY_INVERTER_TYPES.keys())
    raise ValueError(
        f"Unknown inverter type: '{inverter_type}'. "
        f"Supported types: {', '.join(supported)}"
    )
