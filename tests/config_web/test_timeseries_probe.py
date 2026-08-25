"""
Tests for the timeseries pre-flight probe.

The probe is what makes a deliberately strict format workable: it has to say why a
source was rejected, and — for a source it accepts — show the converted numbers so a
unit mistake is visible before saving rather than hours later in the schedule.
"""

from unittest.mock import patch

import pytest
import requests

from src.config_web.timeseries_probe import probe

EVCC_RATES = {
    "rates": [
        {"start": "2026-08-23T00:00:00+02:00", "end": "2026-08-23T00:15:00+02:00", "value": 0.3811},
        {"start": "2026-08-23T00:15:00+02:00", "end": "2026-08-23T00:30:00+02:00", "value": 0.3727},
        {"start": "2026-08-23T00:30:00+02:00", "end": "2026-08-23T00:45:00+02:00", "value": 0.3668},
    ]
}


class FakeResponse:
    def __init__(self, payload=None, status_code=200, raise_json=False):
        self._payload = payload
        self.status_code = status_code
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def run_probe(payload=None, status_code=200, raise_json=False, **kwargs):
    params = {
        "domain": "price",
        "data_url": "http://evcc.local/api/tariff/grid",
        "data_path": "rates",
        "data_token": "",
        "value_unit": "EUR/kWh",
        "time_zone": "Europe/Berlin",
    }
    params.update(kwargs)
    with patch(
        "src.config_web.timeseries_probe.requests.get",
        return_value=FakeResponse(payload, status_code, raise_json),
    ):
        return probe(**params)


class TestSuccessfulProbe:
    def test_reports_shape_and_converted_values(self):
        result = run_probe(EVCC_RATES)

        assert result["ok"] is True
        assert result["entry_count"] == 3
        assert result["resolution_seconds"] == 900
        assert result["value_unit"] == "EUR/kWh"

    def test_slots_are_rendered_in_the_unit_the_ui_shows(self):
        result = run_probe(EVCC_RATES)

        first = result["slots"][0]
        assert first["unit"] == "ct/kWh"
        assert first["value"] == pytest.approx(38.11, abs=0.01)

    def test_slot_timestamps_are_resolved_so_an_offset_problem_is_visible(self):
        result = run_probe(EVCC_RATES)

        assert result["slots"][0]["start"].startswith("2026-08-23T00:00:00")
        assert result["slots"][0]["end"] is not None

    def test_pv_values_reported_in_watt_hours(self):
        payload = {
            "attributes": {
                "data": [
                    {"start": "2026-08-23T00:00:00+02:00", "value": 4000.0},
                    {"start": "2026-08-23T00:15:00+02:00", "value": 4000.0},
                ]
            }
        }

        result = run_probe(
            payload,
            domain="pv",
            data_path="attributes.data",
            value_unit="W",
        )

        assert result["ok"] is True
        assert result["slots"][0]["unit"] == "Wh"
        assert result["slots"][0]["value"] == pytest.approx(1000.0)


class TestUnitMistakeIsSurfaced:
    """Tobias' case: the format is right, the unit is not."""

    def test_evcc_payload_read_as_eur_per_wh_warns_without_failing(self):
        result = run_probe(EVCC_RATES, value_unit="EUR/Wh")

        assert result["ok"] is True
        assert any("implausible price level" in w for w in result["warnings"])

    def test_the_warning_shows_the_absurd_number(self):
        result = run_probe(EVCC_RATES, value_unit="EUR/Wh")

        assert result["slots"][0]["value"] == pytest.approx(38110.0, abs=1.0)

    def test_correct_unit_produces_no_warning(self):
        result = run_probe(EVCC_RATES)

        assert result["warnings"] == []

    def test_unknown_unit_is_rejected_against_the_unit_field(self):
        result = run_probe(EVCC_RATES, value_unit="USD/kWh")

        assert result["ok"] is False
        assert result["field"] == "price.value_unit"


class TestFormatMismatch:
    def test_foreign_field_names_named_against_the_path_field(self):
        payload = {
            "data": [
                {"start_time": "2026-08-23T00:00:00+02:00", "price_per_kwh": 0.3811}
            ]
        }

        result = run_probe(payload, data_path="data")

        assert result["ok"] is False
        assert result["field"] == "price.data_path"
        assert "'start_time'" in result["error"]

    def test_wrong_path_lists_the_top_level_keys(self):
        result = run_probe(EVCC_RATES, data_path="prices")

        assert result["ok"] is False
        assert result["field"] == "price.data_path"
        assert "'rates'" in result["error"]

    def test_unsupported_resolution_is_named(self):
        payload = {
            "rates": [
                {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
                {"start": "2026-08-23T00:05:00+02:00", "value": 0.31},
            ]
        }

        result = run_probe(payload)

        assert result["ok"] is False
        assert "300s" in result["error"]

    def test_single_entry_cannot_determine_resolution(self):
        payload = {"rates": [{"start": "2026-08-23T00:00:00+02:00", "value": 0.30}]}

        result = run_probe(payload)

        assert result["ok"] is False
        assert "at least two" in result["error"]

    def test_naive_timestamps_are_warned_about(self):
        payload = {
            "rates": [
                {"start": "2026-08-23 00:00:00", "value": 0.30},
                {"start": "2026-08-23 00:15:00", "value": 0.31},
            ]
        }

        result = run_probe(payload)

        assert result["ok"] is True
        assert any("no UTC offset" in w for w in result["warnings"])


class TestTransportFailures:
    """Curated messages only — no exception text reaches the client."""

    def test_404_names_the_resource(self):
        result = run_probe({}, status_code=404, resource_label="sensor.grid_prices")

        assert result["ok"] is False
        assert "sensor.grid_prices" in result["error"]

    def test_401_points_at_the_token(self):
        result = run_probe({}, status_code=401)

        assert result["ok"] is False
        assert "token" in result["error"].lower()

    def test_500_reports_the_status(self):
        result = run_probe({}, status_code=500)

        assert result["ok"] is False
        assert "500" in result["error"]

    def test_raised_http_error_carrying_a_response_is_mapped(self):
        exc = requests.exceptions.HTTPError()
        exc.response = type("R", (object,), {"status_code": 404})()

        with patch(
            "src.config_web.timeseries_probe.requests.get", side_effect=exc
        ):
            result = probe(
                "price",
                "http://ha.local/api/states/sensor.x",
                "attributes.data",
                "",
                "EUR/kWh",
                "UTC",
                resource_label="sensor.x",
            )

        assert result["ok"] is False
        assert "sensor.x" in result["error"]

    def test_timeout_is_reported_plainly(self):
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            side_effect=requests.exceptions.Timeout(),
        ):
            result = probe(
                "price", "http://x.local", "rates", "", "EUR/kWh", "UTC"
            )

        assert result["ok"] is False
        assert "No response within" in result["error"]

    def test_connection_error_does_not_leak_exception_text(self):
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            side_effect=requests.exceptions.ConnectionError("secret-internal-host:5432"),
        ):
            result = probe(
                "price", "http://x.local", "rates", "", "EUR/kWh", "UTC"
            )

        assert result["ok"] is False
        assert "secret-internal-host" not in result["error"]

    def test_non_json_response_is_reported(self):
        result = run_probe(None, raise_json=True)

        assert result["ok"] is False
        assert "JSON" in result["error"]

    def test_missing_url_is_reported_without_a_request(self):
        result = probe("price", "", "rates", "", "EUR/kWh", "UTC")

        assert result["ok"] is False
        assert result["field"] == "price.data_url"

    def test_unknown_domain_rejected(self):
        result = probe("weather", "http://x", "d", "", "EUR/kWh", "UTC")

        assert result["ok"] is False


class TestResolutionCompatibility:
    """A green test must not be followed by a runtime "resolution mismatch"."""

    def test_hourly_source_on_15min_system_is_rejected(self):
        payload = {
            "rates": [
                {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
                {"start": "2026-08-23T01:00:00+02:00", "value": 0.31},
            ]
        }

        result = run_probe(payload, time_frame_base=900)

        assert result["ok"] is False
        assert "15-minute slots" in result["error"]

    def test_quarter_hourly_source_on_15min_system_is_fine(self):
        result = run_probe(EVCC_RATES, time_frame_base=900)

        assert result["ok"] is True

    def test_hourly_source_on_hourly_system_is_fine(self):
        payload = {
            "rates": [
                {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
                {"start": "2026-08-23T01:00:00+02:00", "value": 0.31},
            ]
        }

        result = run_probe(payload, time_frame_base=3600)

        assert result["ok"] is True


class TestPvPlausibility:
    """The PV unit mistake is only a factor of four — it needs a reference to be seen."""

    PV_PAYLOAD = {
        "attributes": {
            "data": [
                {"start": "2026-08-23T12:00:00+02:00", "value": 4000.0},
                {"start": "2026-08-23T12:15:00+02:00", "value": 4000.0},
            ]
        }
    }

    def _pv_probe(self, **kwargs):
        return run_probe(
            self.PV_PAYLOAD,
            domain="pv",
            data_path="attributes.data",
            value_unit="W",
            **kwargs,
        )

    def test_matching_installation_produces_no_warning(self):
        result = self._pv_probe(installed_power_w=4000)

        assert result["ok"] is True
        assert result["warnings"] == []

    def test_energy_unit_on_a_power_source_is_flagged(self):
        result = run_probe(
            self.PV_PAYLOAD,
            domain="pv",
            data_path="attributes.data",
            value_unit="Wh",
            installed_power_w=4000,
        )

        assert result["ok"] is True
        assert any("implausible PV level" in w for w in result["warnings"])

    def test_without_a_configured_installation_no_guessing(self):
        result = run_probe(
            self.PV_PAYLOAD,
            domain="pv",
            data_path="attributes.data",
            value_unit="Wh",
            installed_power_w=0,
        )

        assert result["warnings"] == []
