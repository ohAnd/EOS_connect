# Inverter Integration Guide

## Adding a New Inverter Type

Follow these steps to integrate a new inverter (e.g., SMA, SolarEdge, Huawei):

### 1. Create Your Inverter Class

Create a new file `inverter_<brand>.py` in this folder:

```python
from ..inverter_base import BaseInverter
import logging

logger = logging.getLogger("__main__").getChild("YourBrand")

class YourBrandInverter(BaseInverter):
    def __init__(self, config):
        """Initialize with config from yaml."""
        super().__init__(config)
        # Add your specific initialization here
        self.api_endpoint = config.get("api_endpoint", "")
        
    def initialize(self):
        """Heavy initialization - API calls, auth, config loading."""
        # Load firmware, authenticate, get initial state
        pass
    
    def connect_inverter(self) -> bool:
        """Establish connection to inverter."""
        return True
    
    def disconnect_inverter(self) -> bool:
        """Close connection."""
        return True
    
    def set_battery_mode(self, mode: str) -> bool:
        """Set battery mode: 'normal', 'hold', 'charge'."""
        pass
    
    def set_mode_avoid_discharge(self) -> bool:
        """Prevent battery discharge (hold mode)."""
        return self.set_battery_mode("hold")
    
    def set_mode_allow_discharge(self) -> bool:
        """Allow battery discharge (normal mode)."""
        return self.set_battery_mode("normal")
    
    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        """Force charge from grid with specified power."""
        pass
    
    def set_allow_grid_charging(self, value: bool):
        """Enable/disable grid charging."""
        pass
    
    def get_battery_info(self) -> dict:
        """Return battery status: SOC, power, voltage, etc."""
        return {"soc": 50, "power": 0}
    
    def fetch_inverter_data(self) -> dict:
        """Return inverter telemetry: temps, fan speeds, etc."""
        return {}
```

### 2. Register in Factory

Edit `inverter_factory.py`:

```python
from .inverter_yourbrand import YourBrandInverter

INVERTER_TYPES: dict[str, Type[BaseInverter]] = {
    "victron": VictronInverter,
    "fronius_gen24": FroniusV2,
    "yourbrand": YourBrandInverter,  # Add here
}
```

### 3. Export in Package

Edit `__init__.py`:

```python
from .inverter_yourbrand import YourBrandInverter

__all__ = [
    "BaseInverter",
    "create_inverter",
    "FroniusLegacy",
    "FroniusV2",
    "VictronInverter",
    "YourBrandInverter",  # Add here
]
```

### 4. Update Config

Users can now use it in `config.yaml`:

```yaml
inverter:
  type: "yourbrand"
  address: "192.168.1.100"
  api_endpoint: "https://api.yourbrand.com"
  # ... other settings
```

### 5. Add Tests

Create `tests/interfaces/test_inverter_yourbrand.py`:

```python
from src.interfaces.inverters import create_inverter

def test_yourbrand_creation():
    config = {"type": "yourbrand", "address": "192.168.1.1"}
    inverter = create_inverter(config)
    assert inverter.address == "192.168.1.1"
```

## Architecture Overview

```
inverter_base.py          ← Abstract base class (contract)
    ↓
inverters/
    ├── inverter_factory.py   ← Factory (type selection)
    ├── fronius_legacy.py     ← Implementation example
    ├── fronius_v2.py         ← Implementation example
    ├── victron.py            ← Implementation example
    └── inverter_yourbrand.py ← Your new integration
```

## Key Requirements

### Must Implement (Abstract Methods)

These methods **must** be implemented or runtime errors will occur:

- `initialize()` - Heavy setup (API calls, auth)
- `connect_inverter()` / `disconnect_inverter()`
- `set_battery_mode(mode)` - Core battery control
- `set_mode_avoid_discharge()` / `set_mode_allow_discharge()`
- `set_mode_force_charge(charge_power_w)`
- `set_allow_grid_charging(value)`
- `get_battery_info()` - Battery status dict
- `fetch_inverter_data()` - Inverter telemetry dict

### Inherited from Base

These are optional—base class provides defaults:

- `authenticate()` - Returns True by default
- `shutdown()` - Calls `disconnect()` by default
- `disconnect()` - Logs closure
- `supports_extended_monitoring()` - Returns False by default. Override to return True if your inverter provides extended monitoring data (temperature sensors, fan control, etc.)
- `api_set_max_pv_charge_rate(max_pv_charge_rate: int)` - No-op by default. Override if your inverter supports dynamic PV charge rate limiting (e.g., Fronius Gen24). This method is called during optimization to limit PV charging power

### Config Structure

All inverters receive a dict with at least:

```python
{
    "type": "inverter_type",
    "address": "192.168.1.100",
    "user": "customer",
    "password": "secret",
    "max_grid_charge_rate": 3000,
    "max_pv_charge_rate": 5000
}
```

These are stored in `self.config` and common ones extracted:
- `self.address`
- `self.user`
- `self.password`
- `self.max_grid_charge_rate`
- `self.max_pv_charge_rate`

Add your own as needed!

## Best Practices

1. **Use logging**: `logger.info()`, `logger.debug()`, `logger.error()`
2. **Handle errors gracefully**: Return False/None on failure, log details
3. **Keep `__init__` light**: Heavy work goes in `initialize()`
4. **Document API quirks**: Comment firmware versions, endpoint changes
5. **Test with real hardware**: Mock tests are good, real integration is better
6. **Follow existing patterns**: Check `fronius_v2.py` for structure

## Example: Full Implementation

See `fronius_v2.py` or `fronius_legacy.py` for complete, production-ready examples with:
- Digest authentication
- API version detection
- Config backup/restore
- Error handling
- Retry logic

---

**Questions?** Check existing implementations or ask in GitHub discussions!
