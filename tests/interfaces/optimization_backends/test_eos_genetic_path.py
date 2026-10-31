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
    supports_genetic_optimize,
)

MODULE = (
    "src.interfaces.optimization_backends.optimization_backend_eos_genetic"
)
BERLIN = pytz.timezone("Europe/Berlin")


def _path(time_frame_base=900, writes=None):
    """A path object whose config writes are recorded instead of sent."""
    recorded = writes if writes is not None else []

    def set_config(key, value):
        recorded.append((key, value))
        return True

    return EOSGeneticPath(
        "http://eos:8503", time_frame_base, BERLIN, lambda key: None, set_config
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
        _path(900, writes)._apply_run_settings(now)
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
    def _backend(time_frame_base, supported):
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
        ) as probe:
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
