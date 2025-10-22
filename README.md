# EOS Connect

**EOS Connect** is an open-source tool for intelligent energy management and optimization.  
It connects to various smart home platforms (like Home Assistant and OpenHAB) to monitor, forecast, and control your energy flows.  
EOS Connect fetches real-time and forecast data (PV, load, prices), processes it via the [EOS (Energy Optimization System)](https://github.com/Akkudoktor-EOS/EOS), and automatically controls devices (such as Fronius inverters or batteries supported by [evcc](https://docs.evcc.io/docs/devices/meters)) to optimize your energy usage and costs.

**Key Features:**
- **Automated Energy Optimization:**  
  Uses real-time and forecast data to maximize self-consumption and minimize grid costs.
- **Battery and Inverter Management:**  
  Supports charge/discharge control, grid/PV modes, and dynamic charging curves.
- **Integration with Smart Home Platforms:**  
  Works with Home Assistant, OpenHAB, EVCC, and MQTT for seamless data exchange and automation.
- **Dynamic Web Dashboard:**  
  Provides live monitoring, manual control, and visualization of your energy system.
- **Cost Optimization:**  
  Aligns energy usage with dynamic electricity prices (e.g., Tibber, smartenergy.at, Stromligning.dk).
- **Flexible Configuration:**  
  Easy to set up and extend for a wide range of energy systems and user needs.

EOS Connect helps you get the most out of your solar and storage systems—whether you want to save money, increase self-sufficiency, or simply monitor your energy flows in real time.


- [EOS Connect](#eos-connect)
  - [Key Features](#key-features)
    - [**Energy Optimization**](#energy-optimization)
    - [**Interactive Web Interface**](#interactive-web-interface)
    - [**Integration with External Systems**](#integration-with-external-systems)
  - [Current Status](#current-status)
  - [Quick Start](#quick-start)
    - [1. Requirements](#1-requirements)
    - [2. Install via Home Assistant Add-on](#2-install-via-home-assistant-add-on)
    - [3. Configure](#3-configure)
    - [4. Explore](#4-explore)
  - [EOS Configuration Requirements](#eos-configuration-requirements)
    - [Required EOS Prediction Settings](#required-eos-prediction-settings)
    - [What EOS Connect Handles](#what-eos-connect-handles)
    - [Troubleshooting](#troubleshooting)
  - [How it Works](#how-it-works)
    - [Base](#base)
    - [Collecting Data](#collecting-data)
      - [Home Assistant](#home-assistant)
      - [OpenHAB](#openhab)
      - [PV Forecast](#pv-forecast)
      - [Energy Price Forecast](#energy-price-forecast)
  - [Webpage Example](#webpage-example)
  - [Provided Data per **EOS connect** API](#provided-data-per-eos-connect-api)
    - [Web API (REST/JSON)](#web-api-restjson)
      - [Main Endpoints](#main-endpoints)
    - [MQTT - provided data and possible commands](#mqtt---provided-data-and-possible-commands)
      - [Published Topics](#published-topics)
      - [Example Usage](#example-usage)
    - [Subscribed Topics](#subscribed-topics)
    - [System Mode Control (`control/overall_state/set`)](#system-mode-control-controloverall_stateset)
    - [How to Use](#how-to-use)
      - [Examples](#examples)
  - [Configuration](#configuration)
  - [Useful Information](#useful-information)
    - [Getting historical values](#getting-historical-values)
      - [Home Assistant Persistance](#home-assistant-persistance)
      - [Openhab](#openhab-1)
  - [Usage](#usage)
  - [Requirements](#requirements)
  - [Installation and Running](#installation-and-running)
  - [Contributing](#contributing)
  - [Glossary](#glossary)
  - [License](#license)


## Key Features

### **Energy Optimization**
- **Dynamic Energy Flow Control**:
  - Automatically optimizes energy usage based on system states and external data.
  - Supports manual override modes for precise control.
- **Battery Management**:
  - Monitors battery state of charge (SOC) and remaining energy.
  - Configures charging and discharging modes, including:
    - Charge from grid.
    - Avoid discharge.
    - Discharge allowed.
    - EVCC-specific modes (e.g., fast charge, PV mode).
  - **Dynamic Charging Curve**:
    - If enabled, EOS Connect automatically adjusts the maximum battery charging power based on the current state of charge (SOC). This helps to optimize battery health and efficiency by reducing charge power as the battery approaches full capacity.
- **Cost and Solar Optimization**:
  - Aligns energy usage with real-time electricity prices (e.g., from Tibber, [smartenergy.at](https://www.smartenergy.at/), or [Stromligning.dk](https://stromligning.dk/)) to minimize costs.
  - Incorporates PV forecasts to prioritize charging during periods of high solar output.
  - Reduces grid dependency and maximizes self-consumption by combining cost and solar production data.
- **Energy Optimization Scheduling**:
  - Displays timestamps for the last and next optimization runs.
  - Tracks system performance and optimization results.

### **Interactive Web Interface**
- **Real-Time Monitoring**:
  - View current system states, including battery SOC, grid charge power, and EVCC modes.
  - Dynamic icons and color-coded indicators for easy visualization.
- **User Controls**:
  - Set grid charge power and override system modes directly from the interface.
  - Configure EVCC charging behavior with intuitive controls.

### **Integration with External Systems**
- **Home Assistant**:
  - Full MQTT integration with Home Assistant Auto Discovery (enabled by default via `mqtt.ha_mqtt_auto_discovery`).
  - Automatically detects and configures energy system entities.
- **OpenHAB**:
  - Integrates with OpenHAB for monitoring and controlling energy systems.
  - Publishes system states and subscribes to commands via MQTT.
- **EVCC (Electric Vehicle Charging Controller)**:
  - Monitors and controls EVCC charging modes and states.
  - Supports fast charge, PV charging, and combined modes.
- **Inverter Interfaces**:
  - OPTION 1: Communicates directly with a Fronius GEN24 to monitor and control energy flows.
    - `fronius_gen24`: Enhanced interface with firmware-based authentication for all firmware versions
    - `fronius_gen24_legacy`: Legacy interface for corner cases or troubleshooting
  - OPTION 2: Use the [evcc external battery control](https://docs.evcc.io/docs/integrations/rest-api) to interact with all inverter/ battery systems that [are supported by evcc](https://docs.evcc.io/en/docs/devices/meters) (hint: the dynamic max charge power is currently not supported by evcc external battery control)
  - OPTION 3: using without a direct control interface to get the resulting commands by **EOS connect** MQTT or web API to control within your own environment (e.g. [Integrate inverter e.g. sungrow SH10RT #35](https://github.com/ohAnd/EOS_connect/discussions/35)  )
  - Retrieves real-time data such as grid charge power, discharge power, and battery SOC.
- **MQTT Broker**:
  - Acts as the central hub for real-time data exchange.
  - Publishes system states and subscribes to control commands.

## Current Status

This project is in its early stages and is actively being developed and enhanced.

2025-04-10

- EOS made a breaking change - see here https://github.com/Akkudoktor-EOS/EOS/discussions/513
- there were also changes in the API at '<your_ip>:8503' - unfortunately the API is not versioned (*ping* ;-) )
- to fullfil both versions there is small hack to identify the connected EOS
- finally the current version can run with both EOS versions

2025-09-06

- Added **Enhanced Fronius GEN24 Interface** (`fronius_gen24`) with intelligent authentication support:
  - ✅ **Automatic Firmware Detection**: Detects firmware version and selects optimal authentication method
  - ✅ **Universal Compatibility**: Works with all firmware versions (< 1.36.5-1, 1.36.5-1 to 1.38.5-x, ≥ 1.38.6-1)
  - ✅ **Smart Authentication**: MD5 for older firmware, SHA256 with MD5 fallback for newest firmware  
  - ✅ **Optimized Performance**: Reduces authentication overhead by using firmware-appropriate methods
  - ✅ **Better Error Handling**: Clear troubleshooting guidance for authentication issues
  - ✅ **100% Backward Compatibility**: Drop-in replacement for previous interface
  - **Recommended**: Default interface for all Fronius GEN24 installations
  - **Legacy Fallback**: Use `fronius_gen24_legacy` for corner cases if needed

---

## Quick Start

Get up and running with EOS Connect in just a few steps!

### 1. Requirements

- **Home Assistant** (recommended for most users)  
  *(Or see [Installation and Running](#installation-and-running) for Docker and local options)*
- **An already running instance of [EOS (Energy Optimization System)](https://github.com/Akkudoktor-EOS/EOS)**  
  EOS Connect acts as a client and requires a reachable EOS server for optimization and control. (Or use the EOS HA addon mentioned in next step.)
- **Properly configured EOS for prediction** (see [EOS Configuration Requirements](#eos-configuration-requirements) below)

### 2. Install via Home Assistant Add-on

- Add the [ohAnd/ha_addons](https://github.com/ohAnd/ha_addons) repository to your Home Assistant add-on store.
- [if needed] Add the [Duetting/ha_eos_addon](https://github.com/Duetting/ha_eos_addon) (or [thecem/ha_eos_addon](https://github.com/thecem/ha_eos_addon)) repository to your Home Assistant add-on store.
- Install both the **EOS Add-on** and the **EOS Connect Add-on**.
- Configure both add-ons via the Home Assistant UI.
- Start both add-ons.  
  The EOS Connect web dashboard will be available at [http://homeassistant.local:8081](http://homeassistant.local:8081) (or your HA IP).

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fohand%2Fha_addons)

### 3. Configure

- On first start, a default `config.yaml` will be created in the add-on’s config folder.
- Edit this file via the Home Assistant add-on UI to set your EOS server address and other options.
- See the [CONFIG_README](src/CONFIG_README.md) for full configuration details.

### 4. Explore

- Open the web dashboard at [http://homeassistant.local:8081](http://homeassistant.local:8081) (or your HA IP).
- Check live data, forecasts, and system status.
- Integrate with your automation tools using the [Web API](#web-api-restjson) or [MQTT](#mqtt---provided-data-and-possible-commands).

---

## EOS Configuration Requirements

**Important**: EOS Connect requires specific prediction settings in your EOS instance. The default EOS configuration should work out-of-the-box, but verify these settings if you experience issues with forecasting.

### Required EOS Prediction Settings

In your EOS `config.yml`, ensure these prediction parameters are configured:

```yaml
# EOS config.yml - Prediction and Optimization settings
prediction:
  # Prediction horizon (default: 48 hours)
  # EOS Connect requires at least 48 hours for proper optimization
  hours_ahead: 48
  
optimization:
  # Optimization horizon (default: 48 hours) 
  # Should match or be <= prediction hours_ahead
  hours_ahead: 48
```

### What EOS Connect Handles

- **Optimization Requests**: EOS Connect sends optimization requests to EOS on a configurable interval (e.g., every 3 minutes)
- **No EOS Internal Scheduling**: EOS Connect manages all timing - no internal EOS optimization intervals are used
- **48-Hour Forecasting**: EOS Connect provides 48-hour load and PV forecasts to EOS for optimal decision making

### Troubleshooting

- **Short/No predictions**: Verify `prediction.hours_ahead: 48` in EOS config
- **Optimization errors**: Ensure `optimization.hours_ahead` is set to 48 or less than prediction horizon
- **EOS Connect timing**: All optimization scheduling is handled by EOS Connect, not EOS internal timers

The default EOS configuration typically includes these 48-hour settings. If you've customized your EOS config, ensure these values are properly set.

For detailed EOS configuration, refer to the [EOS documentation](https://github.com/Akkudoktor-EOS/EOS#configuration).

---

> If you're new to Home Assistant add-ons, see [the official documentation](https://www.home-assistant.io/addons/) for help.

> **Not using Home Assistant?**  
> See [Installation and Running](#installation-and-running) for Docker and local installation instructions.

## How it Works

### Base

**EOS Connect** is a self-running system that periodically collects:
- Local energy consumption data.
- PV solar forecasts for the next 48 hours.
- Upcoming energy prices.

Using this data, a request is sent to EOS, which creates a model predicting the energy needs based on different energy sources and loads (grid, battery, PV).

**EOS Connect** waits for the response from EOS (e.g., ~2 min 15 sec for a full 48-hour prediction on a Raspberry Pi 5). After receiving the response, it is analyzed to extract the necessary values.

Finally, the system sets up the inverter based on the following states:
- `MODE_CHARGE_FROM_GRID` with a specific target charging power (based on your configuration).
- `MODE_AVOID_DISCHARGE`.
- `MODE_DISCHARGE_ALLOWED` with a specific target maximum discharging power (based on your configuration).

The system repeats this process periodically, e.g., every 3 minutes, as defined in the configuration.

<div align="center">

<img src="doc/eos_connect_flow.png" alt="EOS connect flow" width="450"/>

<br>
<sub><i>Figure: EOS Connect process flow</i></sub>
<br>
<sub><i>Note: Due to my limited drawing skills ;-) , the principle diagram above was generated with the help of an AI image engine.</i></sub>
</div>

---

### Collecting Data

Data collection for load forecasting is based on your existing load data provided by an OpenHAB or Home Assistant instance (using the persistence of each system). EOS requires a load forecast for today and tomorrow.

#### Home Assistant
Load data is retrieved from:
- Today one week ago, averaged with today two weeks ago.
- Tomorrow one week ago, averaged with tomorrow two weeks ago.
- **Car Load Adjustment**: If an electric vehicle (EV) is/ was connected, its load is subtracted from the household load to ensure accurate forecasting of non-EV energy consumption.

**Load Sensor Requirements:**
- **Data Quality**: The sensor must provide numeric values and in unit 'watts'.
- **Value Handling**: EOS Connect accepts both positive and negative values from your sensor. For the internal processing: all values are converted to absolute positive values for load calculations.
- **Sensor Types**: Use sensors representing the overall net household consumption. Expected that all additonal loads as ev charge or an optional load are included here.

(See [Home Assistant Persistance](#home-assistant-persistance) for more details.)

#### OpenHAB
Load data is retrieved from the last two days:
- From two days ago (00:00) to yesterday midnight.
- **Car Load Adjustment**: Similar to Home Assistant, the EV load is subtracted from the household load to isolate non-EV energy consumption.

#### PV Forecast
EOS Connect supports multiple sources for solar (PV) production forecasts. You can choose the provider that best fits your location and needs. The following PV forecast sources are available and configurable:

- **Akkudoktor** (default)  
  Direct integration with the [Akkudoktor API](https://api.akkudoktor.net/forecast) for reliable PV forecasts.

- **Open-Meteo**  
  Uses the [Open-Meteo API](https://open-meteo.com/en/docs) and [open-meteo-solar-forecast Python library](https://github.com/rany2/open-meteo-solar-forecast) for library-based calculation

- **Open-Meteo Local**  
  Gathers radiation and cloud cover data from Open-Meteo and calculates PV output locally using an own model (experimental).

- **Forecast.Solar**  
  Connects to the [Forecast.Solar API](https://doc.forecast.solar/api) for detailed PV production forecasts.

- **Solcast**  
  Integrates with the [Solcast API](https://solcast.com/) for high-precision solar forecasting using satellite data and machine learning models. Requires creating a rooftop site in your Solcast account and using the resource ID (not location coordinates). Free Solcast API key provides up to 10 API calls per day. **Note: EOS Connect automatically uses extended update intervals (2.5 hours) when Solcast is selected to stay within rate limits.**

- **EVCC**  
  Retrieves PV forecasts directly from an existing [EVCC](https://evcc.io/) installation via its API. This option leverages EVCC's built-in solar forecast capabilities, including its automatic scaling feature that adjusts forecasts based on your actual historical PV production data for improved accuracy.

#### Energy Price Forecast
Energy price forecasts are retrieved from the chosen source (e.g. tibber, Akkudoktor, Smartenergy, ...). **Note**: Prices for tomorrow are available earliest at 1 PM. Until then, today's prices are used to feed the model.

---

## Webpage Example

The dashbaord of **EOS connect** is available at `http://localhost:8081`.

![webpage screenshot](doc/screenshot_0_1_20.png)

## Provided Data per **EOS connect** API

EOS Connect can be integrated with your smart home or automation tools using MQTT or its built-in web API. See below for details.

### Web API (REST/JSON)

EOS Connect provides a RESTful web API for real-time data access and remote control.  
All endpoints return JSON and can be accessed via HTTP requests.

<details>
<summary>Details</summary>

**Base URL:**  
`http://<host>:<port>/`  
*(Default port is set in your config, e.g., `8081`)*

---

#### Main Endpoints

| Endpoint                            | Method | Returns / Accepts | Description                                                    |
| ----------------------------------- | ------ | ----------------- | -------------------------------------------------------------- |
| `/json/current_controls.json`       | GET    | JSON              | Current system control states (AC/DC charge, mode, etc.)       |
| `/json/optimize_request.json`       | GET    | JSON              | Last optimization request sent to EOS                          |
| `/json/optimize_response.json`      | GET    | JSON              | Last optimization response from EOS                            |
| `/json/optimize_request.test.json`  | GET    | JSON              | Test optimization request (static file)                        |
| `/json/optimize_response.test.json` | GET    | JSON              | Test optimization response (static file)                       |
| `/controls/mode_override`           | POST   | JSON (see below)  | Override system mode, duration, and grid charge power          |
| `/logs`                             | GET    | JSON              | Retrieve application logs with optional filtering              |
| `/logs/alerts`                      | GET    | JSON              | Retrieve warning and error logs for alert system               |
| `/logs/clear`                       | POST   | JSON              | Clear all stored logs from memory (file logs remain intact)    |
| `/logs/alerts/clear`                | POST   | JSON              | Clear only alert logs from memory, keeping regular logs intact |
| `/logs/stats`                       | GET    | JSON              | Get buffer usage statistics for log storage                    |

---

<details>
<summary>Show Example: <code>/json/current_controls.json</code> (GET)</summary>

Get current system control states and battery information.

**Response:**
```json
{
    "current_states": {
        "current_ac_charge_demand": 0,
        "current_dc_charge_demand": 10000.0,
        "current_discharge_allowed": true,
        "inverter_mode": "MODE DISCHARGE ALLOWED",
        "inverter_mode_num": 2,
        "override_active": false,
        "override_end_time": 0
    },
    "evcc": {
        "charging_state": false,
        "charging_mode": "off",
        "current_sessions": [
            {
                "connected": false,
                "charging": false,
                "mode": "pv",
                "chargeDuration": 0,
                "chargeRemainingDuration": 0,
                "chargedEnergy": 0,
                "chargeRemainingEnergy": 0,
                "sessionEnergy": 0,
                "vehicleSoc": 0,
                "vehicleRange": 0,
                "vehicleOdometer": 0,
                "vehicleName": "",
                "smartCostActive": false
            }
        ]
    },
    "battery": {
        "soc": 23.8,
        "usable_capacity": 3867.11,
        "max_charge_power_dyn": 10000,
        "max_grid_charge_rate": 10000
    },
    "inverter": {
        "inverter_special_data": {
            "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32": 39.71,
            "MODULE_TEMPERATURE_MEAN_01_F32": 27.47,
            "MODULE_TEMPERATURE_MEAN_03_F32": 27.15,
            "MODULE_TEMPERATURE_MEAN_04_F32": 26.81,
            "FANCONTROL_PERCENT_01_F32": 0.0,
            "FANCONTROL_PERCENT_02_F32": 0.0
        }
    },
    "state": {
        "request_state": "response received",
        "last_request_timestamp": "2024-11-14T22:28:56.678704+02:00",
        "last_response_timestamp": "2024-11-14T22:30:01.194684+02:00",
        "next_run": "2024-11-14T22:35:01.196502+02:00"
    },
    "eos_connect_version": "0.2.01.138-develop",
    "timestamp": "2024-06-01T12:00:00+02:00",
    "api_version": "0.0.1"
}
```
</details>

---

<details>
<summary>Show Example: <code>/json/optimize_request.json</code> (GET)</summary>

Get the last optimization request sent to EOS.

**Response:**
```json
{
  "ems": {
    "pv_prognose_wh": [0, 0, 0, ...],
    "strompreis_euro_pro_wh": [0.0003389, 0.0003315, ...],
    "einspeiseverguetung_euro_pro_wh": [0.0000794, 0.0000794, ...],
    "preis_euro_pro_wh_akku": 0,
    "gesamtlast": [383.316, 351.8412, ...]
  },
  "pv_akku": {
    "device_id": "battery1",
    "capacity_wh": 22118,
    "charging_efficiency": 0.93,
    "discharging_efficiency": 0.93,
    "max_charge_power_w": 10000,
    "initial_soc_percentage": 24,
    "min_soc_percentage": 5,
    "max_soc_percentage": 95
  },
  "inverter": {
    "device_id": "inverter1",
    "max_power_wh": 10000,
    "battery_id": "battery1"
  },
  "eauto": {
    "device_id": "ev1",
    "capacity_wh": 27000,
    "charging_efficiency": 0.9,
    "discharging_efficiency": 0.95,
    "max_charge_power_w": 7360,
    "initial_soc_percentage": 50,
    "min_soc_percentage": 5,
    "max_soc_percentage": 100
  },
  "dishwasher": {
    "device_id": "additional_load_1",
    "consumption_wh": 1,
    "duration_h": 1
  },
  "temperature_forecast": [9.3, 9.3,...],
  "start_solution": [0, 14, ...],
  "timestamp": "2025-10-14T22:21:12.128290+02:00"
}
```
</details>

---

<details>
<summary>Show Example: <code>/json/optimize_response.json</code> (GET)</summary>

Get the last optimization response received from EOS.

**Response:**
```json
{
  "ac_charge": [0, 0, ...],
  "dc_charge": [1, 1, ...],
  "discharge_allowed": [0, 0, ...],
  "eautocharge_hours_float": null,
  "result": {
    "Last_Wh_pro_Stunde": [487.02085, 387.7635, ...],
    "EAuto_SoC_pro_Stunde": [50, 50, ...],
    "Einnahmen_Euro_pro_Stunde": [0, 0, ...],
    "Gesamt_Verluste": 817.136415028724,
    "Gesamtbilanz_Euro": 0.638737006073083,
    "Gesamteinnahmen_Euro": 0,
    "Gesamtkosten_Euro": 0.638737006073083,
    "Home_appliance_wh_per_hour": [0, 1, ...],
    "Kosten_Euro_pro_Stunde": [0, 0, ...],
    "Netzbezug_Wh_pro_Stunde": [0, 0, ...],
    "Netzeinspeisung_Wh_pro_Stunde": [0, 0, ...],
    "Verluste_Pro_Stunde": [36.6574833333333, 29.1865, ...],
    "akku_soc_pro_stunde": [24, 21.632343189559, 19.7472269946047, ...],
    "Electricity_price": [0.0003635, 0.0003462, ...]
  },
  "eauto_obj": {
    "device_id": "ev1",
    "hours": 48,
    "charge_array": [1, 1, ...],
    "discharge_array": [1, 1, ...],
    "discharging_efficiency": 0.95,
    "capacity_wh": 27000,
    "charging_efficiency": 0.9,
    "max_charge_power_w": 7360,
    "soc_wh": 13500,
    "initial_soc_percentage": 50
  },
  "start_solution": [0, 14, ...],
  "washingstart": 23,
  "timestamp": "2025-10-14T22:21:12.128796+02:00"
}
```
</details>

---

<details>
<summary>Show Example: <code>/controls/mode_override</code> (POST)</summary>

Override the system mode, duration, and grid charge power.

**Request Payload:**
```json
{
  "mode": 1,                // Integer, see mode table below
  "duration": "02:00",      // String, format "HH:MM"
  "grid_charge_power": 2.0  // Float, kW (e.g., 2.0 for 2000 W)
}
```

**Response:**
- On success:
  ```json
  { 
    "status": "success", 
    "message": "Mode override applied",
    "applied_settings": {
      "mode": 1,
      "mode_name": "ChargeFromGrid",
      "duration": "02:00",
      "grid_charge_power": 2000,
      "end_time": "2024-06-01T14:00:00+02:00"
    }
  }
  ```
- On error:
  ```json
  { 
    "error": "Invalid mode value",
    "details": "Mode must be between 0 and 4"
  }
  ```

**System Modes (`mode` field):**

| Mode Name                     | Mode Number | Description                                 |
| ----------------------------- | ----------- | ------------------------------------------- |
| `Auto`                        | -2          | Fully automatic optimization (default mode) |
| `StartUp`                     | -1          | System startup state                        |
| `Charge from Grid`            | 0           | Force battery charging from the grid        |
| `Avoid Discharge`             | 1           | Prevent battery discharge                   |
| `Discharge Allowed`           | 2           | Allow battery discharge                     |
| `Avoid Discharge EVCC FAST`   | 3           | Avoid discharge with EVCC fast charge       |
| `Avoid Discharge EVCC PV`     | 4           | Avoid discharge with EVCC PV mode           |
| `Avoid Discharge EVCC MIN+PV` | 5           | Avoid discharge with EVCC MIN+PV mode       |

</details>

---

<details>
<summary>Show Example: <code>/logs</code> (GET)</summary>

Retrieve application logs with optional filtering.

**Query Parameters:**
- `level`: Filter by log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)
- `limit`: Maximum number of records to return (default: 100)
- `since`: ISO timestamp to get logs since that time

**Examples:**
- Get last 50 logs: `GET /logs?limit=50`
- Get only error logs: `GET /logs?level=ERROR`
- Get logs since 1 hour ago: `GET /logs?since=2024-06-01T11:00:00Z`

**Response:**
```json
{
  "logs": [
    {
      "timestamp": "2024-06-01T12:00:00.123456",
      "level": "INFO",
      "message": "[Main] Starting optimization run",
      "module": "__main__",
      "funcName": "run_optimization",
      "lineno": 542,
      "severity": 20
    }
  ],
  "total_count": 1,
  "timestamp": "2024-06-01T12:00:00+02:00",
  "filters_applied": {
    "level": null,
    "limit": 100,
    "since": null
  }
}
```
</details>

---

<details>
<summary>Show Example: <code>/logs/alerts</code> (GET)</summary>

Retrieve warning and error logs for alert system.

**Response:**
```json
{
  "alerts": [
    {
      "timestamp": "2024-06-01T12:00:00.123456",
      "level": "WARNING",
      "message": "[Battery] SOC exceeded maximum threshold",
      "module": "__main__",
      "funcName": "setting_control_data",
      "lineno": 234,
      "severity": 30
    }
  ],
  "grouped_alerts": {
    "WARNING": [ ... ],
    "ERROR": [ ... ],
    "CRITICAL": [ ... ]
  },
  "alert_counts": {
    "WARNING": 1,
    "ERROR": 0,
    "CRITICAL": 0
  },
  "timestamp": "2024-06-01T12:00:00+02:00"
}
```
</details>

---

<details>
<summary>Show Example: <code>/logs/stats</code> (GET)</summary>

Get buffer usage statistics for log storage monitoring.

**Response:**
```json
{
  "buffer_stats": {
    "main_buffer": {
      "current_size": 3456,
      "max_size": 5000,
      "usage_percent": 69.1
    },
    "alert_buffer": {
      "current_size": 23,
      "max_size": 2000,
      "usage_percent": 1.2
    },
    "alert_levels": ["WARNING", "ERROR", "CRITICAL"]
  },
  "timestamp": "2024-06-01T12:00:00+02:00"
}
```
</details>

---

<details>
<summary>Show Example: <code>/logs/clear</code> (POST)</summary>

Clear all stored logs from memory (file logs remain intact).

**Response:**
- On success:
  ```json
  { "status": "success", "message": "Logs cleared" }
  ```
- On error:
  ```json
  { "error": "Failed to clear logs" }
  ```

**Note:** This clears both the main log buffer (5000 entries) and the alert buffer (2000 entries).
</details>

---

<details>
<summary>Show Example: <code>/logs/alerts/clear</code> (POST)</summary>

Clear only alert logs from memory, keeping regular logs intact.

**Response:**
- On success:
  ```json
  { "status": "success", "message": "Alert logs cleared" }
  ```
- On error:
  ```json
  { "error": "Failed to clear alert logs" }
  ```

**Note:** This only clears the dedicated alert buffer (2000 entries), leaving the main log buffer untouched.
</details>

---

**How to Use:**
- **Get current system state:**  
  `GET http://localhost:8081/json/current_controls.json`
- **Override mode and charge power:**  
  `POST http://localhost:8081/controls/mode_override`  
  with JSON body as shown above.
- **Monitor application logs:**  
  `GET http://localhost:8081/logs?level=ERROR&limit=20`
- **Get system alerts:**  
  `GET http://localhost:8081/logs/alerts`
- **Get log buffer statistics:**  
  `GET http://localhost:8081/logs/stats`
- **Clear memory logs:**  
  `POST http://localhost:8081/logs/clear`
- **Clear only alerts:**  
  `POST http://localhost:8081/logs/alerts/clear`

You can use `curl`, Postman, or any HTTP client to interact with these endpoints.

**Examples using curl:**
```bash
# Get last 10 error logs
curl "http://localhost:8081/logs?level=ERROR&limit=10"

# Get current system alerts
curl "http://localhost:8081/logs/alerts"

# Get log buffer usage statistics
curl "http://localhost:8081/logs/stats"

# Clear all memory logs
curl -X POST "http://localhost:8081/logs/clear"

# Clear only alert logs
curl -X POST "http://localhost:8081/logs/alerts/clear"

# Override system mode
curl -X POST "http://localhost:8081/controls/mode_override" \
  -H "Content-Type: application/json" \
  -d '{"mode": 1, "duration": "02:00", "grid_charge_power": 2.0}'
```

**Memory Log System Notes:**
- **Main buffer**: Stores the last 5000 log entries (all levels mixed)
- **Alert buffer**: Stores the last 2000 alert entries (WARNING/ERROR/CRITICAL only)
- **Persistent storage**: File-based logs are not affected by memory operations
- **Timezone aware**: All timestamps use the configured timezone
- **Thread-safe**: Safe for concurrent access from multiple clients
- **Performance**: Memory-based access provides fast response times
- **Monitoring**: Use `/logs/stats` to monitor buffer usage and plan capacity

The logging API enables real-time monitoring, alerting systems, and debugging without affecting the persistent file-based logging system.

</details>

---

### MQTT - provided data and possible commands

EOS Connect publishes a wide range of real-time system data and control states to MQTT topics. You can use these topics to monitor system status, battery and inverter data, optimization results, and more from any MQTT-compatible tool (e.g., Home Assistant, Node-RED, Grafana, etc.).

<details>
<summary>MQTT Data Published</summary>

**Base topic:**  
`<mqtt_configured_prefix>/eos_connect/`  
*(Set `<mqtt_configured_prefix>` in your `config.yaml`, e.g., `myhome`)*

---

#### Published Topics

| Topic Suffix                                  | Full Topic Example                                               | Payload Type / Example     | Description                                                 |
| --------------------------------------------- | ---------------------------------------------------------------- | -------------------------- | ----------------------------------------------------------- |
| `optimization/state`                          | `myhome/eos_connect/optimization/state`                          | String (`"ok"`, `"error"`) | Current optimization request state                          |
| `optimization/last_run`                       | `myhome/eos_connect/optimization/last_run`                       | ISO timestamp              | Timestamp of the last optimization run                      |
| `optimization/next_run`                       | `myhome/eos_connect/optimization/next_run`                       | ISO timestamp              | Timestamp of the next scheduled optimization run            |
| `control/override_charge_power`               | `myhome/eos_connect/control/override_charge_power`               | Integer (W)                | Override charge power                                       |
| `control/override_active`                     | `myhome/eos_connect/control/override_active`                     | Boolean (`true`/`false`)   | Whether override is active                                  |
| `control/override_end_time`                   | `myhome/eos_connect/control/override_end_time`                   | ISO timestamp              | When override ends                                          |
| `control/overall_state`                       | `myhome/eos_connect/control/overall_state`                       | Integer (see mode table)   | Current overall system mode - see System Mode Control below |
| `control/eos_homeappliance_released`          | `myhome/eos_connect/control/eos_homeappliance_released`          | Boolean                    | Home appliance released flag                                |
| `control/eos_homeappliance_start_hour`        | `myhome/eos_connect/control/eos_homeappliance_start_hour`        | Integer (hour)             | Home appliance start hour                                   |
| `battery/soc`                                 | `myhome/eos_connect/battery/soc`                                 | Float (%)                  | Battery state of charge                                     |
| `battery/remaining_energy`                    | `myhome/eos_connect/battery/remaining_energy`                    | Integer (Wh)               | Usable battery capacity                                     |
| `battery/dyn_max_charge_power`                | `myhome/eos_connect/battery/dyn_max_charge_power`                | Integer (W)                | Dynamic max charge power                                    |
| `inverter/special/temperature_inverter`       | `myhome/eos_connect/inverter/special/temperature_inverter`       | Float (°C)                 | Inverter temperature (if Fronius V1/V2)                     |
| `inverter/special/temperature_ac_module`      | `myhome/eos_connect/inverter/special/temperature_ac_module`      | Float (°C)                 | AC module temperature (if Fronius V1/V2)                    |
| `inverter/special/temperature_dc_module`      | `myhome/eos_connect/inverter/special/temperature_dc_module`      | Float (°C)                 | DC module temperature (if Fronius V1/V2)                    |
| `inverter/special/temperature_battery_module` | `myhome/eos_connect/inverter/special/temperature_battery_module` | Float (°C)                 | Battery module temperature (if Fronius V1/V2)               |
| `inverter/special/fan_control_01`             | `myhome/eos_connect/inverter/special/fan_control_01`             | Integer                    | Fan control 1 (if Fronius V1/V2)                            |
| `inverter/special/fan_control_02`             | `myhome/eos_connect/inverter/special/fan_control_02`             | Integer                    | Fan control 2 (if Fronius V1/V2)                            |
| `status`                                      | `myhome/eos_connect/status`                                      | String (`"online"`)        | Always set to `"online"`                                    |
| `control/eos_ac_charge_demand`                | `myhome/eos_connect/control/eos_ac_charge_demand`                | Integer (W)                | AC charge demand                                            |
| `control/eos_dc_charge_demand`                | `myhome/eos_connect/control/eos_dc_charge_demand`                | Integer (W)                | DC charge demand                                            |
| `control/eos_discharge_allowed`               | `myhome/eos_connect/control/eos_discharge_allowed`               | Boolean                    | Discharge allowed                                           |



---

#### Example Usage

- **Monitor battery SOC in Home Assistant:**
  - Subscribe to `myhome/eos_connect/battery/soc` to get real-time battery state of charge.
- **Track optimization runs:**
  - Subscribe to `myhome/eos_connect/optimization/last_run` and `myhome/eos_connect/optimization/next_run` for scheduling info.
- **Visualize inverter temperatures:**
  - Subscribe to `myhome/eos_connect/inverter/special/temperature_inverter` (if Fronius V1/V2 inverter is connected).
- **Check if override is active:**
  - Subscribe to `myhome/eos_connect/control/override_active`.

You can use any MQTT client, automation platform, or dashboard tool to subscribe to these topics and visualize or process the data as needed.

---

**Notes:**
- The `<mqtt_configured_prefix>` is set in your configuration file (see `config.yaml`).
- Some topics (e.g., inverter special values) are only published if the corresponding hardware is present and enabled.
- All topics are published with real-time updates as soon as new data is available.

</details>
</br>
EOS Connect can be remotely controlled via MQTT by publishing messages to specific topics. This allows you to change system modes, set override durations, and adjust grid charge power from external tools such as Home Assistant, Node-RED, or any MQTT client.
</br></br>
<details>
<summary>MQTT Data Subscribed</summary>



**Base topic:**  
`<mqtt_configured_prefix>/eos_connect/`  
*(Set `<mqtt_configured_prefix>` in your `config.yaml`, e.g., `myhome`)*

---

### Subscribed Topics

| Topic Suffix                        | Full Topic Example                                     | Expected Payload         | Description / Effect                               |
| ----------------------------------- | ------------------------------------------------------ | ------------------------ | -------------------------------------------------- |
| `control/overall_state/set`         | `myhome/eos_connect/control/overall_state/set`         | Integer or string (mode) | Changes the system mode (see table below)          |
| `control/override_remain_time/set`  | `myhome/eos_connect/control/override_remain_time/set`  | String `"HH:MM"`         | Sets the override duration (e.g., `"02:00"`)       |
| `control/override_charge_power/set` | `myhome/eos_connect/control/override_charge_power/set` | Integer (watts)          | Sets the override grid charge power (e.g., `2000`) |

---

### System Mode Control (`control/overall_state/set`)

You can set the system mode by publishing either the **mode name** (string) or the **mode number** (integer).  
**Only the following values are accepted:**

| Mode Name        | Mode Number | Description                                 |
| ---------------- | ----------- | ------------------------------------------- |
| `Auto`           | 0           | Fully automatic optimization (default mode) |
| `ChargeFromGrid` | 1           | Force battery charging from the grid        |
| `Discharge`      | 2           | Force battery discharge                     |
| `Idle`           | 3           | No charging or discharging                  |
| `PVOnly`         | 4           | Charge battery only from PV (solar)         |

---

### How to Use

- **Publish a message** to the desired topic with the correct payload.
- The system will immediately process the command and update its state.
- You can use any MQTT client, automation platform, or script.

#### Examples

- **Set system mode to "Auto":**
  ```bash
  mosquitto_pub -t "myhome/eos_connect/control/overall_state/set" -m "Auto"
  ```
  or
  ```bash
  mosquitto_pub -t "myhome/eos_connect/control/overall_state/set" -m "0"
  ```

- **Force battery charging from grid:**
  ```bash
  mosquitto_pub -t "myhome/eos_connect/control/overall_state/set" -m "ChargeFromGrid"
  ```
  or
  ```bash
  mosquitto_pub -t "myhome/eos_connect/control/overall_state/set" -m "1"
  ```

- **Set override duration to 1 hour 30 minutes:**
  ```bash
  mosquitto_pub -t "myhome/eos_connect/control/override_remain_time/set" -m "01:30"
  ```

- **Set override grid charge power to 1500 W:**
  ```bash
  mosquitto_pub -t "myhome/eos_connect/control/override_charge_power/set" -m "1500"
  ```

---

**Notes:**
- The `<mqtt_configured_prefix>` is set in your configuration file (see `config.yaml`).
- Payloads must match the expected format for each topic.
- Any value other than those listed for system mode will be ignored or rejected.
- These topics allow you to remotely control and override the energy management system in real time.

</details>

## Configuration

With the first start of **EOS connect** a default `config.yaml` will be generated in the `\src` folder. For full documentation for the different entries go to [CONFIG_README](src/CONFIG_README.md)

*Note: With the default config and a valid EOS server IP/DNS name entry ('eos -> server') - **EOS connect** should be running out of the box with some static defaults as a start point for a step-by-step commissioning.*

## Useful Information

### Getting historical values

#### Home Assistant Persistance

The tool will use historical data from Home Assistant's local database. By default, this database is configured with a retention period of **10 days**.

To improve the accuracy of load forecasts, it is recommended to use data from the last **2 weeks**. 

You can extend the retention period by modifying the `recorder` configuration in Home Assistant's `configuration.yaml` file. If the `recorder` section is not already present, you can add it as shown below:

```yaml
recorder:
  purge_keep_days: 15  # Keep data for 15 days
```

After making this change, restart Home Assistant for the new retention period to take effect.

**Note**: Increasing the retention period will require more storage space, depending on the number of entities being recorded.

If you do not change the retention period, the tool will still work, but it will use the available 10 days of data, which may result in less accurate load forecasts.

#### Openhab

No specific info yet.

## Usage

The application will start fetching energy data from OpenHAB or HomeAssistant and processing it. You can access the web interface at `http://localhost:8081`. For local usage the port is configurable see [CONFIG_README](src/CONFIG_README.md). For docker usage change the mapped port in docker-compose.yml.

## Requirements

To run this project, you need to have the following installed:

- Python >= 3.11


## Installation and Running

You can run EOS Connect in three ways. Choose the method that best fits your environment:

---

<details>
<summary><strong>1. Home Assistant Add-on (Recommended for Home Assistant users)</strong></summary>

- Easiest way if you already use Home Assistant.
- Install the [EOS Add-on](https://github.com/Duetting/ha_eos_addon) and **EOS connect** Add-on from the [ohAnd/ha_addons](https://github.com/ohAnd/ha_addons) repository.
- Configure both add-ons via the Home Assistant UI.
- Both EOS and EOS Connect will run as managed add-ons.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fohand%2Fha_addons)

</details>

---

<details>
<summary><strong>2. Docker (Recommended for most users)</strong></summary>

- Works on any system with Docker and Docker Compose.
- Pull and run the latest image:
  ```bash
  git clone https://github.com/ohAnd/EOS_connect.git
  cd EOS_connect
  docker-compose up --pull always -d
  ```
- The web dashboard will be available at [http://localhost:8081](http://localhost:8081) by default.
- Configure by editing `src/config.yaml`.

![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/ohand/eos_connect/docker-image.yml)

[get the latest version](https://github.com/ohAnd/EOS_connect/pkgs/container/eos_connect)

</details>

---

<details>
<summary><strong>3. Local Installation (Advanced users, for development or custom setups)</strong></summary>

- Requires Python 3.11+ and pip.
- Clone the repository and install dependencies:
  ```bash
  git clone https://github.com/ohAnd/EOS_connect.git
  cd EOS_connect
  pip install -r requirements.txt
  python src/eos_connect.py
  ```
- Configure by editing `src/config.yaml`.

</details>

---

> For all methods, you need a running instance of [EOS (Energy Optimization System)](https://github.com/Akkudoktor-EOS/EOS).  
> See the [Quick Start](#quick-start) section for more details.


## Contributing

We welcome PRs. Keep main clean, iterate fast on develop.

Branch roles
- main: stable, tagged releases only (comes from develop).
- develop: integration branch (target of normal PRs).
- feature_<short-desc> or feature_<issue>-<desc>: new code (from develop).
- bugfix_<issue>-<desc>: fix for something already in develop.
- hotfix_<issue>-<desc>: urgent production fix (from main → PR to main → merge back into develop).
- issue-<number>-<desc>: automatically created from a GitHub issue (allowed and recommended).

You can create a branch manually or use GitHub’s "Create branch" button on an issue, which will name it like `issue-123-description`. This is fully supported and recommended for traceability.

Flow
1. Update local: git fetch origin && git switch develop && git pull --ff-only
2. Create branch: git switch -c feature/better-forecast
3. Code + tests + docs (README / CONFIG_README / MQTT if behavior changes)
4. Run formatting, lint, tests
   - Ensure all Python files are formatted with [Black](https://black.readthedocs.io/en/stable/) (`black .`)
     - **Tip for VS Code users:** Install the [Black Formatter extension](https://github.com/microsoft/vscode-black-formatter) for automatic formatting on save. (// VS Code settings.json "[python]": { "editor.formatOnSave": true })
   - Run [pylint](https://pylint.pycqa.org/) and ensure a score of **9.0 or higher** for all files (`pylint src/`)
   - tests - see info at guidelines below
5. Rebase before PR: git fetch origin && git rebase origin/develop
6. Push: git push -u origin feature/better-forecast
7. Open PR → base: develop (link issues: Closes #123)
8. Keep PR focused; squash or rebase merge (no merge commits)

Commits (Conventional)
feat: add battery forecast smoothing
fix: correct negative PV handling
docs: update MQTT topic table

Hotfix
git switch main
git pull --ff-only
git switch -c hotfix/overrun-calc
...fix...
PR → main, tag release, then: git switch develop && git merge --ff-only main

Guidelines
- One logical change per PR
- Add/adjust tests for logic changes
  - Use [pytest](https://docs.pytest.org/) for all unit and integration tests.
  - Place tests in the `tests/` directory, organized to mirror the structure of the `src/` directory:
    - Create a subfolder for each source module or feature (e.g., if your code is in `src/interfaces/mqtt_interface.py`, place tests in `tests/interfaces/test_mqtt_interface.py`).
    - Name test files as `test_<uut-filename>.py` (e.g., `test_mqtt_interface.py` for `mqtt_interface.py`).
- Document new config keys / API / MQTT topics
- Prefer clarity over cleverness

Thanks for contributing!

## Glossary

<details>
<summary>Show Glossary</summary>

| Term / Abbreviation | Meaning                                                                                                               |
| ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **EOS**             | Energy Optimization System – the [backend optimizer](https://github.com/Akkudoktor-EOS/EOS) this project connects to. |
| **SOC**             | State of Charge – the current charge level of your battery, usually in percent (%).                                   |
| **PV**              | Photovoltaic – refers to solar panels and their energy production.                                                    |
| **EV**              | Electric Vehicle.                                                                                                     |
| **EVCC**            | Electric Vehicle Charge Controller – [software](https://github.com/evcc-io/evcc)/hardware for managing EV charging.   |
| **HA**              | [Home Assistant](https://www.home-assistant.io/) – popular open-source smart home platform.                           |
| **OpenHAB**         | Another [open-source](https://www.openhab.org/) smart home platform.                                                  |
| **MQTT**            | Lightweight messaging protocol for IoT and smart home integration.                                                    |
| **API**             | Application Programming Interface – allows other software to interact with EOS Connect.                               |
| **Add-on**          | A packaged extension for Home Assistant, installable via its UI.                                                      |
| **Grid**            | The public electricity network.                                                                                       |
| **Dashboard**       | The web interface provided by EOS Connect for monitoring and control.                                                 |

</details>

## License

This project is licensed under the MIT License. See the LICENSE file for more details.
