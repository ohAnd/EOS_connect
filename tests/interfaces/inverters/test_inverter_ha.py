"""
Unit tests for the InverterHA class.

Tests the generic Home Assistant inverter interface, including service calls,
sequence execution, mode switching, and BaseInverter compliance.

Common interface compliance tests are inherited from BaseInverterTestSuite.
"""

from unittest.mock import patch, Mock
import pytest
import requests as req_lib

from src.interfaces.inverters import InverterHA, BaseInverter
from .base_inverter_tests import BaseInverterTestSuite

# pylint: disable=import-error,redefined-outer-name,too-few-public-methods
# pylint: disable=protected-access,missing-function-docstring
# pylint: disable=unused-argument


# =========================================================================
# BaseInverterTestSuite — Interface Compliance
# =========================================================================


class TestInverterHABase(BaseInverterTestSuite):
    """Inherited interface compliance tests for InverterHA."""

    inverter_class = InverterHA
    minimal_config = {
        "type": "homeassistant",
        "address": "http://homeassistant.local:8123",
        "url": "http://homeassistant.local:8123",
        "token": "test-token",
        "max_grid_charge_rate": 3000,
        "max_pv_charge_rate": 4000,
    }
    expected_extended_monitoring = False


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def default_config():
    """Returns a default configuration dictionary for InverterHA."""
    return {
        "url": "http://homeassistant.local:8123",
        "token": "test-long-lived-access-token",
        "charge_from_grid": [
            {
                "service": "number.set_value",
                "entity_id": "number.inverter_charge_power",
                "data_template": {"value": "{{ power }}"},
            },
            {
                "service": "select.select_option",
                "entity_id": "select.inverter_mode",
                "data": {"option": "Force Charge"},
            },
        ],
        "avoid_discharge": [
            {
                "service": "select.select_option",
                "entity_id": "select.inverter_mode",
                "data": {"option": "Backup"},
            }
        ],
        "discharge_allowed": [
            {
                "service": "select.select_option",
                "entity_id": "select.inverter_mode",
                "data": {"option": "Self Use"},
            }
        ],
        "max_grid_charge_rate": 5000,
        "max_pv_charge_rate": 8000,
    }


@pytest.fixture
def inverter(default_config):
    """Creates an InverterHA instance — no HTTP call on init."""
    return InverterHA(default_config)


# =========================================================================
# 1. Initialization
# =========================================================================


class TestInverterHAInitialization:
    """Tests for InverterHA initialization and configuration parsing."""

    def test_init_sets_url_and_token(self, inverter):
        assert inverter.url == "http://homeassistant.local:8123"
        assert inverter.token == "test-long-lived-access-token"

    def test_url_trailing_slash_stripped(self):
        cfg = {"url": "http://ha.local:8123/", "token": "tok"}
        inv = InverterHA(cfg)
        assert inv.url == "http://ha.local:8123"

    def test_token_stored_correctly(self, inverter):
        assert inverter.token == "test-long-lived-access-token"

    def test_config_sequences_loaded(self, inverter, default_config):
        assert (
            inverter.mode_sequences["force_charge"]
            == default_config["charge_from_grid"]
        )
        assert (
            inverter.mode_sequences["avoid_discharge"]
            == default_config["avoid_discharge"]
        )
        assert (
            inverter.mode_sequences["allow_discharge"]
            == default_config["discharge_allowed"]
        )

    def test_default_max_rates(self):
        cfg = {"url": "http://ha.local", "token": "tok"}
        inv = InverterHA(cfg)
        assert inv.max_grid_charge_rate == 5000
        assert inv.max_pv_charge_rate == 5000

    def test_custom_max_rates(self, inverter):
        assert inverter.max_grid_charge_rate == 5000
        assert inverter.max_pv_charge_rate == 8000

    def test_missing_url_logs_error(self, caplog):
        cfg = {"url": "", "token": "tok"}
        InverterHA(cfg)
        assert "Missing URL or Token" in caplog.text

    def test_missing_token_logs_error(self, caplog):
        cfg = {"url": "http://ha.local", "token": ""}
        InverterHA(cfg)
        assert "Missing URL or Token" in caplog.text

    def test_inherits_from_base_inverter(self, inverter):
        assert isinstance(inverter, BaseInverter)

    def test_base_inverter_attributes(self, inverter):
        assert inverter.address == "http://homeassistant.local:8123"
        assert inverter.is_authenticated is False
        assert inverter.inverter_type == "InverterHA"

    def test_initial_current_mode_is_none(self, inverter):
        assert inverter.current_mode is None

    def test_token_leading_trailing_whitespace_stripped(self, caplog):
        """Token with leading/trailing whitespace (YAML >- block scalar) is stripped."""
        cfg = {"url": "http://ha.local", "token": "  mytoken  "}
        inv = InverterHA(cfg)
        assert inv.token == "mytoken"
        assert "whitespace stripped" in caplog.text

    def test_token_newline_stripped(self, caplog):
        """Token with embedded newline from YAML >- multi-line block is stripped."""
        cfg = {"url": "http://ha.local", "token": "mytoken\n"}
        inv = InverterHA(cfg)
        assert inv.token == "mytoken"
        assert "whitespace stripped" in caplog.text

    def test_token_internal_whitespace_warns(self, caplog):
        """Token with internal whitespace logs an authentication-failure warning."""
        cfg = {"url": "http://ha.local", "token": "part1 part2"}
        inv = InverterHA(cfg)
        assert inv.token == "part1 part2"
        assert "internal whitespace" in caplog.text

    def test_clean_token_no_warning(self, caplog):
        """Clean token produces no whitespace warning."""
        cfg = {"url": "http://ha.local", "token": "cleantoken"}
        InverterHA(cfg)
        assert "whitespace" not in caplog.text


# =========================================================================
# 2. _call_service()
# =========================================================================


class TestCallService:
    """Tests for _call_service() — single HA service calls."""

    @patch("src.interfaces.inverters.inverter_ha.requests.post")
    def test_successful_service_call(self, mock_post, inverter):
        mock_resp = Mock()
        mock_resp.raise_for_status = Mock()
        mock_post.return_value = mock_resp

        step = {
            "service": "select.select_option",
            "entity_id": "select.inverter_mode",
            "data": {"option": "Self Use"},
        }
        result = inverter._call_service(step)

        assert result is True
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "api/services/select/select_option" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["entity_id"] == "select.inverter_mode"

    def test_invalid_service_format_no_dot(self, inverter):
        step = {"service": "invalid_service"}
        assert inverter._call_service(step) is False

    def test_missing_service_key(self, inverter):
        step = {"entity_id": "switch.test"}
        assert inverter._call_service(step) is False

    @patch("src.interfaces.inverters.inverter_ha.requests.post")
    def test_http_error_returns_false(self, mock_post, inverter):
        mock_resp = Mock()
        mock_resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError(
            "500 Server Error"
        )
        mock_post.return_value = mock_resp

        step = {"service": "switch.turn_on", "entity_id": "switch.test"}
        assert inverter._call_service(step) is False

    @patch("src.interfaces.inverters.inverter_ha.requests.post")
    def test_connection_error_returns_false(self, mock_post, inverter):
        mock_post.side_effect = req_lib.exceptions.ConnectionError("unreachable")

        step = {"service": "switch.turn_on", "entity_id": "switch.test"}
        assert inverter._call_service(step) is False

    @patch("src.interfaces.inverters.inverter_ha.requests.post")
    def test_template_rendering_full_variable(self, mock_post, inverter):
        """{{ power }} as the entire value -> replaced with int (not string)."""
        mock_resp = Mock()
        mock_resp.raise_for_status = Mock()
        mock_post.return_value = mock_resp

        step = {
            "service": "number.set_value",
            "entity_id": "number.charge_power",
            "data_template": {"value": "{{ power }}"},
        }
        inverter._call_service(step, variables={"power": 3000})

        payload = mock_post.call_args[1]["json"]
        assert payload["value"] == 3000  # int, not "3000"

    @patch("src.interfaces.inverters.inverter_ha.requests.post")
    def test_template_rendering_substring(self, mock_post, inverter):
        """{{ power }} as substring -> replaced as string."""
        mock_resp = Mock()
        mock_resp.raise_for_status = Mock()
        mock_post.return_value = mock_resp

        step = {
            "service": "number.set_value",
            "entity_id": "number.charge_power",
            "data_template": {"value": "power={{ power }}W"},
        }
        inverter._call_service(step, variables={"power": 2500})

        payload = mock_post.call_args[1]["json"]
        assert payload["value"] == "power=2500W"

    @patch("src.interfaces.inverters.inverter_ha.requests.post")
    def test_no_variables_passes_data_directly(self, mock_post, inverter):
        mock_resp = Mock()
        mock_resp.raise_for_status = Mock()
        mock_post.return_value = mock_resp

        step = {
            "service": "switch.turn_on",
            "entity_id": "switch.test",
            "data": {"brightness": 100},
        }
        inverter._call_service(step)

        payload = mock_post.call_args[1]["json"]
        assert payload["brightness"] == 100


# =========================================================================
# 3. _execute_sequence()
# =========================================================================


class TestExecuteSequence:
    """Tests for _execute_sequence() — multi-step service calls."""

    def test_empty_sequence_returns_false(self, inverter):
        assert inverter._execute_sequence([]) is False
        assert inverter._execute_sequence(None) is False

    @patch.object(InverterHA, "_call_service", return_value=True)
    def test_multi_step_all_succeed(self, mock_call, inverter):
        seq = [{"service": "a.b"}, {"service": "c.d"}]
        assert inverter._execute_sequence(seq) is True
        assert mock_call.call_count == 2

    @patch.object(InverterHA, "_call_service", side_effect=[True, False])
    def test_partial_failure_returns_false(self, mock_call, inverter):
        seq = [{"service": "a.b"}, {"service": "c.d"}]
        assert inverter._execute_sequence(seq) is False
        # Both steps still attempted (no early exit)
        assert mock_call.call_count == 2

    @patch.object(InverterHA, "_call_service", return_value=True)
    def test_variables_forwarded(self, mock_call, inverter):
        seq = [{"service": "a.b"}]
        inverter._execute_sequence(seq, variables={"power": 1000})
        mock_call.assert_called_once_with(seq[0], {"power": 1000})


# =========================================================================
# 4. set_mode_force_charge()
# =========================================================================


class TestSetModeForceCharge:
    """Tests for set_mode_force_charge()."""

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_with_explicit_power(self, mock_exec, inverter):
        result = inverter.set_mode_force_charge(charge_power_w=3000)
        assert result is True
        mock_exec.assert_called_once_with(
            inverter.mode_sequences["force_charge"], variables={"power": 3000}
        )

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_default_power_uses_max_grid_rate(self, mock_exec, inverter):
        inverter.set_mode_force_charge()
        mock_exec.assert_called_once_with(
            inverter.mode_sequences["force_charge"],
            variables={"power": inverter.max_grid_charge_rate},
        )

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_negative_power_clamped_to_zero(self, mock_exec, inverter):
        inverter.set_mode_force_charge(charge_power_w=-500)
        call_vars = mock_exec.call_args[1]["variables"]
        assert call_vars["power"] == 0

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_excessive_power_clamped_to_max(self, mock_exec, inverter):
        inverter.set_mode_force_charge(charge_power_w=99999)
        call_vars = mock_exec.call_args[1]["variables"]
        assert call_vars["power"] == inverter.max_grid_charge_rate

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_sets_current_mode(self, mock_exec, inverter):
        inverter.set_mode_force_charge()
        assert inverter.current_mode == "force_charge"

    @patch.object(InverterHA, "_execute_sequence", return_value=False)
    def test_returns_false_on_failure(self, mock_exec, inverter):
        result = inverter.set_mode_force_charge()
        assert result is False
        assert inverter.current_mode == "force_charge"

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_return_type_is_bool(self, mock_exec, inverter):
        result = inverter.set_mode_force_charge()
        assert isinstance(result, bool)


# =========================================================================
# 5. set_mode_avoid_discharge()
# =========================================================================


class TestSetModeAvoidDischarge:
    """Tests for set_mode_avoid_discharge()."""

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_success(self, mock_exec, inverter):
        result = inverter.set_mode_avoid_discharge()
        assert result is True
        mock_exec.assert_called_once_with(inverter.mode_sequences["avoid_discharge"])

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_sets_current_mode(self, mock_exec, inverter):
        inverter.set_mode_avoid_discharge()
        assert inverter.current_mode == "avoid_discharge"

    @patch.object(InverterHA, "_execute_sequence", return_value=False)
    def test_returns_false_on_failure(self, mock_exec, inverter):
        assert inverter.set_mode_avoid_discharge() is False

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_return_type_is_bool(self, mock_exec, inverter):
        assert isinstance(inverter.set_mode_avoid_discharge(), bool)


# =========================================================================
# 6. set_mode_allow_discharge()
# =========================================================================


class TestSetModeAllowDischarge:
    """Tests for set_mode_allow_discharge()."""

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_success(self, mock_exec, inverter):
        result = inverter.set_mode_allow_discharge()
        assert result is True
        mock_exec.assert_called_once_with(inverter.mode_sequences["allow_discharge"])

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_sets_current_mode(self, mock_exec, inverter):
        inverter.set_mode_allow_discharge()
        assert inverter.current_mode == "allow_discharge"

    @patch.object(InverterHA, "_execute_sequence", return_value=False)
    def test_returns_false_on_failure(self, mock_exec, inverter):
        assert inverter.set_mode_allow_discharge() is False


# =========================================================================
# 7. api_set_max_pv_charge_rate()
# =========================================================================


class TestApiSetMaxPvChargeRate:
    """Tests for api_set_max_pv_charge_rate()."""

    def test_updates_internal_value(self, inverter):
        inverter.api_set_max_pv_charge_rate(12000)
        assert inverter.max_pv_charge_rate == 12000

    def test_overwrite_previous_value(self, inverter):
        inverter.api_set_max_pv_charge_rate(1000)
        inverter.api_set_max_pv_charge_rate(2000)
        assert inverter.max_pv_charge_rate == 2000


# =========================================================================
# 8. BaseInverter stubs
# =========================================================================


class TestBaseInverterStubs:
    """Tests for BaseInverter stub methods."""

    def test_initialize_sets_authenticated(self, inverter):
        assert inverter.is_authenticated is False
        inverter.initialize()
        assert inverter.is_authenticated is True

    def test_authenticate_returns_true(self, inverter):
        assert inverter.authenticate() is True

    def test_connect_inverter_returns_true(self, inverter):
        assert inverter.connect_inverter() is True

    def test_disconnect_inverter_returns_true(self, inverter):
        assert inverter.disconnect_inverter() is True

    def test_get_battery_info_returns_empty_dict(self, inverter):
        result = inverter.get_battery_info()
        assert result == {}

    def test_fetch_inverter_data_returns_empty_dict(self, inverter):
        result = inverter.fetch_inverter_data()
        assert result == {}


# =========================================================================
# 9. set_battery_mode()
# =========================================================================


class TestSetBatteryMode:
    """Tests for set_battery_mode() dispatch."""

    @patch.object(InverterHA, "set_mode_force_charge", return_value=True)
    def test_dispatch_force_charge(self, mock_fc, inverter):
        assert inverter.set_battery_mode("force_charge") is True
        mock_fc.assert_called_once()

    @patch.object(InverterHA, "set_mode_avoid_discharge", return_value=True)
    def test_dispatch_avoid_discharge(self, mock_ad, inverter):
        assert inverter.set_battery_mode("avoid_discharge") is True
        mock_ad.assert_called_once()

    @patch.object(InverterHA, "set_mode_allow_discharge", return_value=True)
    def test_dispatch_allow_discharge(self, mock_ad, inverter):
        assert inverter.set_battery_mode("allow_discharge") is True
        mock_ad.assert_called_once()

    def test_unknown_mode_returns_false(self, inverter):
        assert inverter.set_battery_mode("turbo_mode") is False


# =========================================================================
# 10. set_allow_grid_charging()
# =========================================================================


class TestSetAllowGridCharging:
    """Tests for set_allow_grid_charging()."""

    @patch.object(InverterHA, "_execute_sequence", return_value=True)
    def test_true_executes_charge_sequence(self, mock_exec, inverter):
        inverter.set_allow_grid_charging(True)
        mock_exec.assert_called_once_with(inverter.mode_sequences["force_charge"])

    @patch.object(InverterHA, "_execute_sequence")
    def test_false_does_not_execute(self, mock_exec, inverter):
        inverter.set_allow_grid_charging(False)
        mock_exec.assert_not_called()


# =========================================================================
# 11. shutdown()
# =========================================================================


class TestShutdown:
    """Tests for shutdown() (inherited from BaseInverter)."""

    def test_shutdown_does_not_raise(self, inverter):
        inverter.shutdown()  # should not raise
