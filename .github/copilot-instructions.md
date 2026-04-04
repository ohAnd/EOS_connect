# GitHub Copilot Instructions for EOS Connect

## Project Guidelines

### Icon Usage

- **Always use FontAwesome icons (free tier only)** for all documentation and web interfaces
- **Never use emoji icons** - they have been replaced with FontAwesome for consistency and professionalism
- The main application icon is located in `/docs/assets/images/icon.png` and `/docs/assets/images/logo.png`
- FontAwesome CDN: https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css

### Design Style

- Follow the dark theme established in `src/web/css/style.css`
- Color scheme:
  - Primary background: `rgb(54, 54, 54)`
  - Secondary background: `rgb(78, 78, 78)`
  - Accent color: `#4a9eff`
  - Border radius: `10px`
- Maintain responsive design patterns

### Documentation

#### Structure

- GitHub Pages documentation is in `/docs` folder
- Structure: 4 main sections (what-is, user-guide, advanced, developer)
- Use HTML for documentation pages (better styling control than Markdown)
- Keep README.md and CONFIG_README.md concise with links to full docs

#### Documentation Update Workflow

**When preparing to commit (NEVER stage changed files and commit automatically - always review changes first and ask for confirmation):**

1. **Update README.md** - Minimal info only, focus on quick start + links to GitHub Pages
2. **Update src/CONFIG_README.md** - Essential configuration overview + links to full docs
3. **Update GitHub Pages** (`/docs` folder) - Complete, detailed documentation
   - Always write from **user perspective** (except developer section)
   - Main focus: **"Easy entry for new and existing users"**
   - Keep all pages current with latest features and changes
   - Use clear, practical examples

#### Documentation Perspective

- **what-is/**, **user-guide/**, **advanced/**: Write for end users (clear, accessible language)
- **developer/**: Write for contributors (technical details, architecture)
- All documentation should help users quickly understand and use EOS Connect
- Avoid jargon unless necessary; explain technical concepts simply

### Project Role Clarity

- **EOS Connect is an integration and control platform**, NOT an optimizer
- The optimization calculations are performed by external servers:
  - Akkudoktor EOS Server (https://github.com/Akkudoktor-EOS/EOS)
  - EVopt (https://github.com/thecem/hassio-evopt)
- Always clarify this distinction in documentation and code comments

### Code Style

- Follow existing Python conventions in the codebase
- Use type hints where appropriate
- Include docstrings for classes and functions
- Follow pylint recommendations for formatting

### Code Changes & Documentation Alignment

**MANDATORY: Every code change, new feature, or bugfix MUST be reflected in documentation**

When making ANY code changes:

1. **Identify Documentation Impact**: Determine which doc sections are affected
   - New features → Update what-is, user-guide, and advanced pages
   - Configuration changes → Update user-guide/configuration.html
   - API changes → Update advanced/index.html (REST API & MQTT sections)
   - Bug fixes → Update troubleshooting in user-guide if user-facing

2. **Update All Affected Pages**: Changes must be synchronized across:
   - `/docs` GitHub Pages (primary documentation)
   - `README.md` (if quick start or core features affected)
   - `src/CONFIG_README.md` (if configuration parameters changed) - NOTE: This file is being deprecated, integrate changes into README.md instead

3. **Maintain Accuracy**: Documentation must match actual code behavior
   - Verify API endpoint responses match code
   - Confirm MQTT topic names and payloads match implementation
   - Validate configuration parameter names, types, and valid values
   - Update examples to reflect current best practices

4. **Version Consistency**: When `src/version.py` is updated, ensure version display is current on all doc pages

**Failure to update documentation is considered incomplete work**

### Commit Preparation

- **NEVER commit automatically** - only prepare changes for user review
- When asked to "prepare to commit", ensure documentation is up-to-date:
  1. Update README.md (minimal, with links)
  2. Update src/CONFIG_README.md (essential info, with links)
  3. Update GitHub Pages documentation (complete details)
- Present a summary of changes for user to review before committing

### Testing Phase Documentation

- **ENERGYFORECAST_TESTING.md**: Temporary file for develop branch testing
  - Contains Smart Price Prediction testing guide
  - **MUST BE DELETED** when merging to main
  - Full documentation already exists in `/docs/user-guide/configuration.html#energyforecast`
  - Purpose: Provide accessible docs while feature is on develop (GitHub Pages shows main only)
  - **Reminder**: Check for and remove any similar `*_TESTING.md` files before merging features to main

### Config Schema Maintenance

- Every new config field **MUST** be added to `src/config_web/schema.py`
- Schema fields: `key`, `type`, `default`, `section`, `level`, `label`, `description`, `help_url`, `validation`, `depends_on`, `hot_reload`, `display_group`
- After schema changes, run: `python scripts/export_config_schema.py`
- The Config Schema is the **SINGLE SOURCE OF TRUTH** for field metadata
- Web UI and GitHub Pages docs both consume the exported JSON (`docs/assets/data/config_schema.json`)
- New fields must specify a `level`: `getting_started`, `standard`, or `expert`
- New experimental features should use the label `"experimental"`
- Hot-reloadable fields (applied without restart) must set `hot_reload=True` and have corresponding logic in `src/config_web/hot_reload.py`

### Config Web Module Architecture

The web-based configuration system lives in `src/config_web/` as a self-contained module. Understanding this architecture is essential for any config-related work.

#### Module Structure

| File            | Purpose                                                                         |
| --------------- | ------------------------------------------------------------------------------- |
| `__init__.py`   | `ConfigWebModule` facade — single entry point for the main app                  |
| `schema.py`     | **SPOT** — All field definitions (`FieldDef`), `BOOTSTRAP_KEYS`, `SECTION_META` |
| `store.py`      | SQLite persistence (WAL mode, thread-safe, change callbacks)                    |
| `migration.py`  | config.yaml → SQLite and HA options.json → SQLite migration                     |
| `merger.py`     | Builds merged config dict in same shape as old `config_manager.config`          |
| `api.py`        | Flask Blueprint with 10 REST endpoints at `/api/config/`                        |
| `hot_reload.py` | `HotReloadAdapter` — applies live changes to running interfaces                 |

#### Key Design Principles

- **Zero interface changes**: Interfaces receive the same dict shape they always did. The merger produces an identical structure.
- **SPOT (Single Point of Truth)**: `schema.py` defines all field metadata. The web UI, REST API validation, docs export, and merger all consume it.
- **Bootstrap vs Store**: ~5 bootstrap keys (`BOOTSTRAP_KEYS` in schema.py) stay in config.yaml/ENV/HA options. Everything else lives in SQLite.
- **Section metadata**: `SECTION_META` in schema.py defines icons + labels for all sections. Frontend and docs read from this — never hardcode section display info elsewhere.

#### Adding a New Config Field (Checklist)

1. Add `FieldDef(...)` to `_ALL_FIELDS` in `src/config_web/schema.py`
2. Run `python scripts/export_config_schema.py` to update docs JSON
3. **Done** — Web UI, API validation, docs table, migration, and merger all pick it up automatically
4. If hot-reloadable: also add to `_PRICE_FIELD_MAP` or `_BATTERY_SOC_FIELDS` in `hot_reload.py`

#### Adding a New Config Section

1. Add fields with the new `section` name in `schema.py`
2. Add entry to `SECTION_META` dict in `schema.py` (icon + label)
3. Run `python scripts/export_config_schema.py`
4. **Done** — Frontend falls back gracefully but `SECTION_META` gives it the right icon/label

#### REST API Endpoints (all under `/api/config/`)

| Method | Path                | Purpose                                                    |
| ------ | ------------------- | ---------------------------------------------------------- |
| GET    | `/schema`           | Full schema JSON (fields + section metadata)               |
| GET    | `/`                 | Current config values (passwords masked)                   |
| PUT    | `/`                 | Partial update (validates, categorizes restart/hot-reload) |
| GET    | `/section/<name>`   | Single section values                                      |
| POST   | `/validate`         | Validate without saving                                    |
| GET    | `/restart-required` | Pending restart-required fields                            |
| GET    | `/export`           | Export all settings as flat JSON                           |
| POST   | `/import`           | Import settings from JSON                                  |
| GET    | `/wizard-status`    | Setup wizard completion state                              |
| POST   | `/wizard-complete`  | Mark wizard as completed                                   |

#### SPOT Pipeline Flow

```
schema.py (Python) → export_config_schema.py → config_schema.json (docs)
                   → /api/config/schema (live API) → config.js (web UI)
                   → /api/config/schema (live API) → wizard.js (setup wizard)
```

### Testing

- Tests are located in `/tests` folder
- Mirror the source structure in test organization
- Use pytest for all testing

### Design Rules & Lessons Learned

These rules emerged from comprehensive manual testing (154 test cases) and must be followed in all future development.

#### Configuration Architecture

- **config.yaml is bootstrap-only**: After migration, config.yaml contains only `eos_connect_web_port`, `time_zone`, `log_level`, and optionally `data_path`. All other settings live in SQLite and are managed via the web UI.
- **Never add non-bootstrap keys to config.yaml**: New config fields go into `schema.py` only. The web UI, API, migration, and merger all pick them up automatically.
- **ConfigManager defaults must use valid values**: `create_default_config()` in `config.py` must use values that pass schema validation. The placeholder `"default"` caused a crash when OptimizationInterface received it as `eos.source`. Always use a real schema-valid default.
- **Schema choices are authoritative**: The `validation.choices` list in `FieldDef` is the single source of truth. Migration coercion validates against it and falls back to `field_def.default` for invalid values.

#### Fresh Install vs Migration Detection

- **`_has_user_configured_values()`** distinguishes real user configs from ConfigManager defaults by checking sentinel fields (source values, sensor names)
- **Wizard flag logic**: `_wizard_completed` is only set when real user config is detected during migration. Fresh installs (all defaults) leave this unset so the setup wizard appears.
- **Never hardcode wizard completion** in migration without checking for real values

#### Web UI Integration Rules

- **1-second polling loop**: `main.js` runs `init()` every second via `setInterval`. Any check triggered from `init()` must be guarded to avoid re-triggering (e.g., `_wizardCheckDone` flag in wizard.js)
- **Error overlay interaction**: The startup error overlay (`#overlay`) blocks the full page. Any overlay (wizard, config) that needs to appear on top must hide `#overlay` first.
- **Restart guidance**: When config changes require restart, show clear visual hints (amber banner) on both the wizard completion screen and the dashboard error overlay.
- **z-index management**: Wizard and full-screen overlays must have higher z-index than the startup overlay

#### Hot Reload Design

- **Schema flag drives behavior**: Set `hot_reload=True` in `FieldDef` to mark a field as live-reloadable
- **Adapter pattern**: `HotReloadAdapter` receives change callbacks from `ConfigStore` and applies them to running interface instances
- **Attribute mapping**: Each hot-reload field maps to a specific interface attribute name + coercion function
- **Side-effects**: Some changes trigger recalculations (e.g., feed-in price change triggers `__create_feedin_prices()`)
- **Config dict sync**: For battery SOC fields, update both the interface method (`set_min_soc()`) AND the `battery_data` dict to prevent clamping against stale values

#### Validation & Security

- **Max length validation**: All string fields should have reasonable `max_length` validation to prevent abuse
- **HTML injection prevention**: User input displayed in the web UI must be text-only (no innerHTML with user data)
- **Latin-1 password encoding**: Passwords containing non-Latin-1 characters cause Modbus/network errors. Validate at the API layer.
- **Unicode safety**: All text fields must handle Unicode correctly (UTF-8 throughout)

### Hot Reload — Current State & Expansion Priority

#### Currently Hot-Reloadable (9 fields, applied without restart)

**Price fields (4)** — via `_PRICE_FIELD_MAP` in `hot_reload.py`:

- `price.fixed_price_adder_ct` → `PriceInterface.fixed_price_adder_ct`
- `price.relative_price_multiplier` → `PriceInterface.relative_price_multiplier`
- `price.feed_in_price` → `PriceInterface.feed_in_tariff_price` (+ recalculates feed-in prices)
- `price.negative_price_switch` → `PriceInterface.negative_price_switch` (+ recalculates feed-in prices)

**Battery fields (5)** — via `_BATTERY_SOC_FIELDS` in `hot_reload.py`:

- `battery.min_soc_percentage` → `BatteryInterface.set_min_soc()` + `battery_data` dict
- `battery.max_soc_percentage` → `BatteryInterface.set_max_soc()` + `battery_data` dict
- `battery.charging_threshold_w` (schema flag set, adapter logic in battery price handler)
- `battery.grid_charge_threshold_w` (schema flag set, adapter logic in battery price handler)
- `battery.battery_price_include_feedin` (schema flag set, adapter logic in battery price handler)

#### Expansion Priority List

Fields grouped by predicted code complexity and user impact. Each group shares interface patterns, so implementing one makes the rest in that group trivial.

**Priority 1 — Simple attribute swaps (low effort, high user value)**

These fields are simple instance attributes that can be set at runtime:

| Group           | Fields                                                                                        | Interface                           | Change Required                                                             |
| --------------- | --------------------------------------------------------------------------------------------- | ----------------------------------- | --------------------------------------------------------------------------- |
| EOS tuning      | `eos.timeout`, `eos.time_frame`                                                               | OptimizationInterface               | Add to field map; attrs already stored as `self.timeout`, `self.time_frame` |
| EOS flags       | `eos.dyn_override_discharge_allowed_pv_greater_load`, `eos.pv_battery_charge_control_enabled` | BaseControl / OptimizationInterface | Simple bool attrs on the control instance                                   |
| Inverter limits | `inverter.max_grid_charge_rate`, `inverter.max_pv_charge_rate`                                | BaseInverter subclass               | Attrs already on inverter instances                                         |
| System          | `request_timeout`                                                                             | All interfaces                      | Update shared timeout value                                                 |
| System          | `refresh_time`                                                                                | Main loop                           | Update `setInterval` timing — need to store as mutable                      |

**Priority 2 — Requires recalculation or reconnect (medium effort)**

These need more than a simple attribute swap:

| Group              | Fields                                                                                                           | Interface           | Change Required                                  |
| ------------------ | ---------------------------------------------------------------------------------------------------------------- | ------------------- | ------------------------------------------------ |
| Battery capacity   | `battery.capacity_wh`, `battery.charge_efficiency`, `battery.discharge_efficiency`, `battery.max_charge_power_w` | BatteryInterface    | Update battery_data dict + recalc charging curve |
| Battery price calc | `battery.price_update_interval`, `battery.price_history_lookback_hours`, `battery.price_euro_per_wh_accu`        | BatteryPriceHandler | Restart timer or update interval                 |
| Price fixed array  | `price.fixed_24h_array`                                                                                          | PriceInterface      | Re-parse array + recalc prices                   |

**Priority 3 — Requires interface reconstruction (high effort, rare changes)**

These change fundamental interface identity (source, URL, credentials). Users rarely change these after initial setup:

| Group           | Fields                                                                            | Interface             | Change Required                               |
| --------------- | --------------------------------------------------------------------------------- | --------------------- | --------------------------------------------- |
| Data source     | `data_source.type`, `data_source.url`, `data_source.access_token`                 | Load, Battery         | Full interface re-init (different API client) |
| Sensor names    | `load.load_sensor`, `battery.soc_sensor`, all sensor fields                       | Load, Battery         | Could swap attrs, but untested behavior       |
| MQTT connection | `mqtt.broker`, `mqtt.port`, `mqtt.user`, `mqtt.password`, `mqtt.tls`              | MqttInterface         | Disconnect + reconnect                        |
| MQTT features   | `mqtt.ha_mqtt_auto_discovery`, `mqtt.ha_mqtt_auto_discovery_prefix`               | MqttInterface         | Re-publish discovery messages                 |
| Inverter type   | `inverter.type`, `inverter.address`, `inverter.user`, `inverter.password`         | InverterFactory       | Full reconstruction via factory               |
| PV sources      | `pv_forecast_source.source`, `pv_forecast_source.api_key`, all pv_forecast fields | PvInterface           | Re-init with new source/installations         |
| Price source    | `price.source`, `price.token`                                                     | PriceInterface        | Different API client                          |
| EOS backend     | `eos.source`, `eos.server`, `eos.port`                                            | OptimizationInterface | Different backend class                       |
| EVCC            | `evcc.url`                                                                        | EvccInterface         | New HTTP client                               |

**Priority 4 — Bootstrap keys (never hot-reloadable)**

These affect the application infrastructure itself:

- `eos_connect_web_port` — Flask server port (requires process restart)
- `time_zone` — System-wide timezone (affects all timestamp handling)
- `log_level` — Logging configuration (could be hot-reloaded but low priority)

#### Adding a New Hot-Reload Field (Checklist)

1. Set `hot_reload=True` in the `FieldDef` in `schema.py`
2. Add field mapping to `hot_reload.py`:
   - Simple attribute: add to `_PRICE_FIELD_MAP` or create new map
   - Method call: add elif branch in the appropriate `_apply_*` method
3. If side-effects needed (recalculation), add trigger set like `_FEEDIN_TRIGGERS`
4. Add tests in `tests/config_web/test_hot_reload.py`
5. Run `python scripts/export_config_schema.py`

### HA Addon Integration

#### Bootstrap Contract with ha_addons Repo

The HA addon (`ohAnd/ha_addons`) must provide exactly these options in its `config.yaml`:

```yaml
options:
  web_port: 8081
  time_zone: "Europe/Berlin"
  log_level: "INFO"

schema:
  web_port: int
  time_zone: str
  log_level: list(DEBUG|INFO|WARNING|ERROR)
```

These map to EOS Connect's `BOOTSTRAP_KEYS` via `_HA_BOOTSTRAP_MAP` in `config.py`.

#### What the Addon Must NOT Do

- **Do not include non-bootstrap config options** in `config.yaml`/`options.json` — all other settings are managed by the web UI and stored in SQLite
- **Do not mount config.yaml into the container** — it's optional; if absent, defaults are used and the wizard guides setup
- **Do not write to `/data/eos_connect.db`** — EOS Connect owns this file exclusively

#### What the Addon Must Provide

- **Persistent `/data/` volume**: SQLite DB lives at `/data/eos_connect.db`
- **Network access**: Port forwarding for the web UI (default 8081)
- **`HASSIO` or `HASSIO_TOKEN` env var**: Set by HA automatically, triggers addon mode
- **`/data/options.json`**: HA writes this with the 3 bootstrap values
- **webui declaration**: `http://[HOST]:[PORT:8081]` for HA sidebar integration

#### Legacy Migration Path

When upgrading from an old addon version that had full config in `options.json`:

1. EOS Connect detects non-bootstrap keys in `/data/options.json`
2. `migrate_ha_options_to_store()` imports them into SQLite
3. Old `options.json` keys are left in place (HA manages that file)
4. Wizard is marked complete — user sees their existing config in the web UI
