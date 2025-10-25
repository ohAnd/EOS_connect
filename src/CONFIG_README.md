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
    - [Example: Using EVCC for PV Forecasts](#example-using-evcc-for-pv-forecasts)
    - [Example: Using Solcast for PV Forecasts](#example-using-solcast-for-pv-forecasts)
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
  Overall consumption of additional load 1 in Wh for the given hours. Set to 0 if not needed. (If not needed, set to `additional_load_1_consumption: ""`)

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
  Data source for electricity prices. Possible values: `tibber`, `smartenergy_at`, `stromligning`, `fixed_24h`, `default` (default uses akkudoktor API).

- **`price.token`**:  
  Token for accessing electricity price data. (If not needed, set to `token: ""`)

  When used with **Tibber**:

  Provide your token

  When used with **Strømligning**:
  - Use the format: `supplierId/productId[/customerGroupId]` (customer group is optional).  
    - Example with customer group: `radius_c/velkommen_gron_el/c`  
    - Example without customer group: `nke-elnet/forsyningen`  
  - You can find the appropriate values on the [Strømligning live page](https://stromligning.dk/live) or via the [API docs](https://stromligning.dk/api/docs/swagger.json#/Prices/get_api_prices). On the site, select the desired "netselskab" and supplier/product; copy the `netselskab` part to `supplierId`, the `produkt` part to `productId`, and the optional group to `customerGroupId`.

- **`fixed_price_adder_ct`**:
  Describes the fixed cost addition in ct per kWh. Only applied to source default (akkudoktor).
  
- **`relative_price_multiplier`**:
  Applied to (base energy price + fixed_price_adder_ct). Use a decimal (e.g., 0.05 for 5%). Only applied to source default (akkudoktor).

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

  *Hint for openhab: Supported format is decimal (0-1) or percentage (0 -100) or with UoM ('0 %'- '100 %')*

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

**Important:** All supported PV forecast providers (akkudoktor, openmeteo, openmeteo_local, forecast_solar, evcc) use the same azimuth convention where **0° = South** and **negative values = East**. No conversion is needed when switching between providers.

`pv_forecast_source` section declares the provider of solar forecast that should be used. Available providers are
- `akkudoktor` - https://api.akkudoktor.net/ - direct request and results
- `openmeteo` - https://open-meteo.com/en/docs - uses the [open-meteo-solar-forecast](https://github.com/rany2/open-meteo-solar-forecast) (no horizon possible by the lib at this time)
- `openmeteo_local` - https://open-meteo.com/en/docs - gathering radiation and cloudcover data and calculating locally with an own model - still in dev to improve the calculation
- `forecast_solar` - https://doc.forecast.solar/api - direct request and results
- `solcast` - https://solcast.com/ - high-precision solar forecasting using satellite data and machine learning models. Requires creating a rooftop site in your Solcast account and using the resource_id (not location coordinates). Free API key provides up to 10 calls per day.
- `evcc` - retrieves forecasts from an existing EVCC installation via API - requires EVCC section to be configured
default is uses akkudoktor

**Temperature Forecasts**: EOS Connect also fetches temperature forecasts to improve optimization accuracy, as temperature affects battery efficiency and energy consumption patterns. When using provider-specific configurations (akkudoktor, openmeteo, etc.), temperature data is automatically retrieved using the same geographical coordinates. When using EVCC, localized temperature forecasts require at least one PV configuration entry with coordinates.

`pv_forecast` section allows you to define multiple PV forecast entries, each distinguished by a user-given name. Below is an example of a default PV forecast configuration:

```yaml
pv_forecast_source:
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, openmeteo_local, forecast_solar, evcc, solcast, default (default uses akkudoktor)
  api_key: "" # API key for Solcast (required only when source is 'solcast')
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
    resource_id: "" # Resource ID for Solcast (required only when source is 'solcast')
```

#### Parameters
- **`name`**:  
  A user-defined identifier for the PV installation. Must be unique if you use multiple installations.

- **`lat`**:  
  Latitude for the PV forecast.

- **`lon`**:  
  Longitude for the PV forecast.

- **`azimuth`**:  
  Azimuth angle for the PV forecast in degrees. **All supported forecast providers use the same solar/PV industry standard convention:**
  - **0° = South** (optimal orientation for Northern Hemisphere)
  - **90° = West**
  - **180° = North** 
  - **-90° = East** (negative values for east-facing)
  
  **Example orientations:** A south-facing roof would use `azimuth: 0`, while a garage facing southeast would use `azimuth: -45`, and a carport facing west would use `azimuth: 90`. An east-facing installation would use `azimuth: -90`.

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

- **`resource_id`**:  
  (Solcast only) The resource ID from your Solcast rooftop site configuration. Required when using Solcast as the PV forecast source. Not used by other providers.


**Special Configuration for Solcast:**

When using `source: solcast`, the configuration requirements are different:

- **`api_key`** (in `pv_forecast_source`): Required. Your Solcast API key obtained from your Solcast account.
- **`resource_id`** (in each `pv_forecast` entry): Required. The resource ID from your Solcast rooftop site configuration.
- **Location parameters for temperature forecasts**: While `azimuth`, `tilt`, and `horizon` are configured in your Solcast dashboard and ignored by EOS Connect, **`lat` and `lon` are still required** for fetching temperature forecasts that EOS needs for accurate optimization calculations.
- **`power`, `powerInverter`, `inverterEfficiency`**: Still required for system scaling and efficiency calculations.

**Setting up Solcast:**
1. Create a free account at [solcast.com](https://solcast.com/)
2. Configure a "Rooftop Site" with your PV system details (location, tilt, azimuth, capacity)
3. Copy the Resource ID from your rooftop site
4. Get your API key from the account settings
5. Use these values in your EOS Connect configuration (including lat/lon for temperature forecasts)

---

### Inverter Configuration

- **`inverter.type`**:  
  Specifies the type of inverter. Possible values:  
  - `fronius_gen24`: Use the Fronius Gen24 inverter (enhanced V2 interface with firmware-based authentication for all firmware versions).
  - `fronius_gen24_legacy`: Use the Fronius Gen24 inverter (legacy V1 interface for corner cases).
  - `evcc`: Use the universal interface via evcc external battery control (evcc config below has to be valid).
  - `default`: Disable inverter control (only display the target state).

- **`inverter.address`**:  
  The IP address of the inverter. (only needed for fronius_gen24/fronius_gen24_legacy)

- **`inverter.user`**:  
  The username for the inverter's local portal. (only needed for fronius_gen24/fronius_gen24_legacy)

- **`inverter.password`**:  
  The password for the inverter's local portal. (only needed for fronius_gen24/fronius_gen24_legacy)
  
  **Note for enhanced interface**: The default `fronius_gen24` interface automatically detects your firmware version and uses the appropriate authentication method. If you recently updated your inverter firmware to 1.38.6-1+ or newer, you may need to reset your password in the WebUI (http://your-inverter-ip/) under Settings -> User Management. New firmware versions require password reset after updates to enable the improved encryption method.

- **`inverter.max_grid_charge_rate`**:  
  The maximum grid charge rate, in watts (W). Limitation for calculating the target grid charge power and for EOS inverter model. (currently not supported by evcc external battery control, but shown and calculated - reachable per **EOS connect** API)

- **`inverter.max_pv_charge_rate`**:  
  The maximum PV charge rate, in watts (W). Limitation for calculating the target pv charge power and for EOS inverter model. (currently not supported by evcc external battery control, but shown and calculated - reachable per **EOS connect** API)

---

### EVCC Configuration

- **`evcc.url`**:  
  The URL for the EVCC instance (e.g., `http://<ip>:7070`). If not used set to `url: ""` or leave as `url: http://yourEVCCserver:7070`

**Note**: When using `evcc` as the `pv_forecast_source`, this EVCC configuration must be properly configured. EOS Connect will retrieve PV forecasts directly from the EVCC API instead of using individual PV installation configurations. In this case, the `pv_forecast` section can be left empty or minimal, as EVCC provides the aggregated forecast data.

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
  source: default  # data source for electricity price tibber, smartenergy_at, stromligning, fixed_24h, default (default uses akkudoktor)
  token: tibberBearerToken # Token for electricity price (for Stromligning use supplierId/productId[/customerGroupId])
  fixed_price_adder_ct: 2.5 # Describes the fixed cost addition in ct per kWh.
  relative_price_multiplier: 0.05 # Applied to (base energy price + fixed_price_adder_ct). Use a decimal (e.g., 0.05 for 5%).
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
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, openmeteo_local, forecast_solar, evcc, solcast, default (default uses akkudoktor)
  api_key: "" # API key for Solcast (required only when source is 'solcast')
# List of PV forecast configurations. Add multiple entries as needed.
# See Akkudtor API (https://api.akkudoktor.net/#/pv%20generation%20calculation/getForecast) for more details.
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
    resource_id: "" # Resource ID for Solcast (required only when source is 'solcast')
# Inverter configuration
inverter:
  type: default  # Type of inverter - fronius_gen24, fronius_gen24_legacy, evcc, default (default will disable inverter control - only displaying the target state) - preset: default
  address: 192.168.1.12 # Address of the inverter (fronius_gen24, fronius_gen24_legacy only)
  user: customer # Username for the inverter (fronius_gen24, fronius_gen24_legacy only)
  password: abc123 # Password for the inverter (fronius_gen24, fronius_gen24_legacy only)
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
  source: default  # data source for electricity price tibber, smartenergy_at, stromligning, fixed_24h, default (default uses akkudoktor)
  token: "" # Provide Tibber token or Stromligning supplierId/productId[/customerGroupId] when needed
  fixed_price_adder_ct: 0 # Describes the fixed cost addition in ct per kWh.
  relative_price_multiplier: 0 # Applied to (base energy price + fixed_price_adder_ct). Use a decimal (e.g., 0.05 for 5%).
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
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, openmeteo_local, forecast_solar, evcc, solcast, default (default uses akkudoktor)
  api_key: "" # API key for Solcast (required only when source is 'solcast')
# List of PV forecast configurations. Add multiple entries as needed.
# See Akkudtor API (https://api.akkudoktor.net/#/pv%20generation%20calculation/getForecast) for more details.
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
  type: default  # Type of inverter - fronius_gen24, fronius_gen24_legacy, evcc, default (default will disable inverter control - only displaying the target state) - preset: default
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

### Example: Using EVCC for PV Forecasts

When using EVCC as your PV forecast source, the configuration is simplified as EVCC provides the aggregated forecast data:

```yaml
# PV forecast source configuration - using EVCC
pv_forecast_source:
  source: evcc # Use EVCC for PV forecasts
pv_forecast:
  - name: "Location for Temperature" # At least one entry needed for temperature forecasts
    lat: 47.5 # Required for temperature forecasts used by EOS optimization
    lon: 8.5 # Required for temperature forecasts used by EOS optimization
    # Other parameters (azimuth, tilt, power, etc.) not used for PV forecasts but can be included
# EVCC configuration - REQUIRED when using evcc as pv_forecast_source
evcc:
  url: http://192.168.1.100:7070  # URL to your EVCC installation
```

In this configuration:
- EVCC handles all PV installation details and provides aggregated forecasts
- The `pv_forecast` section requires at least one entry with valid `lat` and `lon` coordinates for temperature forecasts that EOS needs for accurate optimization
- The `evcc.url` must point to a reachable EVCC instance with API access enabled
- Temperature forecasts are essential for EOS optimization calculations, regardless of PV forecast source

### Example: Using Solcast for PV Forecasts

When using Solcast as your PV forecast source, you need to configure your rooftop sites in the Solcast dashboard first:

```yaml
# PV forecast source configuration - using Solcast
pv_forecast_source:
  source: solcast # Use Solcast for PV forecasts
  api_key: "your_solcast_api_key_here" # Your Solcast API key (required)

# PV forecast configurations using Solcast resource IDs
pv_forecast:
  - name: "Main Roof South"
    resource_id: "abcd-efgh-1234-5678" # Resource ID from Solcast dashboard
    lat: 47.5 # Required for temperature forecasts used by EOS optimization
    lon: 8.5 # Required for temperature forecasts used by EOS optimization
    power: 5000 # Still needed for system scaling
    powerInverter: 5000
    inverterEfficiency: 0.95
    # azimuth, tilt, horizon not used for PV forecasts - configured in Solcast dashboard
  - name: "Garage East"
    resource_id: "ijkl-mnop-9999-0000" # Different resource ID for second installation
    lat: 47.5 # Same location coordinates can be used for multiple installations
    lon: 8.5
    power: 2500
    powerInverter: 3000
    inverterEfficiency: 0.92
```

**Important Solcast Rate Limiting Information:**

- Each PV installation requires a separate rooftop site configured in your Solcast account
- Physical PV parameters (tilt, azimuth) are configured in the Solcast dashboard, not in EOS Connect
- **Location coordinates (lat, lon) are still required** for temperature forecasts that EOS uses for optimization calculations
- The `resource_id` is obtained from your Solcast rooftop site configuration
- `power`, `powerInverter`, and `inverterEfficiency` are still required for proper system scaling
- **Free Solcast accounts are limited to 10 API calls per day**
- **EOS Connect automatically extends update intervals to 2.5 hours when using Solcast** to stay within the 10 calls/day limit (9.6 calls/day actual usage)
- Multiple PV installations will result in multiple API calls per update cycle - consider this when planning your configuration
- If you exceed rate limits, EOS Connect will use the previous forecast data until the next successful API call
