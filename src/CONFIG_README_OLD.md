# Configuration Guide

<!-- TOC -->
- [Configuration Guide](#configuration-guide)
- [Configuration](#configuration)
  - [Configuration Sections](#configuration-sections)
    - [Load Configuration](#load-configuration)
    - [EOS Server Configuration](#eos-server-configuration)
    - [Electricity Price Configuration](#electricity-price-configuration)
    - [Battery Configuration](#battery-configuration)
      - [Dynamic Battery Price Calculation](#dynamic-battery-price-calculation)
    - [PV Forecast Configuration](#pv-forecast-configuration)
      - [PV Forecast Source](#pv-forecast-source)
      - [PV Forecast installations](#pv-forecast-installations)
        - [Parameter Details](#parameter-details)
        - [Example Config Entry](#example-config-entry)
        - [Notes](#notes)
    - [Inverter Configuration](#inverter-configuration)
    - [EVCC Configuration](#evcc-configuration)
    - [MQTT Configuration](#mqtt-configuration)
      - [Parameters](#parameters)
    - [Other Configuration Settings](#other-configuration-settings)
  - [Notes](#notes-1)
    - [config.yaml](#configyaml)
    - [`refresh_time` and `time_frame`](#refresh_time-and-time_frame)
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

- **`eos.source`**:  
  EOS server source - eos_server, evopt, default (default uses eos_server)

- **`eos.server`**:  
  EOS or EVopt server address (e.g., `192.168.1.94`). (Mandatory)

- **`eos.port`**:  
  Port for EOS server (8503) or EVopt server (7050) - default: `8503` (Mandatory)

- **`timeout`**:  
  Timeout for EOS optimization requests, in seconds. Default: `180`. (Mandatory)

- **`time_frame`**:  
  Granularity of the optimization and forecast time steps, in seconds.  
  
  **Backend-Specific Capabilities:**  
  - **EOS Server:** Only supports `3600` (hourly granularity). 15-minute intervals cannot be used with EOS server.  
  - **EVopt:** Supports both `3600` (hourly) and `900` (15-minute). Use 900 for more precise, dynamic optimization.  
  
  This controls the resolution of the forecast and optimization arrays sent to the optimization backend.  
  If you set `time_frame: 900` with `eos_server`, it will be automatically corrected to 3600 at startup with a warning.  
  
  **Note:**  
  - `refresh_time` (see "Other Configuration Settings") controls how often EOS Connect sends a request to the optimization server.  
  - `time_frame` sets the time step granularity inside each optimization request.

  Example (EVopt with 15-minute precision):
  ```yaml
  eos:
    source: evopt
    server: 192.168.1.94
    port: 7050
    timeout: 180
    time_frame: 900   # EVopt supports 15-minute intervals for more precise optimization
  ```
  
  Example (EOS server - hourly only):
  ```yaml
  eos:
    source: eos_server
    server: 192.168.1.94
    port: 8503
    timeout: 180
    time_frame: 3600  # EOS server only supports hourly intervals
  ```

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

- **`battery.charging_curve_enabled`**:  
  Enables or disables the dynamic charging curve for the battery.  
  - `true`: The system will automatically adjust the maximum charging power based on two factors:
    - **SOC-based reduction**: At SOC ≤50%, battery charges at full configured power. Above 50%, power is exponentially reduced to protect battery health and optimize charging efficiency. At 95% SOC, power is reduced to ~5% of maximum.
    - **Temperature-based protection** (if `sensor_battery_temperature` is configured): Additional power reduction is applied during extreme temperatures:
      - Below 0°C: 5-7.5% power (prevents lithium plating in LiFePO4 batteries)
      - 0-5°C: 5-15% power (gradual warm-up)
      - 5-15°C: 15-100% power (transition to optimal range)
      - 15-45°C: 100% power (optimal operating range, only SOC-based reduction applies)
      - 45-50°C: 100-45% power (heat warning)
      - 50-60°C: 45-7.5% power (severe heat protection)
      - Above 60°C: 5-7.5% power (critical protection)
    - The final charging power is the product of both SOC and temperature multipliers (e.g., at -2°C with 51% SOC: ~0.65 kW instead of 10 kW)
  - `false`: The battery will always charge at the configured maximum power, regardless of SOC or temperature.  
  - **Default:** `true`

- **`battery.sensor_battery_temperature`**:  
  Sensor/item identifier for battery temperature in °C. Optional but highly recommended for battery protection.
  - If configured, enables automatic temperature-based charging power reduction to protect the battery from damage during cold/hot conditions.
  - Supports Home Assistant entities (e.g., `sensor.battery_temperature`) and OpenHAB items.
  - Valid temperature range: -30°C to 70°C (values outside this range are ignored for safety)
  - If not configured or sensor fails, temperature protection is disabled and only SOC-based curve is used.
  - **Default:** `""` (disabled)
  - **Example for BYD Battery Box**: `sensor.byd_battery_box_premium_hv_temperatur`

- **`battery.price_euro_per_wh_accu`**:
  Price for battery in €/Wh - can be used to shift the result over the day according to the available energy (more details follow).


- **`battery.price_euro_per_wh_sensor`**:  
 Sensor/item identifier that exposes the battery price in €/Wh. If `battery.source` is set to `homeassistant` or `openhab` and a sensor/item is configured here, the system will fetch the value from that sensor/item. If no sensor is configured, the static value at `price_euro_per_wh_accu` will be used. For Home Assistant use an entity ID (e.g., `sensor.battery_price`); for OpenHAB use an item name (e.g., `BatteryPrice`).

- **`battery.price_calculation_enabled`**:  
  Enables dynamic battery price calculation by analyzing historical charging events.  
  - `true`: The system will analyze the last 96 hours (configurable) of power data to determine the real cost of energy stored in the battery.  
  - `false`: Uses the static price or sensor value.  
  - **Default:** `false`

- **`battery.price_update_interval`**:  
  Interval in seconds between dynamic price recalculations.  
  - **Default:** `900` (15 minutes)

- **`battery.price_history_lookback_hours`**:  
  Number of hours of history to analyze for the price calculation.  
  - **Default:** `96`

- **`battery.battery_power_sensor`**:  
  Home Assistant entity ID or OpenHAB item name for the battery power sensor (in Watts). Positive values must represent charging.

- **`battery.pv_power_sensor`**:  
  Home Assistant entity ID or OpenHAB item name for the total PV power sensor (in Watts).

- **`battery.grid_power_sensor`**:  
  Home Assistant entity ID or OpenHAB item name for the grid power sensor (in Watts). Positive values represent import from the grid.

- **`battery.load_power_sensor`**:  
  Home Assistant entity ID or OpenHAB item name for the household load power sensor (in Watts).

- **`battery.price_sensor`**:  
  Home Assistant entity ID or OpenHAB item name for the current electricity price sensor (in €/kWh or ct/kWh).

- **`battery.charging_threshold_w`**:  
  Minimum battery power in Watts to consider the battery as "charging" during historical analysis.  
  - **Default:** `50.0`

- **`battery.grid_charge_threshold_w`**:  
  Minimum grid import power in Watts to attribute charging energy to the grid rather than PV surplus.  
  - **Default:** `100.0`



#### Dynamic Battery Price Calculation

When `price_calculation_enabled` is set to `true`, the system performs a detailed analysis of your battery's charging history to determine the real cost of the energy currently stored.

**How it works:**
1. **Event Detection:** The system scans historical data (default 48h) to identify "charging events" where the battery power was above the `charging_threshold_w`.
2. **Source Attribution:** For each event, it compares battery power with PV production and Grid import. If grid import is significant (above `grid_charge_threshold_w`), the energy is attributed to the grid at the current market price. Otherwise, it is attributed to PV surplus at the `feed_in_price` (opportunity cost).
3. **Inventory Valuation (LIFO):** Instead of a simple average, the system uses a **Last-In, First-Out** model. It looks at the most recent charging sessions that match your current battery level. This ensures the price reflects the actual "value" of the energy currently inside the battery.
4. **Optimizer Integration:** This resulting price is used by the optimizer to decide when it is profitable to discharge the battery.
5. **Efficiency:** To minimize API load, the system uses a two-step fetching strategy: it first fetches low-resolution data to find events, and then high-resolution data only for the specific periods when the battery was actually charging.

---

### PV Forecast Configuration

This section contains two subsections:

#### PV Forecast Source

The `pv_forecast_source` section defines which provider is used for solar generation forecasts.  
Supported sources are: `akkudoktor`, `openmeteo`, `openmeteo_local`, `forecast_solar`, `evcc`, `solcast`, and `default`.  
- **source**: Select the provider for PV forecasts.  
  - Example: `source: akkudoktor`
- **api_key**: Only required for Solcast. Enter your Solcast API key here if using `solcast` as the source.

Example:
```yaml
pv_forecast_source:
  source: akkudoktor
  api_key: "" # Only needed for Solcast
```

#### PV Forecast installations

Each entry in `pv_forecast` must follow these rules, depending on the selected `pv_forecast_source`:

| Parameter            | Required for Source(s)              | Type/Format    | Default/Notes                                                                                     |
| -------------------- | ----------------------------------- | -------------- | ------------------------------------------------------------------------------------------------- |
| `name`               | all                                 | string         | User-defined identifier. Must be unique if multiple installations.                                |
| `lat`                | all                                 | float          | Latitude of PV installation. ('evcc','solcast' only required for temperature forecasts.)          |
| `lon`                | all                                 | float          | Longitude of PV installation. ('evcc','solcast' only required for temperature forecasts.)         |
| `azimuth`            | all except `solcast`, `evcc`        | int/float      | Required. For `evcc`/`solcast`, not needed (in HA addon config set to 180)                        |
| `tilt`               | all except `solcast`, `evcc`        | int/float      | Required. For `evcc`/`solcast`, not needed (in HA addon config set to 25)                         |
| `power`              | all except `evcc`, `solcast`        | int/float      | Required. For `evcc`/`solcast`, not needed (in HA addon config set to 1000)                       |
| `powerInverter`      | all except `evcc`, `forecast_solar` | int/float      | Required. For `evcc`, `forecast_solar`, `solcast`, not needed (in HA addon config set to 1000)    |
| `inverterEfficiency` | all except `evcc`, `forecast_solar` | float          | Required. For `evcc`, `forecast_solar`, set to `1`, not needed (in HA addon config set to 1)      |
| `horizon`            | `openmeteo_local`, `forecast_solar` | list or string | Mandatory. If missing, defaults to `[0]*36` for `openmeteo_local`, `[0]*24` for `forecast_solar`. |
| `resource_id`        | `solcast`                           | string         | Required for Solcast. Must be set in each entry when using Solcast as the source.                 |

##### Parameter Details

- **name**:  
  User-defined identifier for the PV installation. Must be unique if you use multiple installations.

- **lat/lon**:  
  Latitude and longitude for the PV installation. Required for all sources except `evcc`.  
  *For Solcast, these are still required for temperature forecasts.*

- **azimuth**:  
  Azimuth angle in degrees. Required for all sources except `solcast` and `evcc`.  
  *If missing for `solcast` or `evcc`, defaults to `0`.*

- **tilt**:  
  Tilt angle in degrees. Required for all sources except `solcast` and `evcc`.  
  *If missing for `solcast` or `evcc`, defaults to `0`.*

- **power**:  
  PV installation power in watts. Required for all sources except `evcc` and `solcast`.  
  *For `evcc` and `solcast`, set to `0` (dummy value for temperature forecast).*

- **powerInverter**:  
  Inverter power in watts. Required for all sources except `evcc`, `forecast_solar`, and `solcast`.  
  *For `evcc`, `forecast_solar`, and `solcast`, set to `0` (dummy value for temperature forecast).*

- **inverterEfficiency**:  
  Inverter efficiency as a decimal between `0` and `1`. Required for all sources except `evcc`, `forecast_solar`, and `solcast`.  
  *For `evcc`, `forecast_solar`, and `solcast`, set to `0` (dummy value for temperature forecast).*

- **horizon**:  
  Shading situation for the PV installation.  
  - Mandatory for `openmeteo_local` and `forecast_solar`.  
  - If missing, defaults to `[0]*36` for `openmeteo_local`, `[0]*24` for `forecast_solar`.  
  - Can be a comma-separated string or a list of values.

- **resource_id**:  
  Required only for `solcast`.  
  - Must be set in each entry when using Solcast as the source.  
  - Used to identify the rooftop site in your Solcast account.

##### Example Config Entry

```yaml
pv_forecast:
  - name: Garden
    lat: 52.5200
    lon: 13.4050
    azimuth: 13
    tilt: 31
    power: 860
    powerInverter: 800
    inverterEfficiency: 0.95
    horizon: 0,0,0,0,0,0,0,0,50,70,0,0,0,0,0,0,0,0
    # resource_id: "your_solcast_resource_id"  # Only for Solcast
```

##### Notes

- For `evcc` and `solcast`, dummy values are set for `power`, `powerInverter`, and `inverterEfficiency` to enable temperature forecasts.
- For `openmeteo_local` and `forecast_solar`, ensure `horizon` is provided or defaults will be used.
- For `solcast`, both `api_key` (in `pv_forecast_source`) and `resource_id` (in each `pv_forecast` entry) are required.

Refer to this table and details when editing your `config.yaml` and for troubleshooting configuration errors.
- **`api_key`** (in `pv_forecast_source`): Required. Your Solcast API key obtained from your Solcast account.
- **`resource_id`** (in each `pv_forecast` entry): Required. The resource ID from your Solcast rooftop site configuration.
- **Location parameters for temperature forecasts**: For all PV forecast sources, EOS Connect requires temperature data for optimization. The temperature forecast is always retrieved from Akkudoktor, and therefore, the `lat` and `lon` parameters are mandatory for every PV installation entry, regardless of the selected forecast source.
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
  - `victron`: Use Victron Energy inverters.
  - `evcc`: Use the universal interface via evcc external battery control (evcc config below has to be valid).
  - `default`: Disable inverter control (only display the target state).

- **`inverter.address`**:  
  The IP address of the inverter. (only needed for fronius_gen24/fronius_gen24_legacy/victron)

- **`inverter.user`**:  
  The username for the inverter's local portal. (only needed for fronius_gen24/fronius_gen24_legacy/victron)

- **`inverter.password`**:  
  The password for the inverter's local portal. (only needed for fronius_gen24/fronius_gen24_legacy/victron)
  
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
  This sets how often EOS Connect sends an optimization request to the EOS server.

- **`time_zone`**:  
  Default time zone for the application.

- **`eos_connect_web_port`**:  
  Default port for the EOS Connect server.

- **`log_level`**:  
  Log level for the application. Possible values: `debug`, `info`, `warning`, `error`.

---

## Notes

### config.yaml
- Ensure that the `config.yaml` file is located in the same directory as the application.
- If the configuration file does not exist, the application will create one with default values and prompt you to restart the server after configuring the settings.

### `refresh_time` and `time_frame`

- `refresh_time` sets how often EOS Connect sends a new optimization request to the EOS server (e.g., every 3 minutes).
- `time_frame` sets the granularity of the optimization and forecast arrays inside each request (e.g., 900 for 15-minute steps, 3600 for hourly steps).
- For more precise optimization and control, set `time_frame: 900`. For legacy hourly operation, use `3600`.
- The combination of `refresh_time` and `time_frame` allows you to control both how frequently the system updates and how detailed the optimization is.


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
  source: eos_server  # EOS server source - eos_server, evopt, default (default uses eos_server)
  server: 192.168.1.94  # EOS server address
  port: 8503 # port for EOS server - default: 8503
  timeout: 180 # timeout for EOS optimize request in seconds - default: 180
  time_frame: 900 # granularity of optimization steps in seconds (900=15min, 3600=hourly)
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
  price_euro_per_wh_sensor: "" # Home Assistant entity (e.g. sensor.battery_price) providing €/Wh
  charging_curve_enabled: true # enable dynamic charging curve for battery (SOC-based + temperature-based if sensor configured)
  sensor_battery_temperature: "" # sensor for battery temperature in °C (e.g., sensor.byd_battery_box_premium_hv_temperatur) - enables temperature protection
# List of PV forecast source configuration
pv_forecast_source:
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, openmeteo_local, forecast_solar, evcc, solcast, default (default uses akkudoktor)
  api_key: "" # API key for Solcast (required only when source is 'solcast')
# List of PV forecast configurations. Add multiple entries as needed.
# See Akkudtor API (https://api.akkudoktor.net/#/pv%20generation%20calculation/getForecast) for more details.
pv_forecast:
  - name: myPvInstallation1  # User-defined identifier for the PV installation, have to be unique if you use more installations
    lat: 52.5200 # Latitude for PV forecast
    lon: 13.4050 # Longitude for PV forecast
    azimuth: 90.0 # Azimuth for PV forecast
    tilt: 30.0 # Tilt for PV forecast
    power: 4600 # Power for PV forecast
    powerInverter: 5000 # Power Inverter for PV forecast
    inverterEfficiency: 0.9 # Inverter Efficiency for PV forecast
    horizon: 10,20,10,15 # Horizon to calculate shading up to 360 values to describe shading situation for your PV.
    resource_id: "" # Resource ID for Solcast (required only when source is 'solcast')
# Inverter configuration
inverter:
  type: default  # Type of inverter - fronius_gen24, fronius_gen24_legacy, victron, evcc, default (default will disable inverter control - only displaying the target state) - preset: default
  address: 192.168.1.12 # Address of the inverter (fronius_gen24, fronius_gen24_legacy, victron only)
  user: customer # Username for the inverter (fronius_gen24, fronius_gen24_legacy, victron only)
  password: abc123 # Password for the inverter (fronius_gen24, fronius_gen24_legacy, victron only)
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
  source: eos_server  # EOS server source - eos_server, evopt, default (default uses eos_server)
  server: 192.168.1.94  # EOS server address
  port: 8503 # port for EOS server - default: 8503
  timeout: 180 # timeout for EOS optimize request in seconds - default: 180
  time_frame: 3600 # granularity of optimization steps in seconds (900=15min, 3600=hourly)
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
  charging_curve_enabled: true # enable dynamic charging curve for battery (SOC-based + temperature-based if sensor configured)
  sensor_battery_temperature: "" # sensor for battery temperature in °C - enables temperature protection (optional)
# List of PV forecast source configuration
pv_forecast_source:
  source: akkudoktor # data source for solar forecast providers akkudoktor, openmeteo, openmeteo_local, forecast_solar, evcc, solcast, default (default uses akkudoktor)
  api_key: "" # API key for Solcast (required only when source is 'solcast')
# List of PV forecast configurations. Add multiple entries as needed.
# See Akkudtor API (https://api.akkudoktor.net/#/pv%20generation%20calculation/getForecast) for more details.
pv_forecast:
  - name: myPvInstallation1  # User-defined identifier for the PV installation, have to be unique if you use more installations
    lat: 52.5200 # Latitude for PV forecast
    lon: 13.4050 # Longitude for PV forecast
    azimuth: 90.0 # Azimuth for PV forecast
    tilt: 30.0 # Tilt for PV forecast
    power: 4600 # Power for PV forecast
    powerInverter: 5000 # Power Inverter for PV forecast
    inverterEfficiency: 0.9 # Inverter Efficiency for PV forecast
    horizon: 10,20,10,15 # Horizon to calculate shading up to 360 values to describe shading situation for your PV.
# Inverter configuration
inverter:
  type: default  # Type of inverter - fronius_gen24, fronius_gen24_legacy, victron, evcc, default (default will disable inverter control - only displaying the target state) - preset: default
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
    lat: 52.5200 # Required for temperature forecasts used by EOS optimization
    lon: 13.4050 # Required for temperature forecasts used by EOS optimization
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
    lat: 52.5200 # Required for temperature forecasts used by EOS optimization
    lon: 13.4050 # Required for temperature forecasts used by EOS optimization
    power: 5000 # Still needed for system scaling
    # azimuth, tilt, horizon not used for PV forecasts - configured in Solcast dashboard
  - name: "Garage East"
    resource_id: "ijkl-mnop-9999-0000" # Different resource ID for second installation
    lat: 47.5 # Same location coordinates can be used for multiple installations
    lon: 8.5
    power: 2500
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
