# Configuration Guide

<!-- TOC -->
- [Configuration Guide](#configuration-guide)
- [Configuration](#configuration)
  - [Configuration Sections](#configuration-sections)
    - [Load Configuration](#load-configuration)
    - [EOS Server Configuration](#eos-server-configuration)
    - [Electricity Price Configuration](#electricity-price-configuration)
    - [Battery Configuration](#battery-configuration)
    - [PV Forecast Configuration](#pv-forecast-configuration)
      - [Parameters](#parameters)
    - [Inverter Configuration](#inverter-configuration)
    - [EVCC Configuration](#evcc-configuration)
    - [MQTT Configuration](#mqtt-configuration)
      - [Parameters](#parameters-1)
    - [Other Configuration Settings](#other-configuration-settings)
  - [Notes](#notes)
  - [Config examples](#config-examples)
    - [Full Config Example (will be generated at first startup)](#full-config-example-will-be-generated-at-first-startup)
    - [Minimal possible Config Example](#minimal-possible-config-example)
<!-- /TOC -->

# Configuration

This document provides an overview of the configuration settings for the application. The configuration settings are stored in a `config.yaml` file.  
A default config file will be created with the first start, if there is no `config.yaml` in the `src` folder.

*Hint: There are different combinations of parameters possible. If there is a problem with missing or incorrect configuration, it will be shown in the logs as an error.*

---


##  Configuration Sections

###  Load Configuration

- **`load.source`**:  
  Data source for load power. Possible values: `openhab`, `homeassistant`, `default` (default will use a primitive static consumption scheme).

- **`load.url`**:  
  URL for OpenHAB (e.g., `http://<ip>:8080`) or Home Assistant (e.g., `http://<ip>:8123`).

- **`load.access_token`**:  
  Access token for Home Assistant (optional). If not needed, set to `load.access_token: ""`

  *Hint: If you use Home Assistant as the source for load sensors, you must set the access token here as well. This token is independent from the one in the battery configuration.*

- **`load.load_sensor`**:  
  Item/entity name for load power data (OpenHAB item/Home Assistant sensor).
  Must be in watts. It's mandatory if not choosen 'default' as source.
  - Accepts positive (consumption) or negative (feed-in) values
  - All values converted internally to absolute positive values
  - Should represent the overall net household load
  

- **`load.car_charge_load_sensor`**:  
  Item/entity name for wallbox power data. 
  Must be in watts. (If not needed, set to `load.car_charge_load_sensor: ""`)
  - When configured, this load is subtracted from the main load sensor
  - Helps separate controllable EV charging from base household consumption

- **`additional_load_1_sensor`**:
  Item / entity for additional load power data. e.g. heatpump or dishwasher.
  Must be in watts. (If not needed set to `additional_load_1_sensor: ""`)
  - Also subtracted from main load for more accurate base load calculation

- **`additional_load_1_runtime`**:
  Runtime of additional load 1 in hours. Set to 0 if not needed. (If not needed, set to `additional_load_1_runtime: ""`)

- **`additional_load_1_consumption`**:
  Consumption of additional load 1 in Wh for ONE hour. Set to 0 if not needed. (If not needed, set to `additional_load_1_consumption: ""`)
  - Needed Amount will be calculated based on runtime and this hourly consumption.

---

### EOS Server Configuration

- **`eos.server`**:  
  EOS server address (e.g., `192.168.1.94`). (Mandatory)

- **`eos.port`**:  
  Port for the EOS server. Default: `8503`. (Mandatory)

- **`timeout`**:  
  Timeout for EOS optimization requests, in seconds. Default: `180`. (Mandatory)

---

### Electricity Price Configuration

**Important: All price values must use the same base - either all prices include taxes and fees, or all prices exclude taxes and fees. Mixing different bases will lead to incorrect optimization results.**

- **`price.source`**:  
  Data source for electricity prices. Possible values: `tibber`, `smartenergy_at`,`fixed_24h`,`default` (default uses akkudoktor API).

- **`price.token`**:  
  Token for accessing electricity price data. (If not needed, set to `token: ""`)

- **`price.fixed_24h_array`**:
  24 hours array with fixed end customer prices in ct/kWh over the day.
  - Leave empty if not set source to `fixed_24h`.
  - **Important**: Ensure these prices use the same tax/fee basis as your `feed_in_price`.
  - e.g. 10.42, 10.42, 10.42, 10.42, 10.42, 23.52, 28.17, 28.17, 28.17, 28.17, 28.17, 23.52, 23.52, 23.52, 23.52, 28.17, 28.17, 34.28, 34.28, 34.28, 34.28, 34.28, 28.17, 23.52 means 10.42 ct/kWh from 00 - 01 hour (config entry have to be without any brackets)
  - (If not needed set to `fixed_24h_array: ""`.)

- **`price.feed_in_price`**:  
  Feed-in price for the grid, in €/kWh. Single constant value for the whole day (e.g., `0.08` for 8 ct/kWh).
  - **Important**: Must use the same tax/fee basis as your electricity purchase prices from your chosen source or `fixed_24h_array`.
  - (If not needed, set to `feed_in_price: ""`)

- **`price.negative_price_switch`**:  
  Switch for handling negative electricity prices.  
  - `True`: Limits the feed-in price to `0` if there is a negative stock price for the hour.  
  - `False`: Ignores negative stock prices and uses the constant feed-in price. (If not needed, set to `negative_price_switch: ""`)

---

### Battery Configuration

- **`battery.source`**:  
  Data source for battery SOC (State of Charge). Possible values: `openhab`, `homeassistant`, `default` (static data).

- **`battery.url`**:  
  URL for OpenHAB (e.g., `http://<ip>:8080`) or Home Assistant (e.g., `http://<ip>:8123`).

- **`battery.soc_sensor`**:  
  Item/entity name for the SOC sensor (OpenHAB item/Home Assistant sensor).

- **`battery.access_token`**:  
  Access token for Home Assistant (optional).

  *Hint: If you use Home Assistant as the source for load sensors, you must set the access token here as well. This token is independent from the one in the load configuration.*

- **`battery.capacity_wh`**:  
  Total capacity of the battery, in watt-hours (Wh).

- **`battery.charge_efficiency`**:  
  Efficiency of charging the battery, as a decimal value between `0` and `1`.

- **`battery.discharge_efficiency`**:  
  Efficiency of discharging the battery, as a decimal value between `0` and `1`.

- **`battery.max_charge_power_w`**:  
  Maximum charging power for the battery, in watts (W).

- **`battery.min_soc_percentage`**:  
  Minimum state of charge for the battery, as a percentage.

- **`battery.max_soc_percentage`**:  
  Maximum state of charge for the battery, as a percentage.

- **`price_euro_per_wh_accu`**:
  Price for battery in €/Wh - can be used to shift the result over the day according to the available energy (more details follow).

- **`battery.charging_curve_enabled`**:  
  Enables or disables the dynamic charging curve for the battery.  
  - `true`: The system will automatically reduce the maximum charging power as the battery SOC increases, helping to protect battery health and optimize efficiency.  
  - `false`: The battery will always charge at the configured maximum power, regardless of SOC.  
  - **Default:** `true`

---

### PV Forecast Configuration

This section contains two subsections:
- `pv_forecast_source`
- `pv_forecast`

`pv_forecast_source` section declares the provider of solar forecast that should be used. Available providers are
- `akkudoktor` - https://api.akkudoktor.net/ - direct request and results
- `openmeteo` - https://open-meteo.com/en/docs - uses the [open-meteo-solar-forecast](https://github.com/rany2/open-meteo-solar-forecast) (no horizon possible by the lib at this time)
- `openmeteo_local` - https://open-meteo.com/en/docs - gathering radiation and cloudcover data and calculating locally with an own model - still in dev to improve the calculation
- `forecast_solar` - https://doc.forecast.solar/api - direct request and results
default is uses akkudoktor

`pv_forecast` section allows you to define multiple PV forecast entries, each distinguished by a user-given name. Below is an example of a default PV forecast configuration:

```yaml
pv_forecast_source:
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, forecast_solar, default (default uses akkudoktor)
pv_forecast:
  - name: myPvInstallation1  # User-defined identifier for the PV installation, must be unique if you use multiple installations
    lat: 47.5  # Latitude for PV forecast @ Akkudoktor API
    lon: 8.5  # Longitude for PV forecast @ Akkudoktor API
    azimuth: 90.0  # Azimuth for PV forecast @ Akkudoktor API
    tilt: 30.0  # Tilt for PV forecast @ Akkudoktor API
    power: 4600  # Power for PV forecast @ Akkudoktor API
    powerInverter: 5000  # Power Inverter for PV forecast @ Akkudoktor API
    inverterEfficiency: 0.9  # Inverter Efficiency for PV forecast @ Akkudoktor API
    horizon: 10,20,10,15  # Horizon to calculate shading, up to 360 values to describe the shading situation for your PV.
```

#### Parameters
- **`name`**:  
  A user-defined identifier for the PV installation. Must be unique if you use multiple installations.

- **`lat`**:  
  Latitude for the PV forecast.

- **`lon`**:  
  Longitude for the PV forecast.

- **`azimuth`**:  
  Azimuth angle for the PV forecast.

- **`tilt`**:  
  Tilt angle for the PV forecast.

- **`power`**:  
  The power of the PV installation, in watts (W).

- **`powerInverter`**:  
  The power of the inverter, in watts (W).

- **`inverterEfficiency`**:  
  The efficiency of the inverter, as a decimal value between `0` and `1`.

- **`horizon`**:  
  (Optional) A list of up to 36 values describing the shading situation for the PV installation. The list always covers 360° – 4 entries will represent 90° steps, e.g.
  - 10,20,10,15 – 0–90° is shadowed if sun elevation is below 10°, and so on.
  - 0,0,0,0,0,0,0,0,50,70,0,0,0,0,0,0,0,0 – 18 entries → 20° steps; here, 180°–200° requires 50° of sun elevation, otherwise the panel is shadowed.

---

### Inverter Configuration

- **`inverter.type`**:  
  Specifies the type of inverter. Possible values:  
  - `fronius_gen24`: Use the Fronius Gen24 inverter.
  - `evcc`: Use the universal interface via evcc external battery control (evcc config below has to be valid).
  - `default`: Disable inverter control (only display the target state).

- **`inverter.address`**:  
  The IP address of the inverter. (only needed for fronius_gen24)

- **`inverter.user`**:  
  The username for the inverter's local portal. (only needed for fronius_gen24)

- **`inverter.password`**:  
  The password for the inverter's local portal. (only needed for fronius_gen24)

- **`inverter.max_grid_charge_rate`**:  
  The maximum grid charge rate, in watts (W). Limitation for calculating the target grid charge power and for EOS inverter model. (currently not supported by evcc external battery control, but shown and calculated - reachable per **EOS connect** API)

- **`inverter.max_pv_charge_rate`**:  
  The maximum PV charge rate, in watts (W). Limitation for calculating the target pv charge power and for EOS inverter model. (currently not supported by evcc external battery control, but shown and calculated - reachable per **EOS connect** API)

---

### EVCC Configuration

- **`evcc.url`**:  
  The URL for the EVCC instance (e.g., `http://<ip>:7070`). If not used set to `url: ""` or leave as `url: http://yourEVCCserver:7070`

---

### MQTT Configuration

The `mqtt` section allows you to configure the MQTT broker and Home Assistant MQTT Auto Discovery settings.

#### Parameters

- **`mqtt.enabled`**:  
  Enable or disable MQTT functionality. 
  - `true`: Enable MQTT.  
  - `false`: Disable MQTT.  

- **`mqtt.broker`**:  
  The address of the MQTT broker (e.g., `localhost` or `192.168.1.10`).

- **`mqtt.port`**:  
  The port of the MQTT broker. Default: `1883`.

- **`mqtt.user`**:  
  The username for authenticating with the MQTT broker (optional).

- **`mqtt.password`**:  
  The password for authenticating with the MQTT broker (optional).

- **`mqtt.tls`**:  
  Enable or disable TLS for secure MQTT connections.  
  - `true`: Use TLS for secure connections.  
  - `false`: Do not use TLS.  

- **`mqtt.ha_mqtt_auto_discovery`**:  
  Enable or disable Home Assistant MQTT Auto Discovery.  
  - `true`: Enable Auto Discovery.  
  - `false`: Disable Auto Discovery.  

- **`mqtt.ha_mqtt_auto_discovery_prefix`**:  
  The prefix for Home Assistant MQTT Auto Discovery topics. Default: `homeassistant`.

---

### Other Configuration Settings

- **`refresh_time`**:  
  Default refresh time for the application, in minutes.

- **`time_zone`**:  
  Default time zone for the application.

- **`eos_connect_web_port`**:  
  Default port for the EOS Connect server.

- **`log_level`**:  
  Log level for the application. Possible values: `debug`, `info`, `warning`, `error`.

---

## Notes

- Ensure that the `config.yaml` file is located in the same directory as the application.
- If the configuration file does not exist, the application will create one with default values and prompt you to restart the server after configuring the settings.

## Config examples

### Full Config Example (will be generated at first startup)

```yaml
# Load configuration
load:
  source: default  # Data source for load power - openhab, homeassistant, default (using a static load profile)
  url: http://homeassistant:8123 # URL for openhab or homeassistant (e.g. http://openhab:8080 or http://homeassistant:8123)
  access_token: abc123 # access token for homeassistant (optional)
  load_sensor: Load_Power # item / entity for load power data in watts
  car_charge_load_sensor: Wallbox_Power # item / entity for wallbox power data in watts. (If not needed, set to `load.car_charge_load_sensor: ""`)
  additional_load_1_sensor: "additional_load_1_sensor" # item / entity for wallbox power data in watts. (If not needed set to `additional_load_1_sensor: ""`)
  additional_load_1_runtime: 2 # runtime for additional load 1 in minutes - default: 0 (If not needed set to `additional_load_1_sensor: ""`)
  additional_load_1_consumption: 1500 # consumption for additional load 1 in Wh - default: 0 (If not needed set to `additional_load_1_sensor: ""`)
# EOS server configuration
eos:
  server: 192.168.1.94  # EOS server address
  port: 8503 # port for EOS server - default: 8503
  timeout: 180 # timeout for EOS optimize request in seconds - default: 180
# Electricity price configuration
price:
  source: default  # data source for electricity price tibber, smartenergy_at, fixed_24h, default (default uses akkudoktor)
  token: tibberBearerToken # Token for electricity price
  fixed_24h_array: 10.41, 10.42, 10.42, 10.42, 10.42, 23.52, 28.17, 28.17, 28.17, 28.17, 28.17, 23.52, 23.52, 23.52, 23.52, 28.17, 28.17, 34.28, 34.28, 34.28, 34.28, 34.28, 28.17, 23.52 # 24 hours array with fixed prices over the day
  feed_in_price: 0.0 # feed in price for the grid in €/kWh
  negative_price_switch: false # switch for no payment if negative stock price is given
# battery configuration
battery:
  source: default  # Data source for battery soc - openhab, homeassistant, default
  url: http://homeassistant:8123 # URL for openhab or homeassistant (e.g. http://openhab:7070 or http://homeassistant:8123)
  soc_sensor: battery_SOC # item / entity for battery SOC data in [0..1]
  access_token: abc123 # access token for homeassistant (optional)
  capacity_wh: 11059 # battery capacity in Wh
  charge_efficiency: 0.88 # efficiency for charging the battery in [0..1]
  discharge_efficiency: 0.88 # efficiency for discharging the battery in [0..1]
  max_charge_power_w: 5000 # max charging power in W
  min_soc_percentage: 5 # URL for battery soc in %
  max_soc_percentage: 100 # URL for battery soc in %
  price_euro_per_wh_accu: 0 # price for battery in €/Wh
  charging_curve_enabled: true # enable dynamic charging curve for battery
# List of PV forecast source configuration
pv_forecast_source:
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, forecast_solar, default (default uses akkudoktor)
# List of PV forecast configurations. Add multiple entries as needed.
# See Akkudoktor API (https://api.akkudoktor.net/#/pv%20generation%20calculation/getForecast) for more details.
pv_forecast:
  - name: myPvInstallation1  # User-defined identifier for the PV installation, have to be unique if you use more installations
    lat: 47.5 # Latitude for PV forecast @ Akkudoktor API
    lon: 8.5 # Longitude for PV forecast @ Akkudoktor API
    azimuth: 90.0 # Azimuth for PV forecast @ Akkudoktor API
    tilt: 30.0 # Tilt for PV forecast @ Akkudoktor API
    power: 4600 # Power for PV forecast @ Akkudoktor API
    powerInverter: 5000 # Power Inverter for PV forecast @ Akkudoktor API
    inverterEfficiency: 0.9 # Inverter Efficiency for PV forecast @ Akkudoktor API
    horizon: 10,20,10,15 # Horizon to calculate shading up to 360 values to describe shading situation for your PV.
# Inverter configuration
inverter:
  type: default  # Type of inverter - fronius_gen24, evcc, default (default will disable inverter control - only displaying the target state) - preset: default
  address: 192.168.1.12 # Address of the inverter (fronius_gen24 only)
  user: customer # Username for the inverter (fronius_gen24 only)
  password: abc123 # Password for the inverter (fronius_gen24 only)
  max_grid_charge_rate: 5000 # Max inverter grid charge rate in W - default: 5000
  max_pv_charge_rate: 5000 # Max imverter PV charge rate in W - default: 5000
# EVCC configuration
evcc:
  url: http://yourEVCCserver:7070  # URL to your evcc installation, if not used set to "" or leave as http://yourEVCCserver:7070
mqtt:
  enabled: false # Enable MQTT - default: false
  broker: localhost # URL for MQTT server - default: mqtt://yourMQTTserver
  port: 1883 # Port for MQTT server - default: 1883
  user: mqtt_user # Username for MQTT server - default: mqtt
  password: mqtt_password # Password for MQTT server - default: mqtt
  tls: false # Use TLS for MQTT server - default: false
  ha_mqtt_auto_discovery: true # Enable Home Assistant MQTT auto discovery - default: true
  ha_mqtt_auto_discovery_prefix: homeassistant # Prefix for Home Assistant MQTT auto discovery - default: homeassistant
refresh_time: 3 # Default refresh time of EOS connect in minutes - default: 3
time_zone: Europe/Berlin # Default time zone - default: Europe/Berlin
eos_connect_web_port: 8081 # Default port for EOS connect server - default: 8081
log_level: info # Log level for the application : debug, info, warning, error - default: info
```

### Minimal possible Config Example

*Hint: Within HA addon config the params that are not needed will be integrated automatically again after saving. Here please use the setting for unsed params wit `""`.*

```yaml
# Load configuration
load:
  source: default  # Data source for load power - openhab, homeassistant, default (using a static load profile)
  load_sensor: Load_Power # item / entity for load power data in watts
  car_charge_load_sensor: Wallbox_Power # item / entity for wallbox power data in watts. (If not needed, set to `load.car_charge_load_sensor: ""`)
# EOS server configuration
eos:
  server: 192.168.1.94  # EOS server address
  port: 8503 # port for EOS server - default: 8503
  timeout: 180 # timeout for EOS optimize request in seconds - default: 180
# Electricity price configuration
price:
  source: default  # data source for electricity price tibber, smartenergy_at, fixed_24h, default (default uses akkudoktor)
# battery configuration
battery:
  source: default  # Data source for battery soc - openhab, homeassistant, default
  capacity_wh: 11059 # battery capacity in Wh
  charge_efficiency: 0.88 # efficiency for charging the battery in [0..1]
  discharge_efficiency: 0.88 # efficiency for discharging the battery in [0..1]
  max_charge_power_w: 5000 # max charging power in W
  min_soc_percentage: 5 # URL for battery soc in %
  max_soc_percentage: 100 # URL for battery soc in %
  price_euro_per_wh_accu: 0 # price for battery in €/Wh
  charging_curve_enabled: true # enable dynamic charging curve for battery
# List of PV forecast source configuration
pv_forecast_source:
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, forecast_solar, default (default uses akkudoktor)
# List of PV forecast configurations. Add multiple entries as needed.
# See Akkudoktor API (https://api.akkudoktor.net/#/pv%20generation%20calculation/getForecast) for more details.
pv_forecast:
  - name: myPvInstallation1  # User-defined identifier for the PV installation, have to be unique if you use more installations
    lat: 47.5 # Latitude for PV forecast @ Akkudoktor API
    lon: 8.5 # Longitude for PV forecast @ Akkudoktor API
    azimuth: 90.0 # Azimuth for PV forecast @ Akkudoktor API
    tilt: 30.0 # Tilt for PV forecast @ Akkudoktor API
    power: 4600 # Power for PV forecast @ Akkudoktor API
    powerInverter: 5000 # Power Inverter for PV forecast @ Akkudoktor API
    inverterEfficiency: 0.9 # Inverter Efficiency for PV forecast @ Akkudoktor API
    horizon: 10,20,10,15 # Horizon to calculate shading up to 360 values to describe shading situation for your PV.
# Inverter configuration
inverter:
  type: default  # Type of inverter - fronius_gen24, evcc, default (default will disable inverter control - only displaying the target state) - preset: default
  max_grid_charge_rate: 5000 # Max inverter grid charge rate in W - default: 5000
  max_pv_charge_rate: 5000 # Max imverter PV charge rate in W - default: 5000
# EVCC configuration
evcc:
  url: http://yourEVCCserver:7070  # URL to your evcc installation, if not used set to "" or leave as http://yourEVCCserver:7070
mqtt:
  enabled: false # Enable MQTT - default: false
refresh_time: 3 # Default refresh time of EOS connect in minutes - default: 3
time_zone: Europe/Berlin # Default time zone - default: Europe/Berlin
eos_connect_web_port: 8081 # Default port for EOS connect server - default: 8081
log_level: info # Log level for the application : debug, info, warning, error - default: info
```