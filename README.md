<table style="border:none;">
  <tr>
    <td width="130"><img src="docs/assets/images/logo.png" alt="EOS Connect Logo" width="120"/></td>
    <td style="vertical-align: middle;"><h1 style="margin:0; padding-left:10px;">EOS Connect</h1></td>
  </tr>
</table>

**For full documentation, guides, and configuration details, visit:**  
[https://ohAnd.github.io/EOS_connect/](https://ohAnd.github.io/EOS_connect/)

---

## Overview
EOS Connect is an open-source tool for intelligent energy management and optimization. It acts as the orchestration layer between your energy hardware (inverters, batteries, PV forecasts) and external optimization engines. EOS Connect is an integration and control platform—not an optimizer. Optimization calculations are performed by external servers:
- [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS)
- [EVopt](https://github.com/thecem/hassio-evopt)

EOS Connect fetches real-time and forecast data, processes it via your chosen optimizer, and controls devices to optimize your energy usage and costs.

---

## Key Features
- **Automated Energy Optimization:** Uses real-time and forecast data to maximize self-consumption and minimize grid costs.
- **Battery and Inverter Management:** Charge/discharge control, grid/PV modes, dynamic charging curves.
- **Integration with Smart Home Platforms:** Home Assistant (MQTT auto discovery), OpenHAB, EVCC, and MQTT for seamless data exchange and automation.
- **Dynamic Web Dashboard:** Live monitoring, manual control, and visualization of your energy system.
- **Cost Optimization:** Aligns energy usage with dynamic electricity prices (Tibber, smartenergy.at, Stromligning.dk) with hourly or quarterly distribution.
- **Smart Price Prediction:** Energyforecast.de integration automatically learns your grid fees and taxes to provide accurate price predictions when your primary source lacks tomorrow's prices. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#energyforecast)
- **Dynamic PV Override:** Automatically allows discharge when solar production exceeds load, preventing unwanted grid input during cloud shadows. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#dyn-override)
- **Flexible Configuration:** Easy to set up and extend for a wide range of energy systems and user needs.

---


## How It Works
EOS Connect periodically collects:
- Local energy consumption data
- PV solar forecasts for the next 48 hours
- Upcoming energy prices

It sends this data to the optimizer (EOS or EVopt), which returns a prediction and recommended control strategy. EOS Connect then applies these controls to your devices (inverter, battery, EVCC, etc.). All scheduling and timing is managed by EOS Connect.

<div align="center">
  <img src="docs\assets\images\eos_connect_flow.png" alt="EOS Connect process flow" width="450"/>
  <br>
  <sub><i>Figure: EOS Connect process flow</i></sub>
</div>

Supported data sources and integrations:

- **Home Assistant:** MQTT publishing (dashboard, control, auto-discovery) and direct API integration for sensor/entity data collection.
- **OpenHAB:** MQTT publishing (dashboard, control, auto-discovery via MQTT binding) and direct API integration for item data collection.
- **EVCC:** Monitors and controls EV charging modes and states.
- **Inverter Interfaces:** Victron MultiPlus (3-phase ESS via Modbus/TCP), Fronius GEN24 (with automatic firmware detection), legacy fallback, generic Home Assistant inverter control (e.g., Marstek, Sungrow, Goodwe), and more via MQTT/web API/EVCC external inverter control.


## Quick Start

### Home Assistant Installation (Recommended)
1. **Requirements:**
   - Home Assistant (latest version recommended)
   - EOS or EVopt server (can be installed as part of the setup; see below)

2. **Option A: Install EOS Connect Add-on:**
   - Add the [ohAnd/ha_addons](https://github.com/ohAnd/ha_addons) repository to your Home Assistant add-on store.
   - Install the **EOS Connect** add-on from the store.
  
3. **Option B: Install EOS Connect Add-on:**
   - If you want to use EOS as your optimization backend, add the [Duetting/ha_eos_addon](https://github.com/Duetting/ha_eos_addon) or [thecem/ha_eos_addon](https://github.com/thecem/ha_eos_addon) repository to your Home Assistant add-on store and install the EOS add-on, or ensure your EOS server is running and reachable.
   - If you prefer the lightweight EVopt backend, install [thecem/hassio-evopt](https://github.com/thecem/hassio-evopt) and make sure it is running.

4. **Configure:**
    - On first start, a **Setup Wizard** guides you through initial configuration via the web UI.
    - All settings are managed through the EOS Connect web interface — no manual editing of config files required.
    - The HA addon only handles bootstrap settings (web port, timezone, log level). All other configuration is stored in EOS Connect's built-in database.
    - See the [user-guide/configuration](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html) for full details.

5. **Start & Access:**
    - Start the EOS Connect add-on from the Home Assistant UI.
    - Open `http://homeassistant.local:8081` (or your HA IP) to view the dashboard.

<div align="center">
  <img src="docs/assets/images/screenshot_0_1_20.png" alt="EOS Connect dashboard screenshot" width="600"/>
  <br>
  <sub><i>Figure: EOS Connect dashboard</i></sub>
</div>

**Note for Proxmox / VM Users:**
If the add-on crashes with a Segmentation Fault on startup, your VM might be using a generic CPU type.
- Go to your VM Settings > Hardware > Processor.
- Change Type from `kvm64` (default) to `host`.
- Restart the VM.

This allows the add-on to correctly see and use your physical CPU's instructions.

---

**Other Installation Options:**
- Docker, manual, and advanced setups are supported. See the [docs](https://ohAnd.github.io/EOS_connect/user-guide/index.html) for details.

---

## Configuration

EOS Connect uses a **web-based configuration system**. All settings are managed through the built-in web UI at `http://localhost:8081`.

### First Start (Setup Wizard)
On first launch, a **Setup Wizard** guides you through the essential configuration steps:
1. Data source (Home Assistant / OpenHAB / default)
2. Optimization backend (EOS Server / EVopt)
3. Battery, price, PV forecast, and inverter settings

After the wizard completes, restart EOS Connect to apply the settings.

### Bootstrap Config (`config.yaml`)
Only 3 infrastructure settings live in `config.yaml` — everything else is stored in the database and managed via the web UI:

```yaml
# config.yaml — bootstrap settings only
eos_connect_web_port: 8081  # Web server port
time_zone: Europe/Berlin    # System time zone
log_level: info             # Log level: debug, info, warning, error
```

> **Upgrading from an older version?** On first start, EOS Connect automatically migrates your existing `config.yaml` settings into the database. After migration, you can reduce `config.yaml` to just the bootstrap keys above.

### Changing Configuration
- Open `http://localhost:8081` and click the gear icon to access the configuration page
- Changes marked as **"hot-reloadable"** (e.g., feed-in price, SOC limits) take effect immediately
- Other changes require a restart (the UI shows which fields need restart)
---

## Troubleshooting & Advanced Configuration
For troubleshooting and advanced configuration, see the [docs](https://ohAnd.github.io/EOS_connect/).

---

## Support & Sponsoring
If you find this project useful and would like to support its development, please consider sponsoring:
[https://github.com/sponsors/ohAnd](https://github.com/sponsors/ohAnd)

## Contributing
Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License
MIT License - see [LICENSE](LICENSE) for details.
