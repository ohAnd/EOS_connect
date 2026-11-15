"""
The GENETIC path to an EOS server - the only one that plans in 15-minute slots.

EOS has two optimization entry points, and they are not interchangeable:

``POST /optimize``
    What EOS Connect has always used. It is hardwired to the GENETIC0 algorithm,
    whose ``interval_sec`` is a computed constant of 3600 - no setting makes it
    plan quarter-hourly. EOS marks it deprecated and says so itself: "Uses the
    `classic` GENETIC0 optimisation algorithm (__NO__ 15-minutes slots)."

``POST /v1/optimize``
    The current one, running GENETIC, whose interval accepts 900 or 3600. It
    exists only on recent EOS builds - 0.2.0 and 0.3.0 do not have it - which is
    why this module is selected by capability and never by configuration.

The contract is inverted compared to the old endpoint: hardware lives in the
server's ``devices/*`` configuration and the request carries only observations.
``RuntimeForecasts`` still accepts the series EOS Connect produces, under their
original names, so the forecasting stays here where it belongs.

Everything this module returns is translated back into the shape the rest of
EOS Connect already reads, so nothing outside the backend learns that a second
endpoint exists. See ``_to_midnight_anchored`` for the one real trap.
"""

import json
import logging
import time
from datetime import datetime, timedelta

import requests

try:  # running from src/ as a script - src/ is on sys.path
    from timeseries_grid import HORIZON_HOURS, horizon_slots, local_midnight
except ImportError:  # imported as src.interfaces... (tests)
    from ...timeseries_grid import HORIZON_HOURS, horizon_slots, local_midnight

logger = logging.getLogger("__main__")

#: The endpoint that distinguishes a server able to plan quarter-hourly.
GENETIC_OPTIMIZE_PATH = "/v1/optimize"

#: Device groups EOS Connect mirrors, mapped from the request keys it builds.
#: GENETIC accepts at most one device per group except home appliances, which
#: matches what EOS Connect sends: one battery, one inverter, one EV, one load.
_DEVICE_GROUPS = (
    ("pv_akku", "batteries"),
    ("inverter", "inverters"),
    ("eauto", "electric_vehicles"),
    ("dishwasher", "home_appliances"),
)

#: Keys that belong to the run rather than to the hardware. ``initial_soc_percentage``
#: changes every cycle and travels in the request's ``soc`` map instead.
_PER_RUN_KEYS = frozenset({"initial_soc_percentage"})

#: ``inverter.max_power_wh`` is the legacy request's spelling; the device
#: configuration calls the same number ``max_power_w``.
_RENAMED_DEVICE_KEYS = {"inverters": {"max_power_wh": "max_power_w"}}

#: The schema only a server with device profiles has. Its battery and inverter
#: settings no longer carry capacity, power or efficiencies as flat keys.
_DEVICE_PROFILE_SCHEMA = "BatteryProfileAdjustment"

#: Flat keys that move into ``profile_adjustment`` on a server with device
#: profiles, as (flat key, adjustment key) per group. The mapping is the one
#: EOS's own config-file migration applies (``configmigrate.py``), so a server
#: fed by EOS Connect ends up exactly where a migrated config file would.
_PROFILE_ADJUSTMENT_KEYS = {
    "batteries": (
        ("capacity_wh", "nominal_capacity_wh"),
        ("max_charge_power_w", "max_charge_power_w"),
    ),
    "electric_vehicles": (
        ("capacity_wh", "nominal_capacity_wh"),
        ("max_charge_power_w", "max_charge_power_w"),
    ),
    "inverters": (
        ("max_power_w", "nominal_power_w"),
        ("max_ac_charge_power_w", "max_ac_charge_power_w"),
    ),
}

#: Flat keys a server with device profiles takes from the profile instead.
_PROFILE_PROVIDED_KEYS = {
    "batteries": ("charging_efficiency", "discharging_efficiency"),
    "electric_vehicles": ("charging_efficiency", "discharging_efficiency"),
    "inverters": ("ac_to_dc_efficiency", "dc_to_ac_efficiency"),
}


def _fetch_openapi(base_url, timeout):
    """The server's OpenAPI document; raises on anything but a usable answer."""
    response = requests.get(base_url + "/openapi.json", timeout=timeout)
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError("OpenAPI document is not an object")
    return document


def supports_genetic_optimize(base_url, timeout=10):
    """
    Whether this EOS server exposes the current optimization endpoint.

    Two questions share this answer, because they have the same cause. Can the
    server plan in 15-minute slots? And should EOS Connect still be talking to
    the endpoint EOS marks deprecated? Both are yes exactly when
    ``/v1/optimize`` is there.

    Asks the server what it exposes rather than comparing version strings: the
    dev builds that carry the endpoint sort before the release they are named
    after, so a version comparison would have to encode build timestamps.

    Called before any backend exists - ``eos_connect`` has to decide the global
    slot width before it builds the optimization interface - so it takes a URL
    and not an instance.

    Args:
        base_url (str): Base URL of the EOS server.
        timeout (int): Seconds to wait for the server.

    Returns:
        bool: True only when the server is reachable AND exposes the endpoint.
        An unreachable server returns False: guessing yes would commit the whole
        data pipeline to 192 slots against a server that may not accept them,
        failing every cycle instead of one request.
    """
    try:
        paths = _fetch_openapi(base_url, timeout).get("paths", {})
    except (requests.exceptions.RequestException, ValueError, AttributeError) as err:
        logger.warning(
            "[OPT-EOS] Could not ask the EOS server at %s what it supports (%s: %s) - "
            "falling back to the deprecated endpoint",
            base_url,
            type(err).__name__,
            err,
        )
        return False
    return GENETIC_OPTIMIZE_PATH in paths


def supports_device_profiles(base_url, timeout=10):
    """
    Whether this EOS server describes batteries and inverters by profile.

    Such a server moved capacity and power limits into ``profile_adjustment``
    and takes efficiencies from the profile. It still answers HTTP 200 to the
    flat keys, but drops them without a word: a device written the old way
    ends up with no capacity at all. So the shape has to be chosen up front,
    and like ``supports_genetic_optimize`` it is asked, not derived from a
    version string.

    Args:
        base_url (str): Base URL of the EOS server.
        timeout (int): Seconds to wait for the server.

    Returns:
        bool: True when the server's schema has a battery profile adjustment.
        An unreachable server returns False, which keeps the flat shape that
        every server before profiles needs.
    """
    try:
        schemas = (
            _fetch_openapi(base_url, timeout).get("components", {}).get("schemas", {})
        )
    except (requests.exceptions.RequestException, ValueError, AttributeError) as err:
        logger.warning(
            "[OPT-EOS] Could not ask the EOS server at %s for its device schema "
            "(%s: %s) - writing devices without profiles",
            base_url,
            type(err).__name__,
            err,
        )
        return False
    return isinstance(schemas, dict) and _DEVICE_PROFILE_SCHEMA in schemas


def _to_profile_settings(group, settings):
    """
    Rewrite flat device settings into the shape of a server with profiles.

    Capacity and power limits move into ``profile_adjustment``. A battery's
    charge limit becomes its discharge limit as well: without profiles that one
    number capped both directions, and EOS's own migration carries it over the
    same way - otherwise the profile's default discharge power would apply.

    Efficiencies are dropped: the server takes them from the profile and has
    no setting left for them.

    Returns:
        tuple: ``(settings, dropped_keys)``.
    """
    converted = dict(settings)
    adjustment = {}
    for flat_key, adjustment_key in _PROFILE_ADJUSTMENT_KEYS.get(group, ()):
        value = converted.pop(flat_key, None)
        if value is not None:
            adjustment[adjustment_key] = value
    if (
        group in ("batteries", "electric_vehicles")
        and "max_charge_power_w" in adjustment
    ):
        adjustment.setdefault("max_discharge_power_w", adjustment["max_charge_power_w"])
    dropped = [key for key in _PROFILE_PROVIDED_KEYS.get(group, ()) if key in converted]
    for key in dropped:
        del converted[key]
    if adjustment:
        converted["profile_adjustment"] = adjustment
    return converted, dropped


class EOSGeneticPath:  # pylint: disable=too-many-instance-attributes
    """
    Translates EOS Connect's optimization request to and from ``/v1/optimize``.

    Holds no connection state of its own: the owning ``EOSBackend`` passes in
    its configuration accessors, so there is one place that knows how to talk
    to the server.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        base_url,
        time_frame_base,
        time_zone,
        get_config,
        set_config,
        device_profiles=False,
    ):
        """
        Args:
            base_url (str): Base URL of the EOS server.
            time_frame_base (int): Slot width in seconds, 900 or 3600.
            time_zone: Timezone the midnight-anchored arrays are built on.
            get_config (callable): ``path -> value``, from the owning backend.
            set_config (callable): ``path, value -> bool``, from the owning backend.
            device_profiles (bool): Whether the server describes devices by
                profile, see ``supports_device_profiles``.
        """
        self.base_url = base_url
        self.time_frame_base = time_frame_base
        self.time_zone = time_zone
        self._get_config = get_config
        self._set_config = set_config
        self.device_profiles = device_profiles
        #: What was last written to devices/*, so an unchanged cycle writes nothing.
        self._mirrored_devices = {}
        #: Groups whose dropped efficiencies were already reported.
        self._reported_dropped = set()

    # ------------------------------------------------------------------
    # Slot arithmetic
    # ------------------------------------------------------------------

    def _current_slot(self, now=None):
        """Slots elapsed since true local midnight - the arrays' own anchor."""
        now = now or datetime.now(self.time_zone)
        midnight = local_midnight(self.time_zone, now)
        return int((now - midnight).total_seconds()) // self.time_frame_base

    def _available_horizon_hours(self, now=None):
        """
        Whole hours of control the supplied series can still cover.

        EOS requires ``slot_of_now + horizon`` values counted from local
        midnight, and EOS Connect supplies a fixed 48 hours from there. So the
        horizon rolls with the clock: 48 h at midnight, 36 h at noon, and never
        below 24 h, because the window always reaches the day after tomorrow.

        Floored to whole hours because ``horizon_hours`` is an integer; the
        remainder is handled by padding the response, not by overstating what
        the data covers.
        """
        slots_per_hour = 3600 // self.time_frame_base
        return max(1, int(HORIZON_HOURS - self._current_slot(now) / slots_per_hour))

    # ------------------------------------------------------------------
    # Server configuration
    # ------------------------------------------------------------------

    def _mirror_devices(self, eos_request):
        """
        Copy the request's hardware into the server's ``devices/*`` configuration.

        ``/v1/optimize`` takes its devices from configuration, so what used to
        ride along in every request has to live on the server. The values come
        from the request EOS Connect already builds, which keeps a single source
        of truth; only a changed group is written.

        On a server with device profiles the settings are rewritten into that
        shape first - see ``_to_profile_settings``.
        """
        for request_key, group in _DEVICE_GROUPS:
            device = eos_request.get(request_key)
            if not isinstance(device, dict) or not device.get("device_id"):
                continue
            renames = _RENAMED_DEVICE_KEYS.get(group, {})
            settings = {
                renames.get(key, key): value
                for key, value in device.items()
                if key not in _PER_RUN_KEYS
            }
            if self.device_profiles:
                settings, dropped = _to_profile_settings(group, settings)
                self._report_dropped(group, device["device_id"], dropped)
            payload = {device["device_id"]: settings}
            if self._mirrored_devices.get(group) == payload:
                continue
            if self._set_config(f"devices/{group}", payload):
                self._mirrored_devices[group] = payload
                logger.debug(
                    "[OPT-EOS] Mirrored %s into the EOS device configuration", group
                )

    def _report_dropped(self, group, device_id, dropped):
        """Say once per group that EOS uses its profile's efficiencies, not ours."""
        if not dropped or group in self._reported_dropped:
            return
        self._reported_dropped.add(group)
        logger.warning(
            "[OPT-EOS] EOS takes the efficiencies of '%s' from its device profile - "
            "%s from EOS Connect are not applied. Choose or create a matching "
            "profile in EOSdash if the profile's values do not fit.",
            device_id,
            ", ".join(dropped),
        )

    def _apply_run_settings(self, eos_request, now=None):
        """
        Put the slot width, the horizon and the battery price on the server.

        All three are written every run: the horizon moves with the clock, the
        battery price moves with the PV and grid mix, and the slot width can
        change under a restart.

        The battery price is the one value of the old request body that is not
        an observation and has no home in ``RuntimeForecasts``. It used to ride
        along as ``ems.preis_euro_pro_wh_akku`` and decided what charge left at
        the end of the horizon is worth. Dropping it would hand that judgement
        to the server's own AUTO proxy and quietly overrule what the battery
        interface worked out, so it is mapped onto the setting EOS documents as
        "the historical behaviour": FIXED mode with an explicit price.

        Returns:
            int: The horizon in hours that was applied.
        """
        horizon_hours = self._available_horizon_hours(now)
        self._set_config("optimization/genetic/interval_sec", self.time_frame_base)
        self._set_config("optimization/genetic/horizon_hours", horizon_hours)

        battery_price = (eos_request.get("ems") or {}).get("preis_euro_pro_wh_akku")
        if battery_price is not None:
            self._set_config("optimization/genetic/terminal_value_mode", "FIXED")
            self._set_config(
                # The request prices a Wh; this setting prices a kWh.
                "optimization/genetic/terminal_value_euro_per_kwh",
                float(battery_price) * 1000,
            )
        return horizon_hours

    # ------------------------------------------------------------------
    # Request and response translation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_body(eos_request):
        """
        The observation-only body ``/v1/optimize`` expects.

        The forecast keys keep their original names: ``RuntimeForecasts``
        accepts them as aliases, so the series EOS Connect already assembles go
        across untouched.
        """
        ems = eos_request.get("ems") or {}
        soc = {}
        for request_key in ("pv_akku", "eauto"):
            device = eos_request.get(request_key)
            if isinstance(device, dict) and device.get("device_id") is not None:
                value = device.get("initial_soc_percentage")
                if value is not None:
                    soc[device["device_id"]] = int(value)

        forecasts = {
            name: ems[name]
            for name in (
                "pv_prognose_wh",
                "gesamtlast",
                "strompreis_euro_pro_wh",
                "einspeiseverguetung_euro_pro_wh",
            )
            if ems.get(name)
        }
        if eos_request.get("temperature_forecast"):
            forecasts["temperature_forecast"] = eos_request["temperature_forecast"]

        body = {"soc": soc, "forecasts": forecasts}
        # A warm start is an optimization, not a requirement: a genome from a
        # different slot layout is rejected by EOS rather than silently misread,
        # so it is only worth sending when it matches the grid in use.
        start_solution = eos_request.get("start_solution")
        if start_solution:
            body["start_solution"] = list(start_solution)
        return body

    def _to_midnight_anchored(self, values, first_slot, total_slots):
        """
        Re-anchor a control array that starts at *now* onto the midnight grid.

        This is the one real trap of the new endpoint. Its answer carries
        ``controls_start_at_now: true`` - the arrays begin at the current slot -
        while every consumer in EOS Connect indexes by absolute slots since
        local midnight. Handing the array over unshifted would apply tonight's
        decisions to this morning.

        The slots before *now* are filled with zeros: no plan was made for the
        past, and claiming one would show up as invented history on the chart.
        A short tail can remain when the horizon floors to whole hours; it holds
        the last planned value, which is what ``_adjust_arrays_for_dst`` does
        for the same situation on the old path.
        """
        if not isinstance(values, list) or not values:
            return values
        anchored = [0.0] * first_slot + list(values)
        if len(anchored) >= total_slots:
            return anchored[:total_slots]
        return anchored + [anchored[-1]] * (total_slots - len(anchored))

    def _normalize(self, solution, first_slot, total_slots):
        """
        Turn a GENETIC solution into the shape the rest of EOS Connect reads.

        The endpoint already answers with the familiar keys, so this shifts the
        control arrays onto the midnight grid and leaves the rest alone.

        ``result`` is deliberately not touched. Two anchors live side by side in
        this response and both are load-bearing: the control arrays are read by
        absolute slot since midnight, while ``result`` runs from now and is
        shorter - ``chart.js`` takes its axis length from
        ``result.Last_Wh_pro_Stunde`` and aligns everything else to it. The old
        endpoint answered the same way (a captured 192-slot run carries 155
        result values), so re-anchoring ``result`` here would break the chart
        rather than fix it.

        One small difference remains at 900 s: ``horizon_hours`` is whole hours,
        so ``result`` can end up to three quarter-slots shorter than the old
        convention's ``total - first``. The axis simply ends 45 minutes earlier
        at the far edge of a two-day view, consistently for every series on it.

        ``ac_charge`` is rescaled to the slot. EOS answers with a fraction of the
        charge *power* - 1.0 is ``max_charge_power_w`` for the whole slot - while
        EOS Connect reads ``ac_charge × max_charge_power_w`` as the slot's
        *energy* in Wh, the way the EVopt backends answer. Both readings agree on
        hourly slots and nowhere else: at 900 s every planned AC charge was read
        as four times its energy. The chart showed four times the charge, and
        the control loop asked for four times the power, which only the battery's
        limit stopped - so a planned half-power charge ran at full power (#371).
        """
        normalized = dict(solution)
        if isinstance(normalized.get("ac_charge"), list):
            slot_hours = self.time_frame_base / 3600.0
            normalized["ac_charge"] = [
                float(value) * slot_hours for value in normalized["ac_charge"]
            ]
        for key in ("ac_charge", "dc_charge", "discharge_allowed"):
            if key in normalized:
                normalized[key] = self._to_midnight_anchored(
                    normalized[key], first_slot, total_slots
                )
        # washingstart is read as a slot index into the midnight-anchored arrays
        # (see appliance_start_datetime in eos_connect), so it shifts with them.
        start = normalized.get("washingstart")
        if isinstance(start, int) and not isinstance(start, bool):
            normalized["washingstart"] = start + first_slot
        return normalized

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def optimize(self, eos_request, timeout=180):
        """
        Run one optimization through ``/v1/optimize``.

        Args:
            eos_request (dict): The request EOS Connect builds for either path.
            timeout (int): Seconds to wait for the optimization.

        Returns:
            tuple: ``(response, elapsed_seconds)`` with the response in the same
            shape the old endpoint produced, or ``({"error": ...}, None)``.
        """
        now = datetime.now(self.time_zone)
        first_slot = self._current_slot(now)
        total_slots = horizon_slots(self.time_frame_base)

        self._mirror_devices(eos_request)
        horizon_hours = self._apply_run_settings(eos_request, now)
        body = self._build_body(eos_request)

        request_url = self.base_url + GENETIC_OPTIMIZE_PATH
        logger.info(
            "[OPT-EOS] OPTIMIZE (GENETIC) %s - %d s slots, %d h horizon from slot %d",
            request_url,
            self.time_frame_base,
            horizon_hours,
            first_slot,
        )
        try:
            started = time.time()
            response = requests.post(
                request_url,
                headers={"accept": "application/json", "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
            elapsed = time.time() - started
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error(
                "[OPT-EOS] OPTIMIZE Request timed out after %s seconds | "
                "Config: #eos | ACTION REQUIRED",
                timeout,
            )
            return {"error": "Request timed out - trying again with next run"}, None
        except requests.exceptions.RequestException as err:
            logger.error("[OPT-EOS] OPTIMIZE (GENETIC) request failed: %s", err)
            if getattr(err, "response", None) is not None:
                logger.debug(
                    "[OPT-EOS] OPTIMIZE ERROR - response of EOS is:\n%s",
                    err.response.text,
                )
            logger.debug(
                "[OPT-EOS] OPTIMIZE ERROR - payload for the request was:\n%s",
                json.dumps(body)[:2000],
            )
            # The exception text is withheld deliberately: it carries the
            # resolved host, port and any proxy, and this dict is served
            # verbatim by GET /json/optimize_response.json.
            return {
                "error": f"Request to the EOS server at {self.base_url} failed "
                "- see the log for details, will try again with next cycle"
            }, None

        minutes, seconds = divmod(elapsed, 60)
        logger.info(
            "[OPT-EOS] OPTIMIZE response retrieved successfully "
            "in %d min %.2f sec for current run",
            int(minutes),
            seconds,
        )
        return self._normalize(response.json(), first_slot, total_slots), elapsed
