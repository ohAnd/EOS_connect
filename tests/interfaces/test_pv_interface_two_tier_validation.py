# pylint: disable=protected-access
"""
Unit tests for PvInterface two-tier validation system (Issue #259).

Tests for graceful degradation at startup (lenient mode) vs strict validation
at runtime/hot-reload. Ensures addon doesn't crash when config is incomplete,
allowing users to access web UI to fix configuration.
"""

import pytest
from src.interfaces.pv_interface import PvInterface

time_frame_base = 3600


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """
    Fixture to patch threading.Thread to avoid starting real threads during tests.
    """

    class DummyThread:
        """A dummy thread class used for testing purposes."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            """Return False so shutdown() doesn't wait for thread."""
            return False

        def join(self):
            pass

    monkeypatch.setattr("threading.Thread", DummyThread)


# ============================================================================
# FIXTURES: Configuration Templates
# ============================================================================


@pytest.fixture
def incomplete_victron_config():
    """Victron config missing VRM ID (resource_id)."""
    config_source = {"source": "victron", "api_key": "test-key"}
    config = [{"name": "System", "lat": 51.5, "lon": 10.0}]  # Missing: resource_id
    return config_source, config


@pytest.fixture
def incomplete_victron_no_api_key():
    """Victron config missing API key."""
    config_source = {"source": "victron", "resource_id": "vrm-123"}  # Missing: api_key
    config = [
        {"name": "System", "lat": 51.5, "lon": 10.0}
    ]
    return config_source, config


@pytest.fixture
def incomplete_solcast_config():
    """Solcast config missing API key."""
    config_source = {"source": "solcast", "resource_id": "123456"}  # Missing: api_key
    config = [{"name": "System"}]
    return config_source, config


@pytest.fixture
def incomplete_solcast_no_resource_id():
    """Solcast config missing resource ID."""
    config_source = {"source": "solcast", "api_key": "test-key"}
    config = [{"name": "System"}]  # Missing: resource_id
    return config_source, config


@pytest.fixture
def valid_victron_config():
    """Complete Victron config."""
    config_source = {"source": "victron", "api_key": "test-key", "resource_id": "vrm-123"}
    config = [{"name": "System", "lat": 51.5, "lon": 10.0}]
    return config_source, config


@pytest.fixture
def valid_solcast_config():
    """Complete Solcast config."""
    config_source = {"source": "solcast", "api_key": "test-key", "resource_id": "123456"}
    config = [{"name": "System", "lat": 51.5, "lon": 10.0}]
    return config_source, config


@pytest.fixture
def empty_config():
    """Empty PV config."""
    config_source = {"source": "victron", "api_key": "test-key"}
    config = []
    return config_source, config


# ============================================================================
# TEST SUITE 1: Startup Validation - Graceful Degradation
# ============================================================================


class TestStartupValidationGracefulDegradation:
    """Tests for lenient startup validation (strict=False)."""

    def test_startup_with_incomplete_victron_vrm_id_does_not_crash(
        self, incomplete_victron_config
    ):
        """
        CRITICAL: Startup with missing Victron VRM ID should NOT crash (sys.exit).
        Should set configuration_state='incomplete', configuration_valid=False.
        """
        config_source, config = incomplete_victron_config

        # Should not raise SystemExit
        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_startup_with_incomplete_victron_no_api_key_does_not_crash(
        self, incomplete_victron_no_api_key
    ):
        """Startup with missing Victron API key should NOT crash."""
        config_source, config = incomplete_victron_no_api_key

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_startup_with_incomplete_solcast_no_api_key_does_not_crash(
        self, incomplete_solcast_config
    ):
        """Startup with missing Solcast API key should NOT crash."""
        config_source, config = incomplete_solcast_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_startup_with_incomplete_solcast_no_resource_id_does_not_crash(
        self, incomplete_solcast_no_resource_id
    ):
        """Startup with missing Solcast resource ID should NOT crash."""
        config_source, config = incomplete_solcast_no_resource_id

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_startup_with_empty_config_does_not_crash(self, empty_config):
        """
        Startup with empty PV config should NOT crash.
        Empty config means PV not yet configured - graceful degradation with state='incomplete'.
        User can add entries via web UI later (Settings > PV Forecast).
        """
        config_source, config = empty_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # Empty config -> incomplete state (not yet configured)
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_startup_with_dict_instead_of_list_degrades_gracefully(self):
        """
        Structural errors like dict instead of list are still caught as ValueError
        at startup, resulting in graceful degradation (not sys.exit).
        The app starts in DEGRADED mode so user can fix via web UI.
        """
        config_source = {"source": "akkudoktor"}
        config = {"name": "System", "lat": 51.5, "lon": 10.0}  # Dict, not list!

        # Should NOT crash with sys.exit, but should start in degraded mode
        pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

        # Structural errors result in incomplete/degraded mode
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_startup_with_valid_config_succeeds(self, valid_victron_config):
        """Startup with valid config should succeed and set configuration_valid=True."""
        config_source, config = valid_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True


# ============================================================================
# TEST SUITE 2: Configuration State Tracking
# ============================================================================


class TestConfigurationStateTracking:
    """Tests for configuration_state attribute tracking."""

    def test_configuration_state_initialized_as_unknown(self):
        """After construction begins, configuration_state should be set."""
        config_source = {"source": "akkudoktor"}
        config = []

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # After initialization, state should be set (either incomplete or valid)
        assert pv.configuration_state in ["valid", "incomplete", "invalid"]

    def test_configuration_state_incomplete_when_config_invalid(
        self, incomplete_victron_config
    ):
        """Configuration state should be 'incomplete' for incomplete config."""
        config_source, config = incomplete_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "incomplete"

    def test_configuration_state_valid_when_config_valid(self, valid_victron_config):
        """Configuration state should be 'valid' for complete config."""
        config_source, config = valid_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        assert pv.configuration_state == "valid"

    def test_configuration_valid_flag_matches_state(self, valid_victron_config):
        """configuration_valid should match configuration_state."""
        config_source, config = valid_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        if pv.configuration_state == "valid":
            assert pv.configuration_valid is True
        else:
            assert pv.configuration_valid is False


# ============================================================================
# TEST SUITE 3: get_summarized_pv_forecast() Guard for Incomplete Config
# ============================================================================


class TestGetSummarizedPvForecastGuard:
    """Tests for get_summarized_pv_forecast() guard when config incomplete."""

    def test_get_forecast_with_incomplete_config_returns_zeros(
        self, incomplete_victron_config
    ):
        """
        When configuration_valid=False, get_summarized_pv_forecast() should return
        zeros array instead of attempting API calls.
        """
        config_source, config = incomplete_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # Should return zeros, not crash
        forecast = pv.get_summarized_pv_forecast()

        # Should be array of zeros (48 elements)
        assert isinstance(forecast, list)
        assert len(forecast) == 48
        assert all(v == 0.0 for v in forecast)

    def test_get_forecast_with_incomplete_config_does_not_crash(
        self, incomplete_solcast_config
    ):
        """get_summarized_pv_forecast() should never crash, even with incomplete config."""
        config_source, config = incomplete_solcast_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # Should not raise any exception
        try:
            forecast = pv.get_summarized_pv_forecast()
            assert isinstance(forecast, list)
        except Exception as exc:
            pytest.fail(f"get_summarized_pv_forecast() should not crash: {exc}")

    def test_get_forecast_logs_configuration_state_when_incomplete(
        self, incomplete_victron_config, caplog
    ):
        """
        When config incomplete, get_summarized_pv_forecast() should log the configuration state.
        """
        config_source, config = incomplete_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        with caplog.at_level("DEBUG"):
            forecast = pv.get_summarized_pv_forecast()

        # Should log skipping forecast retrieval
        assert any("Skipping PV forecast retrieval" in record.message for record in caplog.records)


# ============================================================================
# TEST SUITE 4: Hot-Reload Validation (Strict Mode)
# ============================================================================


class TestHotReloadValidationStrict:
    """Tests for strict validation during hot-reload."""

    def test_hot_reload_with_incomplete_config_rejected(
        self, valid_victron_config, incomplete_victron_config
    ):
        """
        Hot-reload with incomplete config should reject the change and
        restore previous state. Uses strict validation.
        """
        config_source_valid, config_valid = valid_victron_config
        config_source_incomplete, config_incomplete = incomplete_victron_config

        # Start with valid config
        pv = PvInterface(
            config_source_valid, config_valid, time_frame_base, {}, timezone="UTC"
        )
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

        # Try to reload with incomplete config
        with pytest.raises(ValueError):
            pv.reload_config(
                config_source_incomplete,
                config_incomplete,
                {},
                False,
                "UTC",
            )

        # State should be restored to previous
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

    def test_hot_reload_with_complete_config_accepted(
        self, incomplete_victron_config, valid_victron_config
    ):
        """
        Hot-reload with complete config should accept and update state to 'valid'.
        """
        config_source_incomplete, config_incomplete = incomplete_victron_config
        config_source_valid, config_valid = valid_victron_config

        # Start with incomplete config
        pv = PvInterface(
            config_source_incomplete, config_incomplete, time_frame_base, {}, timezone="UTC"
        )
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

        # Reload with complete config
        pv.reload_config(
            config_source_valid,
            config_valid,
            {},
            False,
            "UTC",
        )

        # State should be updated
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

    def test_hot_reload_saves_configuration_state(self, valid_victron_config):
        """
        reload_config() should save configuration_state and configuration_valid
        to old_state before making changes.
        """
        config_source, config = valid_victron_config

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # Access the internal old_state dict during reload by catching the exception
        config_source_bad = {"source": "victron"}  # Will fail validation
        config_bad = []

        try:
            pv.reload_config(config_source_bad, config_bad, {}, False, "UTC")
        except ValueError:
            pass  # Expected

        # State should be restored even though reload failed
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True


# ============================================================================
# TEST SUITE 5: Validation Mode Parameter (Lenient vs Strict)
# ============================================================================


class TestValidationModeParameter:
    """Tests for strict parameter in validation methods."""

    def test_check_config_with_strict_true_raises_on_missing_victron_vrm(
        self, incomplete_victron_config
    ):
        """
        __check_config(strict=True) should raise ValueError for missing Victron VRM ID.
        This is used for hot-reload validation.
        """
        config_source, config = incomplete_victron_config
        config_source_copy = config_source.copy()
        config_copy = [c.copy() for c in config]

        pv = PvInterface(
            {"source": "akkudoktor"}, [], time_frame_base, {}, timezone="UTC"
        )

        # Manually set config and call check_config with strict=True
        pv.config = config_copy
        pv.config_source = config_source_copy

        with pytest.raises(ValueError, match="Victron VRM ID"):
            pv._PvInterface__check_config(strict=True)

    def test_check_config_with_strict_false_allows_incomplete_config(
        self, incomplete_victron_config
    ):
        """
        __check_config(strict=False) indicates startup mode (lenient logging),
        but still raises ValueError for incomplete config.
        At startup, these errors are caught and converted to degraded mode.
        """
        config_source, config = incomplete_victron_config
        config_copy = [c.copy() for c in config]

        pv = PvInterface(
            {"source": "akkudoktor"}, [], time_frame_base, {}, timezone="UTC"
        )

        # Manually set config and call check_config with strict=False
        pv.config = config_copy
        pv.config_source = config_source.copy()

        # Should raise ValueError (lenient mode only affects logging, not validation)
        with pytest.raises(ValueError):
            pv._PvInterface__check_config(strict=False)


# ============================================================================
# TEST SUITE 6: Logging Behavior - Error vs Warning
# ============================================================================


class TestLoggingBehavior:
    """Tests for different logging levels in strict vs lenient modes."""

    def test_startup_logs_warning_for_incomplete_config(
        self, incomplete_victron_config, caplog
    ):
        """
        At startup (strict=False), incomplete config should log WARNING, not ERROR.
        """
        config_source, config = incomplete_victron_config

        import logging
        caplog.set_level(logging.WARNING)

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # Should have warnings in logs
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_logs) > 0
        assert any("DEGRADED mode" in r.message for r in warning_logs)

    def test_startup_logs_guidance_to_web_ui(
        self, incomplete_victron_config, caplog
    ):
        """
        Startup validation should log guidance directing user to Settings → PV Forecast.
        """
        config_source, config = incomplete_victron_config

        import logging
        caplog.set_level(logging.WARNING)

        pv = PvInterface(
            config_source, config, time_frame_base, {}, timezone="UTC"
        )

        # Should mention web UI or Settings
        all_logs = [r for r in caplog.records]
        assert any("Settings" in r.message or "web UI" in r.message for r in all_logs)


# ============================================================================
# END OF TEST SUITE
# ============================================================================
