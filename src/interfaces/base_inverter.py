from abc import ABC, abstractmethod
import logging

logger = logging.getLogger("__main__").getChild("BaseInverter")
logger.setLevel(logging.INFO)


class BaseInverter(ABC):
    """Abstrakte Basisklasse für verschiedene Wechselrichter-Typen."""

    def __init__(self, config: dict):
        # ✔ komplette Config speichern (für Tests & spätere Erweiterungen)
        self.config = config

        # ✔ weiterhin einzelne Werte extrahieren
        self.address = config.get("address")
        self.user = config.get("user", "customer").lower()
        self.password = config.get("password", "")
        self.max_grid_charge_rate = config.get("max_grid_charge_rate")
        self.max_pv_charge_rate = config.get("max_pv_charge_rate")

        self.is_authenticated = False
        self.inverter_type = self.__class__.__name__

        logger.info(f"[{self.inverter_type}] Initialized for {self.address}")

    # --- Optionale Authentifizierung ---

    @abstractmethod
    def initialize(self):
        """Heavy initialization (API calls)."""
        pass

    def authenticate(self) -> bool:
        """
        Optionale Authentifizierung.
        Standardmäßig tut diese Methode nichts und gibt True zurück.
        Subklassen können sie überschreiben, wenn sie Auth benötigen.
        """
        logger.debug(f"[{self.inverter_type}] No authentication required")
        self.is_authenticated = True
        return True

    # --- Pflichtmethoden für alle Inverter ---

    @abstractmethod
    def set_battery_mode(self, mode: str) -> bool:
        """Setzt den Batteriemodus (z. B. normal, hold, charge)."""
        pass

    # --- EOS Connect Helfer ---

    @abstractmethod
    def set_mode_avoid_discharge(self) -> bool:
        """Vermeidet Entladung (Hold Mode)"""
        return self.set_battery_mode("hold")

    @abstractmethod
    def set_mode_allow_discharge(self) -> bool:
        """Erlaubt Entladung (Normal Mode)"""
        return self.set_battery_mode("normal")

    @abstractmethod
    def set_allow_grid_charging(self, value: bool):
        pass

    @abstractmethod
    def get_battery_info(self) -> dict:
        """Liest aktuelle Batterieinformationen."""
        pass

    @abstractmethod
    def fetch_inverter_data(self) -> dict:
        """Liest aktuelle Inverterdaten."""
        pass

    @abstractmethod
    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        """
        Force charge mode with specific power.
        Jede Subklasse muss diese Methode implementieren.
        """
        pass

    @abstractmethod
    def connect_inverter(self) -> bool:
        """
        Establishes a connection to the inverter.

        This method is required to be implemented by all subclasses.
        It should return True if the connection was successful, False otherwise.
        """
        pass

    @abstractmethod
    def disconnect_inverter(self) -> bool:
        """
        Disconnect from the inverter.

        This method is required to be implemented by all subclasses.
        It should return True if the disconnection was successful, False otherwise.
        """
        pass

    # --- Gemeinsame Utility-Methoden ---

    def disconnect(self):
        """Session schließt sich selbst."""
        logger.info(f"[{self.inverter_type}] Session closed")

    def shutdown(self):
        """Standard-Shutdown (kann überschrieben werden)."""
        self.disconnect()
