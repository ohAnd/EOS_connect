"""
The GENETIC path: quarter-hourly planning against a current EOS server.

EOS plans in 15-minute slots only behind ``/v1/optimize``; the endpoint EOS
Connect has always used is hardwired to an algorithm fixed at 3600 s (#353).
The new path exists only on recent EOS builds, so it is chosen by asking the
server what it exposes - never by configuration - and everything it returns is
translated back into the shape the rest of EOS Connect already reads.

Kept out of ``test_optimization_backend_eos.py``: that module is already at
pylint's module-size limit.
"""

from datetime import datetime
from unittest.mock import Mock, patch

import pytest
import pytz
import requests

from src.interfaces.optimization_backends.optimization_backend_eos_genetic import (
    EOSGeneticPath,
    supports_device_profiles,
    supports_genetic_optimize,
)

MODULE = (
    "src.interfaces.optimization_backends.optimization_backend_eos_genetic"
)
BERLIN = pytz.timezone("Europe/Berlin")


def _path(time_frame_base=900, writes=None, device_profiles=False):
    """A path object whose config writes are recorded instead of sent."""
    recorded = writes if writes is not None else []

    def set_config(key, value):
        recorded.append((key, value))
        return True

    return EOSGeneticPath(
        "http://eos:8503",
        time_frame_base,
        BERLIN,
        lambda key: None,
        set_config,
        device_profiles=device_profiles,
    )


# ----------------------------------------------------------------------
# Capability detection
# ----------------------------------------------------------------------


class TestCapabilityDetection:
    """Which server can plan quarter-hourly is asked, not assumed."""

    @staticmethod
    def _openapi(paths):
        response = Mock()
        response.json.return_value = {"paths": {p: {} for p in paths}}
        response.raise_for_status = Mock()
        return response

    def test_a_server_exposing_the_endpoint_can(self):
        with patch(
            MODULE + ".requests.get",
            return_value=self._openapi(["/optimize", "/v1/optimize"]),
        ):
            assert supports_genetic_optimize("http://eos:8503") is True

    def test_a_server_without_it_cannot(self):
        """0.2.0 and 0.3.0 carry only the deprecated endpoint."""
        with patch(
            MODULE + ".requests.get", return_value=self._openapi(["/optimize"])
        ):
            assert supports_genetic_optimize("http://eos:8503") is False

    def test_an_unreachable_server_falls_back_to_the_old_path(self):
        """
        Silence must mean the deprecated endpoint, not the current one.

        The answer also decides the width of every series EOS Connect builds
        for the whole run, so guessing yes for a server that turns out not to
        accept it would fail every cycle instead of one request.
        """
        with patch(
            MODULE + ".requests.get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            assert supports_genetic_optimize("http://eos:8503") is False

    def test_a_server_answering_nonsense_falls_back_too(self):
        response = Mock()
        response.json.side_effect = ValueError("not json")
        response.raise_for_status = Mock()
        with patch(MODULE + ".requests.get", return_value=response):
            assert supports_genetic_optimize("http://eos:8503") is False


# ----------------------------------------------------------------------
# The rolling horizon
# ----------------------------------------------------------------------


class TestRollingHorizon:
    """
    How far ahead the supplied series let EOS plan.

    EOS wants ``slot_of_now + horizon`` values counted from local midnight and
    EOS Connect supplies a fixed 48 hours from there, so the horizon rolls down
    with the clock - and never below 24 h, because the window always reaches
    the day after tomorrow.
    """

    @pytest.mark.parametrize(
        "hour, minute, expected",
        [(0, 0, 48), (6, 0, 42), (12, 0, 36), (18, 0, 30), (23, 0, 25), (23, 59, 24)],
    )
    def test_it_rolls_with_the_clock(self, hour, minute, expected):
        now = BERLIN.localize(datetime(2026, 10, 31, hour, minute))
        assert _path()._available_horizon_hours(now) == expected

    def test_it_never_drops_below_a_day(self):
        """The worst case is still what EOS would default to on its own."""
        worst = min(
            _path()._available_horizon_hours(
                BERLIN.localize(datetime(2026, 10, 31, hour, minute))
            )
            for hour in range(24)
            for minute in (0, 15, 30, 45)
        )
        assert worst == 24

    def test_hourly_slots_agree_with_quarter_hourly(self):
        now = BERLIN.localize(datetime(2026, 10, 31, 12, 0))
        assert _path(3600)._available_horizon_hours(now) == 36

    def test_interval_and_horizon_are_both_published(self):
        writes = []
        now = BERLIN.localize(datetime(2026, 10, 31, 12, 0))
        _path(900, writes)._apply_run_settings({"ems": {}}, now)
        assert ("optimization/genetic/interval_sec", 900) in writes
        assert ("optimization/genetic/horizon_hours", 36) in writes


# ----------------------------------------------------------------------
# Mirroring the hardware into the server's configuration
# ----------------------------------------------------------------------


class TestDeviceMirroring:
    """``/v1/optimize`` reads hardware from config, so it has to get there."""

    REQUEST = {
        "pv_akku": {
            "device_id": "battery1",
            "capacity_wh": 22118,
            "initial_soc_percentage": 42,
        },
        "inverter": {
            "device_id": "inverter1",
            "max_power_wh": 10000,
            "battery_id": "battery1",
        },
        "eauto": {"device_id": "ev1", "capacity_wh": 27000},
    }

    def test_each_group_lands_under_its_device_id(self):
        writes = []
        _path(900, writes)._mirror_devices(self.REQUEST)
        written = dict(writes)
        assert "battery1" in written["devices/batteries"]
        assert "inverter1" in written["devices/inverters"]
        assert "ev1" in written["devices/electric_vehicles"]

    def test_the_inverter_power_key_is_renamed(self):
        """The request spells it max_power_wh; the device settings max_power_w."""
        writes = []
        _path(900, writes)._mirror_devices(self.REQUEST)
        inverter = dict(writes)["devices/inverters"]["inverter1"]
        assert inverter["max_power_w"] == 10000
        assert "max_power_wh" not in inverter

    def test_the_state_of_charge_stays_out_of_the_hardware(self):
        """
        It changes every cycle and travels in the request's soc map. Writing it
        as configuration would make a per-run observation look like hardware.
        """
        writes = []
        _path(900, writes)._mirror_devices(self.REQUEST)
        battery = dict(writes)["devices/batteries"]["battery1"]
        assert "initial_soc_percentage" not in battery
        assert battery["capacity_wh"] == 22118

    def test_unchanged_hardware_is_not_written_again(self):
        """Hardware does not move between cycles; the config write should not either."""
        writes = []
        path = _path(900, writes)
        path._mirror_devices(self.REQUEST)
        first_round = len(writes)
        path._mirror_devices(self.REQUEST)
        assert len(writes) == first_round

    def test_changed_hardware_is_written_again(self):
        writes = []
        path = _path(900, writes)
        path._mirror_devices(self.REQUEST)
        before = len(writes)
        grown = {
            **self.REQUEST,
            "pv_akku": {**self.REQUEST["pv_akku"], "capacity_wh": 30000},
        }
        path._mirror_devices(grown)
        assert len(writes) > before

    def test_a_device_without_an_id_is_skipped(self):
        """Nothing to key the mapping by - and EOS rejects an unkeyed entry."""
        writes = []
        _path(900, writes)._mirror_devices({"pv_akku": {"capacity_wh": 1}})
        assert not writes


# ----------------------------------------------------------------------
# Servers that describe devices by profile
# ----------------------------------------------------------------------


class TestDeviceProfileDetection:
    """
    EOS builds from 2026-11-11 on describe batteries and inverters by profile.
    They answer HTTP 200 to the flat keys and drop them silently, so the shape
    is chosen by asking the schema - never by version.
    """

    @staticmethod
    def _openapi(schemas):
        response = Mock()
        response.json.return_value = {
            "paths": {},
            "components": {"schemas": {name: {} for name in schemas}},
        }
        response.raise_for_status = Mock()
        return response

    def test_a_server_with_profiles_is_recognised(self):
        with patch(
            MODULE + ".requests.get",
            return_value=self._openapi(
                ["BatteryProfileAdjustment", "BatteriesCommonSettings-Input"]
            ),
        ):
            assert supports_device_profiles("http://eos:8503") is True

    def test_a_server_without_profiles_is_not(self):
        with patch(
            MODULE + ".requests.get",
            return_value=self._openapi(["BatteriesCommonSettings-Input"]),
        ):
            assert supports_device_profiles("http://eos:8503") is False

    def test_an_unreachable_server_keeps_the_flat_shape(self):
        with patch(
            MODULE + ".requests.get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            assert supports_device_profiles("http://eos:8503") is False

    def test_a_server_answering_nonsense_keeps_the_flat_shape(self):
        response = Mock()
        response.json.return_value = ["not", "an", "object"]
        response.raise_for_status = Mock()
        with patch(MODULE + ".requests.get", return_value=response):
            assert supports_device_profiles("http://eos:8503") is False


class TestProfileShapedMirroring:
    """
    On a server with profiles, capacity and power go into ``profile_adjustment``
    - the mapping EOS's own config-file migration applies.
    """

    REQUEST = {
        "pv_akku": {
            "device_id": "battery1",
            "capacity_wh": 12000,
            "charging_efficiency": 0.93,
            "discharging_efficiency": 0.94,
            "max_charge_power_w": 4000,
            "initial_soc_percentage": 42,
            "min_soc_percentage": 10,
            "max_soc_percentage": 95,
        },
        "inverter": {
            "device_id": "inverter1",
            "max_power_wh": 10000,
            "battery_id": "battery1",
        },
        "eauto": {
            "device_id": "ev1",
            "capacity_wh": 27000,
            "charging_efficiency": 0.90,
            "discharging_efficiency": 0.95,
            "max_charge_power_w": 7360,
        },
    }

    def _written(self, device_profiles=True):
        writes = []
        _path(900, writes, device_profiles)._mirror_devices(self.REQUEST)
        return dict(writes)

    def test_the_battery_capacity_and_power_become_an_adjustment(self):
        battery = self._written()["devices/batteries"]["battery1"]
        assert battery["profile_adjustment"] == {
            "nominal_capacity_wh": 12000,
            "max_charge_power_w": 4000,
            "max_discharge_power_w": 4000,
        }
        for flat in ("capacity_wh", "max_charge_power_w"):
            assert flat not in battery

    def test_the_charge_limit_also_caps_discharging(self):
        """
        Without profiles one number capped both directions. Leaving the discharge
        limit out would hand it to the profile default (5000 W for generic-lfp).
        """
        battery = self._written()["devices/batteries"]["battery1"]
        adjustment = battery["profile_adjustment"]
        assert adjustment["max_discharge_power_w"] == adjustment["max_charge_power_w"]

    def test_efficiencies_are_left_to_the_profile(self):
        """The server has no setting left for them; sending them changes nothing."""
        written = self._written()
        for group, device_id in (
            ("devices/batteries", "battery1"),
            ("devices/electric_vehicles", "ev1"),
        ):
            device = written[group][device_id]
            assert "charging_efficiency" not in device
            assert "discharging_efficiency" not in device

    def test_policy_settings_stay_where_they_are(self):
        battery = self._written()["devices/batteries"]["battery1"]
        assert battery["min_soc_percentage"] == 10
        assert battery["max_soc_percentage"] == 95
        assert "initial_soc_percentage" not in battery

    def test_the_inverter_power_becomes_its_nominal_power(self):
        inverter = self._written()["devices/inverters"]["inverter1"]
        assert inverter["profile_adjustment"] == {"nominal_power_w": 10000}
        assert inverter["battery_id"] == "battery1"
        assert "max_power_w" not in inverter
        assert "max_power_wh" not in inverter

    def test_the_ev_is_converted_like_the_battery(self):
        ev = self._written()["devices/electric_vehicles"]["ev1"]
        assert ev["profile_adjustment"]["nominal_capacity_wh"] == 27000
        assert ev["profile_adjustment"]["max_charge_power_w"] == 7360

    def test_a_server_without_profiles_keeps_the_flat_shape(self):
        battery = self._written(device_profiles=False)["devices/batteries"]["battery1"]
        assert battery["capacity_wh"] == 12000
        assert battery["charging_efficiency"] == 0.93
        assert "profile_adjustment" not in battery

    def test_the_dropped_efficiencies_are_reported_once(self, caplog):
        path = _path(900, [], device_profiles=True)
        with caplog.at_level("WARNING", logger="__main__"):
            path._mirror_devices(self.REQUEST)
            changed = {
                **self.REQUEST,
                "pv_akku": {**self.REQUEST["pv_akku"], "capacity_wh": 13000},
            }
            path._mirror_devices(changed)
        battery_warnings = [
            record
            for record in caplog.records
            if "efficiencies of 'battery1'" in record.getMessage()
        ]
        assert len(battery_warnings) == 1


class TestProfileDevicesKeepWhatEOSdashSet:
    """
    #385: replacing the whole device on every start reset what was set on the
    server - the profile chosen in EOSdash, a full-charge interval, and with
    the old keys even the capacity. An existing device only gets the fields
    EOS Connect owns, and only those that changed.
    """

    REQUEST = TestProfileShapedMirroring.REQUEST

    #: What GET devices/batteries returns for a battery set up in EOSdash.
    ON_SERVER = {
        "battery1": {
            "device_id": "battery1",
            "battery_profile": "my-byd-hvs",
            "profile_adjustment": {
                "nominal_capacity_wh": 12000,
                "max_charge_power_w": 3000.0,
                "max_discharge_power_w": 3000.0,
                "full_charge_interval_days": 21.0,
                "full_charge_detect_percentage": None,
            },
            "levelized_cost_of_storage_amt_kwh": 0.08,
            "min_soc_percentage": 10,
            "max_soc_percentage": 95,
            "effective_nominal_capacity_wh": 12000,
        },
        "battery2": {"device_id": "battery2", "battery_profile": "generic-lfp"},
    }

    @staticmethod
    def _path(on_server, writes, failing_read=False):
        def get_config(key):
            if failing_read:
                raise requests.exceptions.ConnectionError("refused")
            return on_server.get(key)

        def set_config(key, value):
            writes.append((key, value))
            return True

        return EOSGeneticPath(
            "http://eos:8503",
            900,
            BERLIN,
            get_config,
            set_config,
            device_profiles=True,
        )

    def _battery_writes(self, on_server, request=None):
        writes = []
        path = self._path(on_server, writes)
        path._mirror_devices({"pv_akku": (request or self.REQUEST)["pv_akku"]})
        return writes

    def test_only_the_changed_fields_are_written(self):
        writes = self._battery_writes({"devices/batteries": self.ON_SERVER})
        assert writes == [
            ("devices/batteries/battery1/profile_adjustment/max_charge_power_w", 4000),
            (
                "devices/batteries/battery1/profile_adjustment/max_discharge_power_w",
                4000,
            ),
        ]

    def test_what_eosdash_set_is_never_written(self):
        writes = self._battery_writes({"devices/batteries": self.ON_SERVER})
        written_paths = [key for key, _ in writes]
        assert "devices/batteries" not in written_paths
        for untouched in (
            "battery_profile",
            "full_charge_interval_days",
            "levelized_cost_of_storage_amt_kwh",
        ):
            assert not any(key.endswith(untouched) for key in written_paths)

    def test_a_device_matching_the_server_writes_nothing(self):
        in_sync = {
            "battery1": {
                **self.ON_SERVER["battery1"],
                "profile_adjustment": {
                    **self.ON_SERVER["battery1"]["profile_adjustment"],
                    "max_charge_power_w": 4000.0,
                    "max_discharge_power_w": 4000.0,
                },
            }
        }
        assert not self._battery_writes({"devices/batteries": in_sync})

    def test_a_new_device_joins_the_devices_already_there(self):
        """EOS answers 400 to a device created by its own path."""
        on_server = {"battery2": self.ON_SERVER["battery2"]}
        writes = self._battery_writes({"devices/batteries": on_server})
        assert len(writes) == 1
        key, group = writes[0]
        assert key == "devices/batteries"
        assert group["battery2"] == self.ON_SERVER["battery2"]
        assert group["battery1"]["profile_adjustment"]["nominal_capacity_wh"] == 12000

    def test_an_empty_server_gets_the_device(self):
        writes = self._battery_writes({"devices/batteries": None})
        assert [key for key, _ in writes] == ["devices/batteries"]

    def test_an_unreadable_group_is_tried_again_next_cycle(self):
        writes = []
        path = self._path({}, writes, failing_read=True)
        path._mirror_devices({"pv_akku": self.REQUEST["pv_akku"]})
        assert not writes
        assert "batteries" not in path._mirrored_devices

    def test_a_server_without_profiles_still_gets_the_whole_group(self):
        """Before profiles, the group write is what those servers understand."""
        writes = []
        on_server = {"devices/batteries": self.ON_SERVER}
        path = self._path(on_server, writes)
        path.device_profiles = False
        path._mirror_devices({"pv_akku": self.REQUEST["pv_akku"]})
        assert [key for key, _ in writes] == ["devices/batteries"]


# ----------------------------------------------------------------------
# The request body
# ----------------------------------------------------------------------


class TestRequestBody:
    """Observations only: the forecasts EOS Connect owns, plus live SoC."""

    REQUEST = {
        "ems": {
            "pv_prognose_wh": [1.0, 2.0],
            "gesamtlast": [3.0, 4.0],
            "strompreis_euro_pro_wh": [0.1, 0.2],
            "einspeiseverguetung_euro_pro_wh": [0.01, 0.02],
            "preis_euro_pro_wh_akku": 0.05,
        },
        "pv_akku": {"device_id": "battery1", "initial_soc_percentage": 42},
        "eauto": {"device_id": "ev1", "initial_soc_percentage": 50},
        "temperature_forecast": [7.9, 8.0],
    }

    def test_the_state_of_charge_is_keyed_by_device(self):
        body = EOSGeneticPath._build_body(self.REQUEST)
        assert body["soc"] == {"battery1": 42, "ev1": 50}

    def test_the_series_keep_the_names_eos_accepts(self):
        """
        RuntimeForecasts takes the original spellings as aliases, so the arrays
        EOS Connect already assembles go across untouched.
        """
        body = EOSGeneticPath._build_body(self.REQUEST)
        assert body["forecasts"]["pv_prognose_wh"] == [1.0, 2.0]
        assert body["forecasts"]["gesamtlast"] == [3.0, 4.0]
        assert body["forecasts"]["temperature_forecast"] == [7.9, 8.0]

    def test_the_battery_price_is_not_a_forecast(self):
        """preis_euro_pro_wh_akku is a scalar setting, not a series."""
        body = EOSGeneticPath._build_body(self.REQUEST)
        assert "preis_euro_pro_wh_akku" not in body["forecasts"]

    def test_no_warm_start_is_sent_when_there_is_none(self):
        body = EOSGeneticPath._build_body({**self.REQUEST, "start_solution": None})
        assert "start_solution" not in body

    def test_a_warm_start_is_passed_on(self):
        body = EOSGeneticPath._build_body({**self.REQUEST, "start_solution": [1.0, 0.0]})
        assert body["start_solution"] == [1.0, 0.0]


# ----------------------------------------------------------------------
# Re-anchoring the answer
# ----------------------------------------------------------------------


class TestMidnightAnchoring:
    """
    The trap of the new endpoint.

    Its answer sets ``controls_start_at_now``: the control arrays begin at the
    current slot. Every consumer in EOS Connect indexes by absolute slots since
    local midnight, so an unshifted array would apply tonight's decisions to
    this morning.
    """

    def test_the_plan_moves_to_where_it_belongs(self):
        anchored = _path()._to_midnight_anchored([7, 8, 9], first_slot=2, total_slots=6)
        assert anchored == [0.0, 0.0, 7, 8, 9, 9]

    def test_the_past_is_empty_rather_than_invented(self):
        """No plan was made for this morning; claiming one would show on the chart."""
        anchored = _path()._to_midnight_anchored([1] * 4, first_slot=3, total_slots=7)
        assert anchored[:3] == [0.0, 0.0, 0.0]

    def test_a_short_tail_holds_the_last_decision(self):
        """
        horizon_hours floors to whole hours, so up to three quarter-hourly slots
        can be left at the end. Holding beats dropping to zero, and matches what
        the hourly path does when it pads for DST.
        """
        anchored = _path()._to_midnight_anchored([5, 6], first_slot=0, total_slots=5)
        assert anchored == [5, 6, 6, 6, 6]

    def test_an_overlong_plan_is_cut_to_the_grid(self):
        anchored = _path()._to_midnight_anchored([1, 2, 3, 4], first_slot=2, total_slots=4)
        assert anchored == [0.0, 0.0, 1, 2]

    def test_every_control_array_is_shifted(self):
        solution = {
            "ac_charge": [1, 1],
            "dc_charge": [2, 2],
            "discharge_allowed": [1, 0],
            "result": {"untouched": True},
        }
        normalized = _path()._normalize(solution, first_slot=1, total_slots=3)
        for key in ("ac_charge", "dc_charge", "discharge_allowed"):
            assert len(normalized[key]) == 3, key
            assert normalized[key][0] == 0.0, key
        assert normalized["result"] == {"untouched": True}

    def test_the_appliance_start_slot_moves_with_them(self):
        """washingstart is read as an index into the same arrays."""
        normalized = _path()._normalize(
            {"washingstart": 3}, first_slot=10, total_slots=192
        )
        assert normalized["washingstart"] == 13

    def test_no_appliance_start_stays_absent(self):
        normalized = _path()._normalize(
            {"washingstart": None}, first_slot=10, total_slots=192
        )
        assert normalized["washingstart"] is None


# ----------------------------------------------------------------------
# Which path the backend takes
# ----------------------------------------------------------------------


class TestPathSelection:
    """
    The choice is the server's capability and the configured slot width - never
    a setting of its own. From the outside nothing about EOS Connect changes.
    """

    EOS_MODULE = "src.interfaces.optimization_backends.optimization_backend_eos"

    @staticmethod
    def _backend(time_frame_base, supported, profiles=False):
        """Build an EOSBackend against a mocked EOS, with capability forced."""
        from src.interfaces.optimization_backends.optimization_backend_eos import (
            EOSBackend,
        )

        health = Mock()
        health.json.return_value = {"status": "alive", "version": "0.3.0.dev2"}
        health.raise_for_status = Mock()
        optimization = Mock()
        optimization.json.return_value = {
            "algorithm": "GENETIC",
            "genetic": {"horizon_hours": 48},
            "genetic0": {"horizon_hours": 48},
        }
        optimization.raise_for_status = Mock()
        devices = Mock()
        devices.json.return_value = None
        devices.raise_for_status = Mock()

        def get_side_effect(url, **kwargs):  # pylint: disable=unused-argument
            if "/v1/health" in url:
                return health
            if "/v1/config/optimization" in url:
                return optimization
            return devices

        module = TestPathSelection.EOS_MODULE
        with patch(module + ".requests.get", side_effect=get_side_effect), patch(
            module + ".requests.put"
        ), patch(
            module + ".supports_genetic_optimize", return_value=supported
        ) as probe, patch(
            module + ".supports_device_profiles", return_value=profiles
        ):
            backend = EOSBackend(
                "http://eos:8503", time_frame_base, BERLIN
            )
        return backend, probe

    def test_an_hourly_run_also_leaves_the_deprecated_endpoint(self):
        """
        EOS marks /optimize deprecated, so a server offering the current one
        gets it for hourly planning too - not only where 15-min slots force it.
        That is what lets the legacy path eventually be retired here as well.
        """
        backend, _ = self._backend(3600, supported=True)
        assert backend._genetic_path is not None

    def test_a_quarter_hourly_run_takes_the_genetic_path(self):
        backend, _ = self._backend(900, supported=True)
        assert backend._genetic_path is not None

    def test_an_hourly_run_on_an_old_server_keeps_the_old_path(self):
        backend, _ = self._backend(3600, supported=False)
        assert backend._genetic_path is None

    def test_a_server_that_cannot_falls_back_to_the_old_path(self):
        """0.2.0 and 0.3.0 have no /v1/optimize; they must still work."""
        backend, _ = self._backend(900, supported=False)
        assert backend._genetic_path is None

    def test_a_server_with_profiles_gets_profile_shaped_devices(self):
        backend, _ = self._backend(900, supported=True, profiles=True)
        assert backend._genetic_path.device_profiles is True

    def test_a_server_without_profiles_gets_flat_devices(self):
        backend, _ = self._backend(900, supported=True, profiles=False)
        assert backend._genetic_path.device_profiles is False

    def test_the_genetic_path_is_the_one_that_answers(self):
        backend, _ = self._backend(900, supported=True)
        backend._genetic_path = Mock()
        backend._genetic_path.optimize.return_value = ({"ac_charge": []}, 1.0)
        backend.optimize({"ems": {}}, timeout=5)
        backend._genetic_path.optimize.assert_called_once()

    def test_the_result_series_keep_their_own_anchor(self):
        """
        Two anchors live in one response, and both are load-bearing.

        The control arrays are read by absolute slot since midnight; ``result``
        runs from now and is shorter, and ``chart.js`` takes its axis length
        from ``result.Last_Wh_pro_Stunde``. The old endpoint answered the same
        way, so shifting ``result`` here would break the chart, not fix it.
        """
        solution = {
            "ac_charge": [1, 1],
            "result": {"Last_Wh_pro_Stunde": [5, 6], "Electricity_price": [0.1, 0.2]},
        }
        normalized = _path()._normalize(solution, first_slot=4, total_slots=8)
        assert normalized["result"]["Last_Wh_pro_Stunde"] == [5, 6]
        assert normalized["result"]["Electricity_price"] == [0.1, 0.2]
        assert len(normalized["ac_charge"]) == 8, "the control array still shifts"


# ----------------------------------------------------------------------
# ac_charge: EOS answers in power, EOS Connect reads energy (#371)
# ----------------------------------------------------------------------


class TestAcChargeIsSlotEnergy:
    """
    EOS answers ``ac_charge`` as a fraction of the charge power: 1.0 is
    ``max_charge_power_w`` for the whole slot. EOS Connect reads
    ``ac_charge × max_charge_power_w`` as the slot's energy in Wh, which is how
    the EVopt backends answer. The two agree on hourly slots only; at 900 s an
    unscaled answer was read as four times its energy - on the chart, and as
    four times the power by the control loop.
    """

    def test_a_quarter_hour_takes_a_quarter_of_the_power_as_energy(self):
        normalized = _path(900)._normalize(
            {"ac_charge": [1.0, 0.5, 0.0]}, first_slot=0, total_slots=3
        )
        assert normalized["ac_charge"] == [0.25, 0.125, 0.0]

    def test_an_hour_is_left_as_it_is(self):
        normalized = _path(3600)._normalize(
            {"ac_charge": [1.0, 0.5]}, first_slot=0, total_slots=2
        )
        assert normalized["ac_charge"] == [1.0, 0.5]

    def test_only_ac_charge_is_rescaled(self):
        normalized = _path(900)._normalize(
            {"dc_charge": [1, 1], "discharge_allowed": [1, 0]},
            first_slot=0,
            total_slots=2,
        )
        assert normalized["dc_charge"] == [1, 1]
        assert normalized["discharge_allowed"] == [1, 0]

    @patch("src.interfaces.base_control.datetime")
    def test_a_planned_half_power_charge_is_driven_at_half_power(self, mock_datetime):
        """The reported case: 3600 W, 15-minute slots, EOS plans half power."""
        from src.interfaces.base_control import (  # pylint: disable=import-outside-toplevel
            BaseControl,
        )

        mock_datetime.now.return_value = BERLIN.localize(datetime(2026, 11, 7, 10, 0, 0))
        config = {
            "battery": {"max_charge_power_w": 3600, "capacity_wh": 10000},
            "inverter": {"max_grid_charge_rate": 3600, "max_pv_charge_rate": 3600},
        }
        normalized = _path(900)._normalize(
            {"ac_charge": [0.5]}, first_slot=0, total_slots=1
        )

        control = BaseControl(config, BERLIN, time_frame_base=900)
        control.set_current_bat_charge_max(3600)
        control.set_current_ac_charge_demand(normalized["ac_charge"][0])

        assert control.get_current_ac_charge_demand() == pytest.approx(450.0)
        assert control.get_needed_ac_charge_power() == pytest.approx(1800.0)


# ----------------------------------------------------------------------
# The running slot: EOS plans only what is left of it
# ----------------------------------------------------------------------


class TestFirstSlotFraction:
    """
    A run inside a slot plans only its remainder. EOS reports the share as
    ``first_slot_fraction``; the first ``ac_charge`` is the power for that
    remainder, so its energy is only the remainder's share of a slot.
    """

    def test_the_running_slot_holds_only_its_remainder(self):
        normalized = _path(3600)._normalize(
            {"ac_charge": [0.5, 0.5], "first_slot_fraction": 0.25},
            first_slot=0,
            total_slots=2,
        )
        assert normalized["ac_charge"] == [0.125, 0.5]

    def test_it_combines_with_the_quarter_hour_scaling(self):
        normalized = _path(900)._normalize(
            {"ac_charge": [1.0, 1.0], "first_slot_fraction": 0.5},
            first_slot=0,
            total_slots=2,
        )
        assert normalized["ac_charge"] == [0.125, 0.25]

    def test_the_fraction_applies_where_now_lands_on_the_grid(self):
        """Index 0 of the answer is the current slot, wherever it is anchored."""
        normalized = _path(3600)._normalize(
            {"ac_charge": [0.8, 0.8], "first_slot_fraction": 0.5},
            first_slot=10,
            total_slots=12,
        )
        assert normalized["ac_charge"][10] == pytest.approx(0.4)
        assert normalized["ac_charge"][11] == pytest.approx(0.8)

    @pytest.mark.parametrize("fraction", [None, 0, -0.5, 1.5, "0.5", True])
    def test_a_missing_or_impossible_fraction_means_a_whole_slot(self, fraction):
        solution = {"ac_charge": [0.5]}
        if fraction is not None:
            solution["first_slot_fraction"] = fraction
        normalized = _path(3600)._normalize(solution, first_slot=0, total_slots=1)
        assert normalized["ac_charge"] == [0.5]

    def test_other_controls_are_not_scaled(self):
        """They are permissions and power shares, not energies."""
        normalized = _path(3600)._normalize(
            {
                "ac_charge": [1.0],
                "dc_charge": [1.0],
                "discharge_allowed": [1],
                "first_slot_fraction": 0.25,
            },
            first_slot=0,
            total_slots=1,
        )
        assert normalized["dc_charge"] == [1.0]
        assert normalized["discharge_allowed"] == [1]

    @patch("src.interfaces.base_control.datetime")
    def test_a_half_power_plan_at_quarter_to_runs_at_half_power(self, mock_datetime):
        """
        Hourly slots, a run at 10:45, EOS plans half of 3600 W for the 15
        minutes left. Counted as a whole slot, the 1800 Wh would have been
        pushed through in those 15 minutes - full power, capped by the battery.
        """
        from src.interfaces.base_control import (  # pylint: disable=import-outside-toplevel
            BaseControl,
        )

        mock_datetime.now.return_value = BERLIN.localize(
            datetime(2026, 11, 15, 10, 45, 0)
        )
        config = {
            "battery": {"max_charge_power_w": 3600, "capacity_wh": 10000},
            "inverter": {"max_grid_charge_rate": 3600, "max_pv_charge_rate": 3600},
        }
        normalized = _path(3600)._normalize(
            {"ac_charge": [0.5], "first_slot_fraction": 0.25},
            first_slot=0,
            total_slots=1,
        )

        control = BaseControl(config, BERLIN, time_frame_base=3600)
        control.set_current_bat_charge_max(3600)
        control.set_current_ac_charge_demand(normalized["ac_charge"][0])

        assert control.get_current_ac_charge_demand() == pytest.approx(450.0)
        assert control.get_needed_ac_charge_power() == pytest.approx(1800.0)
