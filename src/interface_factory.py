"""
Interface Factory - Centralized creation and initialization of interfaces with integrated startup validation.

This factory pattern centralizes interface instantiation and error handling, reducing code duplication
in the main application and providing a consistent approach to startup error collection.
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)


class InterfaceFactory:
    """
    Factory for creating and initializing interfaces with integrated startup validation.
    
    Handles:
    - Interface instantiation
    - Error catching and registration with startup validator
    - Fallback creation for non-critical failures
    - Categorization of critical vs non-critical errors
    """

    def __init__(self, startup_validator):
        """
        Initialize the factory with a startup validator.
        
        Args:
            startup_validator: StartupValidator instance to register errors with
        """
        self.validator = startup_validator
        self.created_interfaces = {}

    def create_load_interface(
        self,
        config: Dict[str, Any],
        time_frame_base: int,
        time_zone: pytz.timezone,
        request_timeout: int = 10,
        critical: bool = True,
    ):
        """
        Create LoadInterface with error handling.
        
        Args:
            config: Load configuration dictionary
            time_frame_base: Base time frame in seconds
            time_zone: Timezone for timestamps
            request_timeout: Request timeout in seconds
            critical: Whether interface is critical (stops startup on failure)
            
        Returns:
            LoadInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="load_interface",
            category="connectivity",
            critical=critical,
            title="Load interface unavailable",
            error_message="Failed to retrieve load data from HomeAssistant",
            config_link="#load",
            creator_func=lambda: self._import_and_create(
                "interfaces.load_interface",
                "LoadInterface",
                config,
                time_frame_base,
                time_zone,
                request_timeout=request_timeout,
            ),
        )

    def create_battery_interface(
        self,
        config: Dict[str, Any],
        load_interface,
        time_zone: pytz.timezone,
        base_control,
        request_timeout: int = 10,
        critical: bool = True,
    ):
        """
        Create BatteryInterface with error handling.
        
        Args:
            config: Battery configuration dictionary
            load_interface: LoadInterface instance (dependency)
            time_zone: Timezone for timestamps
            base_control: BaseControl instance (dependency)
            request_timeout: Request timeout in seconds
            critical: Whether interface is critical
            
        Returns:
            BatteryInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="battery_interface",
            category="connectivity",
            critical=critical,
            title="Battery sensor unreachable",
            error_message="Failed to retrieve battery data from HomeAssistant",
            config_link="#battery",
            creator_func=lambda: self._import_and_create(
                "interfaces.battery_interface",
                "BatteryInterface",
                config,
                on_bat_max_changed=None,
                load_interface=load_interface,
                timezone=time_zone,
                base_control=base_control,
                request_timeout=request_timeout,
            ),
        )

    def create_price_interface(
        self,
        config: Dict[str, Any],
        time_frame_base: int,
        time_zone: pytz.timezone,
        critical: bool = False,
    ):
        """
        Create PriceInterface with error handling.
        
        Args:
            config: Price configuration dictionary
            time_frame_base: Base time frame in seconds
            time_zone: Timezone for timestamps
            critical: Whether interface is critical (non-critical by default)
            
        Returns:
            PriceInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="price_interface",
            category="connectivity",
            critical=critical,
            title="Price source unreachable",
            error_message="Failed to retrieve price data",
            additional_message=" Fallback prices will be used.",
            config_link="#price",
            creator_func=lambda: self._import_and_create(
                "interfaces.price_interface",
                "PriceInterface",
                config,
                time_frame_base,
                time_zone,
            ),
        )

    def create_pv_interface(
        self,
        pv_forecast_source: Dict[str, Any],
        pv_forecast: list,
        time_frame_base: int,
        evcc_config: Dict[str, Any],
        eos_source: str,
        time_zone_str: str,
        critical: bool = False,
    ):
        """
        Create PvInterface with error handling.
        
        Args:
            pv_forecast_source: PV forecast source configuration
            pv_forecast: List of PV forecast configurations
            time_frame_base: Base time frame in seconds
            evcc_config: EVCC configuration
            eos_source: EOS source type
            time_zone_str: Timezone string
            critical: Whether interface is critical (non-critical by default)
            
        Returns:
            PvInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="pv_interface",
            category="connectivity",
            critical=critical,
            title="PV forecast unavailable",
            error_message="Failed to retrieve PV forecast data",
            additional_message=" System continues without PV data.",
            config_link="#pv_forecast_source",
            creator_func=lambda: self._import_and_create(
                "interfaces.pv_interface",
                "PvInterface",
                pv_forecast_source,
                pv_forecast,
                time_frame_base,
                evcc_config,
                eos_source == "eos_server",
                time_zone_str,
            ),
        )

    def create_mqtt_interface(
        self,
        config: Dict[str, Any],
        critical: bool = False,
    ):
        """
        Create MqttInterface with error handling.
        
        Args:
            config: MQTT configuration dictionary
            critical: Whether interface is critical (non-critical by default)
            
        Returns:
            MqttInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="mqtt_interface",
            category="connectivity",
            critical=critical,
            title="MQTT broker unreachable",
            error_message="Failed to connect to MQTT broker",
            additional_message=" System continues but MQTT control is unavailable.",
            config_link="#mqtt",
            creator_func=lambda: self._import_and_create(
                "interfaces.mqtt_interface",
                "MqttInterface",
                config_mqtt=config,
                on_mqtt_command=None,
            ),
        )

    def create_evcc_interface(
        self,
        evcc_url: str,
        ext_bat_mode: bool,
        critical: bool = False,
    ):
        """
        Create EvccInterface with error handling.
        
        Args:
            evcc_url: EVCC URL
            ext_bat_mode: Extended battery mode flag
            critical: Whether interface is critical (non-critical by default)
            
        Returns:
            EvccInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="evcc_interface",
            category="connectivity",
            critical=critical,
            title="EVCC unreachable",
            error_message="Failed to connect to EVCC",
            additional_message=" System continues but EVCC data is unavailable.",
            config_link="#evcc",
            creator_func=lambda: self._import_and_create(
                "interfaces.evcc_interface",
                "EvccInterface",
                url=evcc_url,
                ext_bat_mode=ext_bat_mode,
                update_interval=10,
                on_charging_state_change=None,
            ),
        )

    def create_inverter_interface(
        self,
        config: Dict[str, Any],
        critical: bool = True,
    ):
        """
        Create inverter interface using factory with error handling.
        
        Args:
            config: Inverter configuration dictionary
            critical: Whether interface is critical (stops startup on failure)
            
        Returns:
            Inverter interface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="inverter_interface",
            category="initialization",
            critical=critical,
            title="Inverter type not supported or not configured",
            error_message="Configured inverter type is not supported",
            config_link="#inverter.type",
            creator_func=lambda: self._import_and_create(
                "interfaces.inverters",
                "create_inverter",
                config,
            ),
        )

    def create_optimization_interface(
        self,
        config: Dict[str, Any],
        time_frame_base: int,
        timezone: pytz.timezone,
        critical: bool = True,
    ):
        """
        Create OptimizationInterface with error handling.
        
        Args:
            config: Optimization configuration dictionary
            time_frame_base: Base time frame in seconds
            timezone: Timezone for timestamps
            critical: Whether interface is critical (stops startup on failure)
            
        Returns:
            OptimizationInterface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        return self._create_interface(
            component_name="optimization_interface",
            category="initialization",
            critical=critical,
            title="Optimizer backend failed to initialize",
            error_message="Failed to load optimizer configuration",
            config_link="#eos",
            creator_func=lambda: self._import_and_create(
                "interfaces.optimization_interface",
                "OptimizationInterface",
                config=config,
                time_frame_base=time_frame_base,
                timezone=timezone,
            ),
        )

    def _create_interface(
        self,
        component_name: str,
        category: str,
        critical: bool,
        title: str,
        error_message: str,
        config_link: str,
        creator_func,
        additional_message: str = "",
    ):
        """
        Generic interface creation with error handling and validation.
        
        Args:
            component_name: Name of the component for logging
            category: Error category (initialization, configuration, connectivity)
            critical: Whether failure should stop startup
            title: User-friendly error title
            error_message: Base error message
            config_link: Link to configuration section
            creator_func: Function that creates the interface
            additional_message: Additional context for the error message
            
        Returns:
            Interface instance or None if non-critical and failed
            
        Raises:
            Exception if critical interface fails
        """
        try:
            interface = creator_func()
            
            # For inverter interface, also initialize it if not None
            if component_name == "inverter_interface" and interface is not None:
                try:
                    interface.initialize()
                except Exception as e:
                    raise Exception(f"Inverter initialization failed: {str(e)}")
            
            self.created_interfaces[component_name] = interface
            logger.info("[Factory] Successfully created %s", component_name)
            return interface
            
        except Exception as e:
            error_detail = str(e)
            full_message = f"{error_message}: {error_detail}{additional_message}"
            
            logger.exception(
                "[Factory] Failed to create %s (critical=%s): %s",
                component_name,
                critical,
                full_message,
            )
            
            # Register error with validator
            self.validator.add_error(
                category=category,
                component=component_name,
                severity="error",
                title=title,
                message=full_message,
                action_required=critical,
                config_link=config_link,
            )
            
            # Handle critical vs non-critical failures
            if critical:
                raise  # Re-raise to stop startup
            
            # For non-critical, return None but allow continued startup
            logger.warning(
                "[Factory] %s failed but is non-critical, continuing startup",
                component_name,
            )
            return None

    @staticmethod
    def _import_and_create(module_name: str, class_name: str, *args, **kwargs):
        """
        Dynamically import a module and class, then instantiate it.
        
        Args:
            module_name: Module path (e.g., 'interfaces.load_interface')
            class_name: Class name to instantiate
            *args: Positional arguments for class constructor
            **kwargs: Keyword arguments for class constructor
            
        Returns:
            Instance of the class
            
        Raises:
            ImportError or any exception from class instantiation
        """
        import importlib
        
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        return cls(*args, **kwargs)
