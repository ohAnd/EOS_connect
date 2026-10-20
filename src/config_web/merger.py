"""
Merged Config Builder — Produces a config dict in the EXACT same shape as
``config_manager.config`` so that all existing interfaces work without changes.

Reads bootstrap values from config.yaml (via ConfigManager) and all other values
from the SQLite store. Resolves the unified ``data_source`` into per-section
``source``/``url``/``access_token`` fields so interfaces receive the same dict shape.
"""

import logging
import re
from typing import Any, Optional

from .store import ConfigStore
from .schema import ConfigSchema, BOOTSTRAP_KEYS, LIST_SECTIONS

logger = logging.getLogger("__main__")

# Sections that receive data_source inheritance
_DATA_SOURCE_SECTIONS = ("load", "battery")

# data_source.type values the PV autoscaler can actually read a counter from.
_AUTOSCALER_SOURCES = frozenset({"homeassistant", "openhab", "evcc"})


def build_merged_config(
    bootstrap_config: dict,
    store: ConfigStore,
    schema: ConfigSchema,
    resolved_out: Optional[dict] = None,
) -> dict:
    """
    Build a merged config dict that has the EXACT same shape as ``config_manager.config``.

    Priority order for each value:
    1. SQLite store (user-edited via web UI)
    2. Bootstrap config.yaml (for bootstrap-only keys)
    3. Schema defaults (fallback)

    What the EVCC data source resolves (the address, the virtual entities, the
    battery parameters EVCC reports) is *derived* here on every merge and never
    stored. Storing it pinned values that then went stale - a capacity from
    before the battery was extended, an address that could no longer be edited
    because the next merge wrote the old one back - and it put derived values
    into the backup file, which ``/api/config/export`` doubles as.

    Args:
        bootstrap_config: The ``config_manager.config`` dict (for bootstrap keys).
        store: An opened ConfigStore with migrated/edited values.
        schema: The ConfigSchema registry.
        resolved_out: Optional dict, filled with the flat dot-keyed values the
            EVCC resolution derived. The settings UI shows these for the fields
            it renders read-only; everything else reads the store as before.

    Returns:
        A nested dict identical in shape to the original config dict.
    """
    all_settings = store.get_all()
    defaults = schema.defaults_dict()
    result = {}

    # Build sections from schema
    for section in schema.sections():
        if section == "data_source":
            # data_source is internal — not passed to interfaces
            continue

        if section == "system":
            # System-level keys are top-level (no nesting)
            continue

        if section in LIST_SECTIONS:
            # Built as a list further down, not as a flat group of keys.
            continue

        result[section] = _build_section(
            section, all_settings, bootstrap_config, defaults
        )

    # Top-level (system) keys
    for field_def in schema.get_section("system"):
        key = field_def.key
        if key in BOOTSTRAP_KEYS:
            result[key] = bootstrap_config.get(key, field_def.default)
        elif key in all_settings:
            result[key] = all_settings[key]
        else:
            result[key] = bootstrap_config.get(key, field_def.default)

    # pv_forecast — list of installations
    result["pv_forecast"] = _build_pv_forecast(
        all_settings, bootstrap_config, defaults, schema
    )

    # managed_loads — list of appliances. Unlike pv_forecast, missing keys are *not*
    # filled from the schema: the defaults that matter here depend on the entry's type,
    # and ``loads.presets.apply_defaults`` is the one place that knows them. Filling
    # them here as well would give a sauna a pool's swimming season.
    result["managed_loads"] = _build_managed_loads(
        all_settings, bootstrap_config, defaults
    )

    # Resolve data_source -> load/battery connection fields
    evcc_resolved = _apply_data_source_inheritance(result, all_settings)

    # Inject data_source credentials to inverter when type is homeassistant
    _apply_inverter_data_source_injection(result, all_settings)

    # Apply central HA data source for price and PV sources
    _apply_central_ha_data_source(result, all_settings, evcc_resolved)

    # Handed to the caller rather than written anywhere. ``store`` stays
    # read-only for the whole of this function - that is the point.
    if resolved_out is not None:
        resolved_out.clear()
        resolved_out.update(evcc_resolved)

    return result


def _build_section(
    section: str,
    all_settings: dict[str, Any],
    bootstrap_config: dict,
    defaults: dict,
) -> dict:
    """Build one config section dict from store values, falling back to defaults."""
    section_dict = {}
    section_defaults = defaults.get(section, {})
    bootstrap_section = bootstrap_config.get(section, {})

    # Collect all keys for this section from store
    prefix = f"{section}."
    for key, value in all_settings.items():
        if key.startswith(prefix):
            subkey = key[len(prefix) :]
            section_dict[subkey] = value

    # Fill in missing keys from bootstrap config, then defaults
    if isinstance(section_defaults, dict):
        for subkey, default_val in section_defaults.items():
            if subkey not in section_dict:
                # Try bootstrap config
                if isinstance(bootstrap_section, dict) and subkey in bootstrap_section:
                    section_dict[subkey] = bootstrap_section[subkey]
                else:
                    section_dict[subkey] = default_val

    return section_dict


def _build_pv_forecast(
    all_settings: dict[str, Any],
    bootstrap_config: dict,
    defaults: dict,
    schema: ConfigSchema,
) -> list:
    """
    Rebuild the pv_forecast list from store values.

    Two storage formats are supported:
    1. Web UI format (preferred): indexed keys like ``pv_forecast.0.name``,
       ``pv_forecast.1.lat``, etc.
    2. Migration format (fallback): entire list under key ``"pv_forecast"``

    Missing fields in entries are filled with schema defaults.
    Falls back to bootstrap config if neither is found.
    """
    # Try format 1: indexed keys (from web UI) — preferred, checked first
    result_dict = {}
    for key, value in all_settings.items():
        if key.startswith("pv_forecast."):
            # Extract index and subkey: pv_forecast.0.name -> (0, "name")
            parts = key.split(".")
            if len(parts) >= 3 and parts[1].isdigit():
                idx = int(parts[1])
                subkey = ".".join(parts[2:])
                if idx not in result_dict:
                    result_dict[idx] = {}
                result_dict[idx][subkey] = value

    if result_dict:
        # Rebuild list from dict, sorted by index, filling in missing fields from schema
        result_list = []
        for i in sorted(result_dict.keys()):
            entry = result_dict[i].copy()
            # Fill in missing fields with schema defaults
            for field_def in schema.get_section("pv_forecast"):
                # Extract the field name (e.g., "pv_forecast.name" -> "name")
                field_name = field_def.key.split(".")[-1]
                if field_name not in entry:
                    entry[field_name] = field_def.default
            result_list.append(entry)
        return result_list

    # Try format 1.5: unindexed template keys like pv_forecast.name, pv_forecast.lat
    template_entry = {}
    for key, value in all_settings.items():
        if key.startswith("pv_forecast.") and not re.match(r"^pv_forecast\.\d+\.", key):
            parts = key.split(".")
            if len(parts) == 2:
                template_entry[parts[1]] = value

    if template_entry:
        logger.warning(
            "[Merger] Found unindexed pv_forecast template keys in the store; synthesizing one installation"
        )
        entry = template_entry.copy()
        for field_def in schema.get_section("pv_forecast"):
            field_name = field_def.key.split(".")[-1]
            if field_name not in entry:
                entry[field_name] = field_def.default
        return [entry]

    # Try format 2: single key (from migration) — fallback
    if "pv_forecast" in all_settings:
        stored = all_settings["pv_forecast"]
        if isinstance(stored, list):
            return stored

    # Last resort: fallback to bootstrap config or defaults
    return bootstrap_config.get(
        "pv_forecast",
        defaults.get("pv_forecast", []),
    )


def _build_managed_loads(
    all_settings: dict[str, Any],
    bootstrap_config: dict,
    defaults: dict,
) -> list:
    """
    Rebuild the managed_loads list from indexed store keys.

    Entries are returned as stored, with only ``id`` normalised and unusable ones
    dropped. Per-type defaults are deliberately *not* applied here - see the comment at
    the call site - so an entry may legitimately be missing most of its keys.

    Two storage formats are supported, mirroring ``_build_pv_forecast``:
    indexed keys (``managed_loads.0.type``), and the whole list under one key, which is
    what a restored backup carries.
    """
    entries: dict[int, dict] = {}
    for key, value in all_settings.items():
        match = re.match(r"^managed_loads\.(\d+)\.(.+)$", key)
        if match:
            index = int(match.group(1))
            entries.setdefault(index, {})[match.group(2)] = value

    if entries:
        result = []
        for index in sorted(entries):
            entry = dict(entries[index])
            entry["id"] = str(entry.get("id", "") or "").strip()
            if not entry["id"] or not entry.get("type"):
                # An entry with no id has no MQTT topic and no API path, and one with
                # no type has no model. Dropping it here keeps that judgement out of
                # the runtime, which would otherwise log the same error every cycle.
                logger.warning(
                    "[Merger] managed_loads entry %d has no id or no type - ignoring it",
                    index,
                )
                continue
            result.append(entry)
        return result

    stored = all_settings.get("managed_loads")
    if isinstance(stored, list):
        return [entry for entry in stored if isinstance(entry, dict)]

    return bootstrap_config.get("managed_loads", defaults.get("managed_loads", []))


def _apply_data_source_inheritance(
    result: dict, all_settings: dict[str, Any]
) -> dict[str, Any]:
    """
    Resolve ``data_source.*`` into ``load`` and ``battery`` connection fields.

    Per-section overrides (Expert level) take precedence. If a section already has
    its own ``source``/``url``/``access_token`` with non-default values, those win.
    Otherwise the global ``data_source`` values are injected.

    A central EVCC source adds one more injection the other types do not need: the
    EVCC URL lives in the ``evcc`` section, and either copy of it —
    ``data_source.url`` or ``evcc.url`` — should configure both. The placeholder is
    skipped so a half-configured setup does not look connected.

    Returns:
        Flat dot-keyed values the resolution derived, for the settings UI to
        display. Empty when EVCC is not involved - the plain HA/openHAB
        inheritance is recomputed on every merge and has nothing to show.
    """
    resolved: dict[str, Any] = {}
    ds_type = all_settings.get("data_source.type", "default")
    ds_url = all_settings.get("data_source.url", "")
    ds_token = all_settings.get("data_source.access_token", "")
    ds_ssl_ignore = all_settings.get("data_source.ssl_ignore", False)

    evcc_url = str(all_settings.get("evcc.url", "") or "").strip()
    if ds_type == "evcc":
        # One address, two keys. The Data Source URL field is the input: what
        # the user last typed there wins, and the ``evcc`` section follows it so
        # the roles reading that section - inverter controller, price and PV
        # source - point at the same instance. Only while the field still holds
        # a shipped default of the *previous* connection is it treated as empty
        # and the evcc section's own address stands in.
        #
        # Nothing is written back. The earlier version mirrored the two keys
        # into the store, which meant the next merge overwrote a freshly typed
        # address with the old one - the EVCC URL could not be changed at all
        # once it had been set, because the page carrying the other copy is
        # hidden while EVCC is the data source.
        typed = str(all_settings.get("data_source.url", "") or "").strip()
        if typed in ("", "http://homeassistant:8123", "http://openhab:8080"):
            typed = ""
        resolved_url = typed or (evcc_url if _is_evcc_url_usable(evcc_url) else "")
        if resolved_url and isinstance(result.get("evcc"), dict):
            result["evcc"]["url"] = resolved_url
        resolved["data_source.url"] = resolved_url
        ds_url = resolved_url
        ds_token = ""

    for section in _DATA_SOURCE_SECTIONS:
        if section not in result:
            continue

        sec = result[section]

        # Only inherit if section doesn't have its own override
        current_source = sec.get("source", "default")
        if current_source in ("default", "", None):
            sec["source"] = ds_type
            sec["url"] = ds_url
            sec["access_token"] = ds_token
        # Inject ssl_ignore regardless of source override
        sec["ssl_ignore"] = ds_ssl_ignore
        # If section has its own source set, keep it (Expert override)

    if ds_type == "evcc":
        resolved.update(_apply_evcc_virtual_entities(result, all_settings) or {})
        resolved.update(_apply_evcc_battery_entities(result, all_settings) or {})
    return resolved


def _is_evcc_placeholder(url) -> bool:
    """Whether *url* is empty, blank, or still the schema's EVCC placeholder."""
    if not url or not isinstance(url, str):
        return True
    stripped = url.strip()
    return not stripped or stripped == "http://yourEVCCserver:7070"


def _is_evcc_url_usable(url) -> bool:
    """A real EVCC address: set, and not the schema placeholder."""
    return not _is_evcc_placeholder(url)


def _apply_evcc_virtual_entities(
    result: dict, all_settings: dict[str, Any]
) -> dict[str, Any]:
    """
    Pre-fill the load section's virtual entities when EVCC becomes the data source.

    The household load reads group ``home``. That group is EVCC's *residual*
    house consumption - ``grid + max(0, pv) + battery - totalChargePower`` - so
    it is already the base load this interface wants, with every loadpoint taken
    out. The wallbox field therefore stays empty here; see below.

    Only ``group:home`` is derivable. Everything else EVCC meters carries a
    title its owner chose, so the additional load is the user's to name - all
    this does there is clear a name the new source cannot answer.
    """
    load = result.get("load")
    if not isinstance(load, dict):
        return {}

    # The inheritance loop above has already written "evcc" into source - either
    # from the central setting or because the section overrode it the same way.
    # Keying on that value covers both arrivals; a load still on another source
    # (an expert kept Home Assistant for it) stays untouched.
    evcc_now = load.get("source") == "evcc"

    # A sensor name left from a Home Assistant setup is stale on an EVCC source:
    # grammar-valid entities are kept, anything else is pre-filled, so a plain
    # switch of the data source produces a working setup rather than a load
    # profile that fails its grammar check into the built-in curve. The user is
    # told in the web UI what was filled (the fields are visible there either way).
    ha_sensors = {
        "load_sensor",
        "additional_load_1_sensor",
    }
    stale = {
        key: load.get(key, "") or ""
        for key in ha_sensors
        if evcc_now
        and str(load.get(key, "") or "").strip()
        and not _is_evcc_virtual_entity(load.get(key))
    }

    if evcc_now and not str(load.get("load_sensor", "") or "").strip():
        load["load_sensor"] = "group:home"
    if evcc_now and "load_sensor" in stale and "sensor." in stale["load_sensor"]:
        load["load_sensor"] = "group:home"
    if evcc_now:
        # Nothing to name for the wallbox, and naming it is actively wrong:
        # ``group:home`` has already had every loadpoint subtracted by EVCC, so
        # the per-slot subtraction in get_load_profile_for_day would take the
        # charging energy off a base load that never carried it. Where the house
        # drew more than the car, the profile silently lost that energy; where it
        # drew less, the guard held and every charging slot logged "household
        # load smaller than controllables" instead. A name carried over from a
        # Home Assistant setup is cleared for the same reason.
        load["car_charge_load_sensor"] = ""
    if evcc_now and "additional_load_1_sensor" in stale:
        # Cleared, not guessed. EVCC meters appliances under names their owner
        # chose, so there is no name to derive - the user picks the series on
        # EVCC's own energy page and types "group:consumer:<title>" here. What
        # must not stay is the Home Assistant entity id: this source cannot
        # answer it, so it would silently subtract nothing while looking set.
        load["additional_load_1_sensor"] = ""

    # Only the user-visible sensor names are worth showing; source and url are
    # recomputed every pass and the user never sees them on these pages.
    return {
        f"load.{key}": load[key]
        for key in ("load_sensor", "car_charge_load_sensor", "additional_load_1_sensor")
        if key in load
    }


def _is_evcc_virtual_entity(entity) -> bool:
    """Whether *entity* already IS a virtual entity (group:/tariff: grammar)."""
    if not entity or not isinstance(entity, str):
        return False
    parts = entity.strip().split(":")
    if parts[0] == "tariff" and len(parts) == 2:
        return True
    return parts[0] == "group" and len(parts) in (2, 3)


def _apply_evcc_battery_entities(
    result: dict, all_settings: dict[str, Any]
) -> dict[str, Any]:
    """
    Switch the battery section's stale Home Assistant names to EVCC equivalents.

    The SOC sensor moves onto the live state (``battery.soc``); the five price
    calculation sensors move onto the history groups they measure. A sensor name
    this source cannot answer is how a switched setup ends up with a static 5%
    SOC and a price calculation that silently stops updating — the same failure
    the load section's prefill exists for.

    Battery physics come from EVCC too, when it reports them: capacity from the
    battery meter, and the SoC bounds EVCC's own management uses (bufferSoc /
    prioritySoc). Only values EVCC actually answers are written; an expert-set
    manual value already matching stays untouched, and an unreachable EVCC
    simply leaves the configured numbers in place.
    """
    battery = result.get("battery")
    if not isinstance(battery, dict) or battery.get("source") != "evcc":
        return {}

    soc = str(battery.get("soc_sensor", "") or "").strip()
    if soc and not _is_evcc_virtual_entity(soc) and not soc.startswith("battery."):
        battery["soc_sensor"] = "battery.soc"

    # The connection the battery's own sensors answer through. The EVCC URL the
    # section inherited only reaches /api/state and /api/history/*; a sensor
    # name carried over from an Home Assistant setup (the battery temperature is
    # the recurring case) needs the supplementary connection to stay readable.
    # A non-remote supplementary choice means "no second connection": the keys
    # are blanked rather than carried over from the previous save.
    supp_type = str(all_settings.get("data_source.supplementary_type", "") or "")
    supplementary_usable = supp_type in ("homeassistant", "openhab")
    battery["supplementary_type"] = supp_type if supplementary_usable else ""
    battery["supplementary_url"] = (
        str(all_settings.get("data_source.supplementary_url", "") or "")
        if supplementary_usable
        else ""
    )
    battery["supplementary_access_token"] = (
        str(all_settings.get("data_source.supplementary_access_token", "") or "")
        if supplementary_usable
        else ""
    )
    battery["supplementary_ssl_ignore"] = (
        bool(all_settings.get("data_source.supplementary_ssl_ignore", False))
        if supplementary_usable
        else False
    )

    price_sensors = {
        "battery_power_sensor": "group:battery",
        "pv_power_sensor": "group:pv",
        "grid_power_sensor": "group:grid",
        "load_power_sensor": "group:home",
        "price_sensor": "tariff:grid",
    }
    for key, virtual in price_sensors.items():
        current = str(battery.get(key, "") or "").strip()
        if current and not _is_evcc_virtual_entity(current):
            battery[key] = virtual

    # Capacity is a physical fact EVCC's meter knows better than a hand-typed
    # number. The SOC bounds are deliberately NOT auto-filled: EVCC's
    # The SoC window comes from ``battery.devices[]``, where EVCC reads it off
    # each battery through its BatterySocLimiter capability. NOT from the
    # site-level bufferSoc/prioritySoc: those are *charging behaviour*
    # thresholds (when surplus stops being stored, what stays in reserve), and
    # reading them as the physical range once took a site with priority 70 as
    # "min SOC 70%" and locked the optimizer out of three quarters of the
    # battery. Efficiency and charge-power limits are not exposed by EVCC at
    # all and stay configurable exactly as they were.
    parameters = None
    try:
        # Imported late: interfaces is heavier than this module's import graph
        # wants at module load, and EVCC mode is the only case that needs it.
        # Both spellings, because this package is a top-level one at runtime
        # (the container starts from src/) and a sub-package under the tests -
        # the same dual import entity_probe.py carries, guarded by
        # tests/config_web/test_runtime_import_layout.py.
        try:
            from interfaces.evcc_parameters import fetch_evcc_parameters
        except ImportError:
            from ..interfaces.evcc_parameters import fetch_evcc_parameters

        parameters = fetch_evcc_parameters(battery.get("url", ""))
    except (ImportError, OSError, ValueError):
        logger.warning(
            "[Merger] EVCC battery parameters could not be read; "
            "keeping the configured values",
            exc_info=True,
        )
    # Only what EVCC actually answered. A battery whose integration carries no
    # SoC limits, or an EVCC that is simply not reachable, leaves the configured
    # values standing - the same rule the sensor names above follow.
    from_evcc = ("capacity_wh", "min_soc_percentage", "max_soc_percentage")
    for key in from_evcc:
        if parameters and key in parameters:
            battery[key] = parameters[key]

    resolved: dict[str, Any] = {}
    for key in (
        "soc_sensor",
        "battery_power_sensor",
        "pv_power_sensor",
        "grid_power_sensor",
        "load_power_sensor",
        "price_sensor",
    ):
        if key in battery:
            resolved[f"battery.{key}"] = battery[key]
    # The physical parameters are only "resolved" when EVCC supplied them;
    # otherwise the field must stay an ordinary input the user can set.
    for key in from_evcc:
        if parameters and key in parameters:
            resolved[f"battery.{key}"] = battery[key]
    # Supplementary connection: computed per merge, nothing for the UI to show.
    return resolved


def _apply_inverter_data_source_injection(
    result: dict, all_settings: dict[str, Any]
) -> None:
    """
    Inject data_source URL and token to inverter when type is homeassistant.

    When inverter.type is "homeassistant", inject the data_source.url and
    data_source.access_token as inverter.url and inverter.token respectively.
    This ensures the inverter uses the same HA credentials as the load/battery interfaces.
    """
    if "inverter" not in result:
        return

    inverter = result["inverter"]
    if inverter.get("type") != "homeassistant":
        return

    ds_url = all_settings.get("data_source.url", "")
    ds_token = all_settings.get("data_source.access_token", "")
    ds_ssl_ignore = all_settings.get("data_source.ssl_ignore", False)

    # Inject credentials from data_source
    inverter["url"] = ds_url
    inverter["token"] = ds_token
    inverter["ssl_ignore"] = ds_ssl_ignore


_CENTRAL_HA_SECTIONS = (
    ("price", "", "sensor.grid_prices"),
    ("pv_forecast_source", "", "sensor.pv_forecast"),
    ("price", "feed_in_", "sensor.feed_in_prices"),
)


def _apply_central_ha_data_source(
    result: dict, all_settings: dict[str, Any], evcc_resolved: Optional[dict] = None
) -> None:
    """
    Apply the central data source to every section that opted in.

    When a section's use_ha_central_data_source toggle is true, its connection
    details are constructed from the centrally configured data_source (url and
    access_token), avoiding repetition for end users. The timeseries-capable
    domains come from _CENTRAL_HA_SECTIONS; pv_autoscaling keeps its own block
    because its key names and source handling differ.

    An EVCC data source splits the two apart, because the two needs differ:

    - the timeseries domains want a *named Home Assistant entity* read from
      ``/api/states/<name>``, an endpoint EVCC does not have. They go to the
      supplementary connection, which exists for exactly this, and are left
      alone with a warning when there is none. Handing them the EVCC address
      produced a URL that could only 404.
    - the PV auto-scaling counter EVCC *does* have: ``pvEnergy`` in its live
      state is the site's cumulative PV energy, which is what that field asks
      for. No second connection needed.

    Args:
        result: The merged config dict to modify in-place.
        all_settings: All settings from the store.
        evcc_resolved: The EVCC resolution so far; the counter name is added to
            it, and the resolved address is read from it rather than from the
            store, which may hold either copy.
    """
    ds_type = all_settings.get("data_source.type", "default")
    ds_url = all_settings.get("data_source.url", "")
    ds_token = all_settings.get("data_source.access_token", "")
    ds_ssl_ignore = all_settings.get("data_source.ssl_ignore", False)

    evcc_url = ""
    if ds_type == "evcc":
        evcc_url = (evcc_resolved or {}).get("data_source.url", "") or ""
        supp_type = str(all_settings.get("data_source.supplementary_type", "") or "")
        if supp_type in ("homeassistant", "openhab"):
            ds_url = str(all_settings.get("data_source.supplementary_url", "") or "")
            ds_token = str(
                all_settings.get("data_source.supplementary_access_token", "") or ""
            )
            ds_ssl_ignore = bool(
                all_settings.get("data_source.supplementary_ssl_ignore", False)
            )
        else:
            ds_url, ds_token = "", ""

    # One spec per timeseries-capable domain instead of a copy-pasted block per
    # section: (section, key prefix within it, default sensor). Must stay in
    # step with api._TIMESERIES_DOMAIN_SECTIONS — a test pins the two together.
    for section, prefix, default_sensor in _CENTRAL_HA_SECTIONS:
        target = result.get(section)
        if not isinstance(target, dict):
            continue
        if not target.get(f"{prefix}use_ha_central_data_source"):
            continue
        if ds_type == "evcc" and not ds_url:
            logger.warning(
                "[ConfigMerger] %s%s reads a Home Assistant entity through the "
                "central data source, which is EVCC - it has no /api/states "
                "endpoint. Configure the supplementary connection under Settings "
                "> Data Source, or give the domain its own URL.",
                section,
                f".{prefix.rstrip('_')}" if prefix else "",
            )
            continue
        sensor_name = target.get(f"{prefix}ha_sensor_name", default_sensor)
        # Construct HA API URL from sensor entity
        target[f"{prefix}data_url"] = f"{ds_url}/api/states/{sensor_name}"
        target[f"{prefix}data_token"] = ds_token

    # Apply to pv_autoscaling section
    if "pv_autoscaling" in result:
        pv_auto = result["pv_autoscaling"]
        if pv_auto.get("use_ha_central_data_source"):
            if ds_type == "evcc":
                pv_auto["url"] = evcc_url
                pv_auto["access_token"] = ""
                pv_auto["ssl_ignore"] = bool(
                    all_settings.get("data_source.ssl_ignore", False)
                )
                pv_auto["src"] = "evcc"
                # ``pvEnergy`` is the site's cumulative PV energy in EVCC's live
                # state, read through the same state reader as battery.soc. A
                # name left from a Home Assistant setup cannot resolve there, so
                # it is replaced rather than carried over.
                current = str(pv_auto.get("sensor_entity_id", "") or "").strip()
                if not current or current.startswith("sensor."):
                    pv_auto["sensor_entity_id"] = "pvEnergy"
                if evcc_resolved is not None:
                    evcc_resolved["pv_autoscaling.sensor_entity_id"] = pv_auto[
                        "sensor_entity_id"
                    ]
                return
            pv_auto["url"] = ds_url
            pv_auto["access_token"] = ds_token
            pv_auto["ssl_ignore"] = ds_ssl_ignore
            # The autoscaler reads its counter from Home Assistant or openHAB only.
            # data_source.type also allows "default", which is its schema default, so
            # copying it through unchecked would write a value the pv_autoscaling.src
            # choices forbid and leave the autoscaler raising on every poll. Keep the
            # section's own src in that case so the misconfiguration stays visible.
            if ds_type in _AUTOSCALER_SOURCES:
                pv_auto["src"] = ds_type
            elif pv_auto.get("enabled"):
                logger.warning(
                    "[ConfigMerger] pv_autoscaling is enabled with central data source "
                    "type '%s', which cannot provide a PV counter. Set data_source.type "
                    "to one of %s, or configure pv_autoscaling.src manually.",
                    ds_type,
                    ", ".join(sorted(_AUTOSCALER_SOURCES)),
                )
