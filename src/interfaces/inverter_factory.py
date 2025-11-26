import logging
from typing import Type
from src.interfaces.base_inverter import BaseInverter

# Importiere alle Inverter-Klassen
from src.interfaces.inverter_victron import VictronInverter
from src.interfaces.inverter_fronius_v1 import FroniusInverterV1
from src.interfaces.inverter_fronius_v3 import FroniusInverterV2


logger = logging.getLogger(__name__)

# Aktiver, modern unterstützter Inverter
# Mapping: Config-String → Inverterklasse
INVERTER_TYPES: dict[str, Type[BaseInverter]] = {
    "victron": VictronInverter,
    "fronius_gen24_v2": FroniusInverterV2,
}

# Veraltete, technisch ersetzte Inverter
# Mapping: Config-String → Inverterklasse
LEGACY_INVERTER_TYPES: dict[str, Type[BaseInverter]] = {
    "fronius_gen24_legacy": FroniusInverterV1,
}


def create_inverter(config: dict) -> BaseInverter:
    inverter_type = config.get("type", "").lower()

    # 1) Neuer, moderner Inverter
    if inverter_type in INVERTER_TYPES:

        cls = INVERTER_TYPES[inverter_type]
        logger.info(
            f"[Factory] Initialisiere modernen Inverter '{inverter_type}' ({cls.__name__})"
        )
        return cls(config)

    # 3) Alter Inverter → darf weiterhin genutzt werden
    if inverter_type in LEGACY_INVERTER_TYPES:
        cls = LEGACY_INVERTER_TYPES[inverter_type]
        logger.warning(
            f"[Factory] Initialisiere *alten* Inverter '{inverter_type}' "
            f"({cls.__name__}). Bitte erwäge ein Update der Hardware."
        )
        return cls(config)

    # 4) Unbekannter Typ
    raise ValueError(
        f"Unbekannter Invertertyp: '{inverter_type}'. " "Bitte prüfe deine config.yaml."
    )
