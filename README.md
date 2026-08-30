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
EOS Connect is a comprehensive energy management and optimization platform. While it remains a flexible orchestration layer between your hardware and various optimization engines, it has evolved from a pure "data gateway" into a full-featured, self-contained optimization solution.

EOS Connect now ships with a **built-in MILP optimizer** (`local_evopt`) — providing a complete, high-performance energy management system out of the box. For specialized needs, it maintains its open nature by allowing connections to external backends:
- **Built-in (Recommended):** [local_evopt](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#local-evopt) — A high-performance, local optimizer based on [evcc-io/optimizer](https://github.com/evcc-io/optimizer).
- **External:** [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS) or [EVopt](https://github.com/thecem/hassio-evopt).

EOS Connect fetches real-time and forecast data (solar, prices), runs the integrated optimization (or delegates it), and automatically controls your devices to maximize self-consumption and minimize grid costs.

---

## Key Features
- **All-in-One Optimization Solution:** No external servers required for standard energy optimization.
- **Privacy & Reliability:** With `local_evopt`, all calculations happen on your device, ensuring faster response times and no dependency on external network reachability.
- **Automated Energy Management:** Uses real-time and forecast data into a cohesive control strategy to maximize self-consumption.
- **Battery and Inverter Management:** Precise charge/discharge control, grid/PV modes, and manufacturer-validated dynamic charging curves.
- **Integration with Smart Home Platforms:** Home Assistant (MQTT auto discovery, native inverter control via service calls), OpenHAB, EVCC, and REST APIs.
- **Dynamic Web Dashboard:** Live monitoring, manual overrides, and visualization of the optimization process.
- **Cost Optimization:** Automatic alignment with dynamic electricity prices (Tibber, smartenergy.at, EVCC, timeseries, etc.) with configurable resolution. The `timeseries` source reads any HTTP or Home Assistant endpoint that publishes EVCC's `{start, end, value}` format. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#price)
- **Dynamic Feed-In Pricing:** Optimize battery discharge for maximum profit when export prices are favorable. Switch feed-in sources live without restart via hot reload. Supports fixed, Elpris DK, EPEX Spot, and EVCC. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#price)
- **Smart Price Prediction:** Learned grid fees and taxes for accurate planning even when future prices aren't yet available. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#energyforecast)
- **Dynamic PV Override:** Intelligent discharge prevention during high solar production or intermittent clouds. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#dyn-override)
- **PV Auto-Scaling:** Learns from historical measured solar yield and automatically corrects PV forecasts with per-timeframe scale factors before optimization. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#pv-autoscaling)
- **Smart Grid Limits (EVopt):** Grid import/export limits automatically default to your inverter capabilities when not explicitly configured, ensuring optimization respects your hardware. [Learn more →](https://ohAnd.github.io/EOS_connect/advanced/index.html#grid-limits)
- **Backup & Restore:** One file holding your whole install — configuration plus the measured PV yield history the auto-scaler learns from, which is otherwise deleted on a rolling window. Restores preview before they apply, and an old backup's history can be shifted into the current window so scaling works from the first run. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#backup-restore)
- **Robust Data Quality Handling:** Automatic detection and recovery from incomplete Home Assistant sensor data gaps. Forward-fill strategy ensures optimization always receives complete, valid input arrays. [Learn more →](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#data-quality)

---


## How It Works
EOS Connect acts as the central brain of your energy system:
1. **Data Collection:** Periodically collects local consumption, battery states, and inverter data.
2. **Forecasting:** Fetches PV solar forecasts and upcoming energy prices for the next 48 hours.
3. **Internal Optimization:** The built-in optimizer processes this data locally to generate the most cost-efficient power strategy.
4. **Active Control:** Applies targeted commands to your devices (inverters, batteries, wallboxes) based on the calculated strategy.

All scheduling, logic, and interface management is handled by EOS Connect, providing a unified and reliable energy management experience.

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

2. **Install EOS Connect Add-on:**
   - Add the [ohAnd/ha_addons](https://github.com/ohAnd/ha_addons) repository to your Home Assistant add-on store.
   - Install the **EOS Connect** add-on from the store.
   - The built-in optimizer (`local_evopt`) works out of the box — no additional add-ons required.

3. **(Optional) External optimization backend:**
   - To use Akkudoktor EOS as backend, add the [Duetting/ha_eos_addon](https://github.com/Duetting/ha_eos_addon) or [thecem/ha_eos_addon](https://github.com/thecem/ha_eos_addon) repository and install the EOS add-on.
   - To use EVopt, install [thecem/hassio-evopt](https://github.com/thecem/hassio-evopt) and make sure it is running.

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

⚠️ **Important: Proxmox / KVM64 CPU Limitation**

If EOS Connect **crashes with a segmentation fault on startup** on **Proxmox** with the default CPU type, your VM is likely using `kvm64` (a generic CPU emulation) which does not expose advanced CPU instructions (AVX, SSE4.2, …). The prebuilt `numpy` / `pandas` wheels need them.

> **Not this:** if `local_evopt` fails with `FileNotFoundError … solverdir/cbc/linux/i64/cbc`, that is **not** a CPU issue — see the add-on section below.

**✅ Solution:**

1. **Stop the HA VM** in Proxmox
2. **VM Settings → Hardware → Processor**
3. **Change CPU Type** from `kvm64` → `host`
   - This passes through your physical CPU directly instead of generic emulation
4. **Restart the VM**

After this change, EOS Connect (including `local_evopt`) will run normally and with full performance.

**Note:** Changing to `host` CPU type is safe and recommended for all Proxmox VMs running containerized applications that require native CPU features.

⚠️ **Important: Home Assistant OS Add-on (x86_64) CBC Solver**

The CBC solver binary that ships inside the `pulp` package is, on x86_64 only, dynamically linked against glibc. The add-on images are Alpine-based (musl), which provides no glibc loader, so the binary cannot be started at all — Linux reports the misleading `FileNotFoundError: [Errno 2] No such file or directory` even though the file is present. aarch64 add-ons are unaffected, because the arm64 binary that `pulp` ships is statically linked.

The add-on now installs a **statically linked CBC** of its own, which needs no glibc and runs on musl. Update to the latest add-on version and `local_evopt` works out of the box. EOS Connect also verifies the solver by actually executing it at startup and logs which binary it selected, so any remaining problem is visible in the log rather than surfacing as a cryptic error mid-optimization.

For full details see the [troubleshooting docs](https://ohAnd.github.io/EOS_connect/user-guide/index.html#troubleshooting).

**Note on SSL Certificate Verification:**
By default, EOS Connect validates SSL certificates when connecting to Home Assistant or OpenHAB. If you use a setup with **self-signed or private CA certificates**, you can disable verification in Settings → Data Source → **SSL Ignore** (expert level, requires restart). Only enable this in **trusted private networks** where you fully control the network path. Currently, EOS Connect does not support supplying custom root CA certificates — this feature is planned for future releases. For production setups, we recommend obtaining a valid certificate through Let's Encrypt (free) or your organization's certificate authority.

---

**Other Installation Options:**
- Docker, manual, and advanced setups are supported. See the [docs](https://ohAnd.github.io/EOS_connect/user-guide/index.html) for details.

---

## Configuration

EOS Connect uses a **web-based configuration system**. All settings are managed through the built-in web UI at `http://localhost:8081`.

### First Start (Setup Wizard)
On first launch, a **Setup Wizard** guides you through the essential configuration steps in optimal order:
1. **Optimizer** — Select your optimization backend (built-in Local EVopt, EOS Server, or external EVopt)
2. **EVCC** (Optional) — Configure if you want to use EVCC for PV forecasts, inverter control gateway, or car charging dependent control. Can be skipped if not using EVCC.
3. **Data Source** — Connect to Home Assistant, OpenHAB, or use default sensors
4. **Inverter** — Select your inverter type for battery control (display-only if not using hardware control). Can use EVCC as controller if configured in step 2, or Home Assistant if configured in step 3.
5. **Battery** — Set capacity and SOC limits (including the SOC sensor, if a data source is connected)
6. **Load** — Connect your load sensor. Skipped automatically when no data source is connected — there is nothing to read a sensor from
7. **Price** — Choose your electricity pricing provider
8. **PV Installations** — Configure your solar forecast provider and PV systems (location-based sources only). Preset to **Default**, a built-in demo forecast that needs no setup, so a first run finishes without asking for your location.

The wizard saves only the answers it asked you for. Everything else keeps its default until you change it in Settings.

After the wizard completes, restart EOS Connect to apply the settings.

**Note:**
- EVCC and Data Source both come before Inverter, because either can act as the inverter controller. If the EVCC URL is not configured, that option is greyed out in the Inverter and PV Source fields.
- The **Default** PV source is a fixed demo curve for an assumed 4 kW array, not a forecast for your roof. It exists so you can see EOS Connect running immediately; swap it for a real provider under Settings ▸ PV Source afterwards. The **Review & Finish** step reminds you.
- PV Installations configuration is only required for location-based forecast sources (Akkudoktor, OpenMeteo, Forecast.Solar). Other sources (Default, Solcast, Victron, EVCC, Timeseries) configure their data elsewhere and do not need PV Installations defined. For `Timeseries`, see the [Home Assistant template snippets](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#timeseries-templates).
- Sensor fields only appear once **Data Source** names Home Assistant or openHAB. Left unset, load and battery fall back to built-in defaults and say so in the log — they are never left polling a name you did not choose. Each sensor field has a **Test** button in Settings that reads the entity and shows its current value, so a typo is caught before you restart.
- A few choices need a setting the wizard does not ask for, because it is not a getting-started field — the Home Assistant inverter is driven by service-call sequences, and a fixed 24-hour tariff needs your own hourly prices. The **Review & Finish** step lists anything still outstanding, so you know to visit Settings afterwards.

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

### Backup & Restore
- **Menu → Backup & Restore** saves configuration *and* measured PV yield history to one file, and restores both
- The measured history is not backed up anywhere else and is purged on a rolling window (7 days by default)
- Restoring always previews first — including which settings it would remove — and nothing is written until you confirm
- **The backup file contains your tokens and inverter password in plain text.** Store it accordingly
- Full details: [Backup & Restore](https://ohAnd.github.io/EOS_connect/user-guide/configuration.html#backup-restore)
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
