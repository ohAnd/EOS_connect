"""
Unit tests for the UpdateChecker class in src.interfaces.update_checker.

This module contains tests for initialization, GHCR API interaction,
version parsing, update detection, and background service lifecycle.
"""

from unittest.mock import patch, MagicMock
import pytest
import logging
import requests
from src.interfaces.update_checker import UpdateChecker

# Accessing protected members is fine in white-box tests.
# Pytest fixtures: unused-argument and redefined-outer-name are expected patterns
# pylint: disable=protected-access,unused-argument,redefined-outer-name


@pytest.fixture(autouse=True)
def setup_logging():
    """Setup basic logging for tests."""
    logging.basicConfig(level=logging.DEBUG)
    yield
    # Clean up logging handlers after tests
    logging.getLogger("__main__").handlers.clear()


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """
    Fixture to patch threading.Thread to avoid starting real threads during tests.
    """

    class DummyThread:
        """Dummy thread class for testing without actual threading."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    monkeypatch.setattr("threading.Thread", DummyThread)


@pytest.fixture
def mock_port_interface(monkeypatch):
    """Mock PortInterface to control HA Add-on detection."""
    monkeypatch.setattr(
        "src.interfaces.update_checker.PortInterface.is_running_in_hassio",
        lambda: False,
    )


def test_init_stable_version(mock_port_interface):
    """Test initialization with a stable version."""
    checker = UpdateChecker("0.2.30")

    assert checker.current_version == "0.2.30"
    assert checker.is_develop is False
    assert checker.current_version_clean == "0.2.30"
    assert checker.is_ha_addon is False
    assert checker.update_available is False
    assert checker.latest_version is None


def test_init_develop_version(mock_port_interface):
    """Test initialization with a develop version."""
    checker = UpdateChecker("0.2.31.236-develop")

    assert checker.current_version == "0.2.31.236-develop"
    assert checker.is_develop is True
    assert checker.current_version_clean == "0.2.31.236"
    assert checker.is_ha_addon is False


def test_init_ha_addon_disabled(monkeypatch):
    """Test that update checking is disabled for HA Add-on."""
    monkeypatch.setattr(
        "src.interfaces.update_checker.PortInterface.is_running_in_hassio", lambda: True
    )

    checker = UpdateChecker("0.2.30")

    assert checker.is_ha_addon is True
    status = checker.get_update_status()
    assert status["enabled"] is False
    assert status["is_ha_addon"] is True


def test_init_invalid_version(mock_port_interface):
    """Test initialization with invalid version format."""
    checker = UpdateChecker("invalid-version")

    assert checker.current_version_parsed is None
    status = checker.get_update_status()
    assert status["enabled"] is False


def test_get_update_status_structure(mock_port_interface):
    """Test that get_update_status returns correct structure."""
    checker = UpdateChecker("0.2.30")
    status = checker.get_update_status()

    # Check all expected keys are present
    assert "enabled" in status
    assert "is_ha_addon" in status
    assert "current_version" in status
    assert "is_develop_branch" in status
    assert "update_available" in status
    assert "latest_version" in status
    assert "last_check_time" in status
    assert "last_check_success" in status
    assert "last_error" in status
    assert "next_check_in_seconds" in status

    # Check initial values
    assert status["enabled"] is True
    assert status["current_version"] == "0.2.30"
    assert status["is_develop_branch"] is False
    assert status["update_available"] is False


def test_get_ghcr_token_success(mock_port_interface):
    """Test successful GHCR token retrieval."""
    checker = UpdateChecker("0.2.30")

    with patch("src.interfaces.update_checker.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"token": "test-token-12345"}
        mock_get.return_value = mock_resp

        token = checker._get_ghcr_token()

        assert token == "test-token-12345"
        assert mock_get.called


def test_get_ghcr_token_access_token_field(mock_port_interface):
    """Test GHCR token retrieval when using 'access_token' field."""
    checker = UpdateChecker("0.2.30")

    with patch("src.interfaces.update_checker.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "alt-token-67890"}
        mock_get.return_value = mock_resp

        token = checker._get_ghcr_token()

        assert token == "alt-token-67890"


def test_get_ghcr_token_failure(mock_port_interface):
    """Test GHCR token retrieval failure."""
    checker = UpdateChecker("0.2.30")

    with patch("src.interfaces.update_checker.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        token = checker._get_ghcr_token()

        assert token is None


def test_get_ghcr_token_network_error(mock_port_interface):
    """Test GHCR token retrieval with network error."""
    checker = UpdateChecker("0.2.30")

    with patch("src.interfaces.update_checker.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.Timeout()

        token = checker._get_ghcr_token()

        assert token is None


def test_get_ghcr_tags_success(mock_port_interface):
    """Test successful GHCR tags retrieval."""
    checker = UpdateChecker("0.2.30")

    with patch("src.interfaces.update_checker.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tags": ["0.2.30", "0.2.29", "latest", "develop"]
        }
        mock_get.return_value = mock_resp

        tags = checker._get_ghcr_tags("test-token")

        assert tags == ["0.2.30", "0.2.29", "latest", "develop"]


def test_get_ghcr_tags_failure(mock_port_interface):
    """Test GHCR tags retrieval failure."""
    checker = UpdateChecker("0.2.30")

    with patch("src.interfaces.update_checker.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        tags = checker._get_ghcr_tags("test-token")

        assert tags is None


def test_find_latest_version_stable(mock_port_interface):
    """Test finding latest stable version from tags."""
    checker = UpdateChecker("0.2.28")  # Stable version

    tags = [
        "0.2.30",
        "0.2.29",
        "0.2.28",
        "0.2.31.236-develop",
        "0.2.31.235-develop",
        "latest",
        "develop",
        "feature-test",
    ]

    latest = checker._find_latest_version(tags)

    assert latest is not None
    assert latest["tag"] == "0.2.30"


def test_find_latest_version_develop(mock_port_interface):
    """Test finding latest develop version from tags."""
    checker = UpdateChecker("0.2.31.234-develop")  # Develop version

    tags = [
        "0.2.30",
        "0.2.29",
        "0.2.31.236-develop",
        "0.2.31.235-develop",
        "0.2.31.234-develop",
        "latest",
        "develop",
    ]

    latest = checker._find_latest_version(tags)

    assert latest is not None
    assert latest["tag"] == "0.2.31.236-develop"


def test_find_latest_version_no_matching_branch(mock_port_interface):
    """Test finding latest version when no matching branch versions exist."""
    checker = UpdateChecker("0.2.30")  # Stable version

    tags = ["0.2.31.236-develop", "develop", "latest"]  # Only develop versions

    latest = checker._find_latest_version(tags)

    assert latest is None


def test_find_latest_version_filters_special_tags(mock_port_interface):
    """Test that special tags are filtered out."""
    checker = UpdateChecker("0.2.30")

    tags = ["latest", "release", "develop", "feature-branch"]

    latest = checker._find_latest_version(tags)

    assert latest is None


def test_check_for_updates_update_available(mock_port_interface):
    """Test check_for_updates when update is available."""
    checker = UpdateChecker("0.2.28")

    with patch.object(checker, "_get_ghcr_token", return_value="test-token"):
        with patch.object(
            checker, "_get_ghcr_tags", return_value=["0.2.30", "0.2.29", "0.2.28"]
        ):
            success = checker.check_for_updates()

            assert success is True
            assert checker.update_available is True
            assert checker.latest_version_tag == "0.2.30"
            assert checker.last_check_success is True
            assert checker.last_error is None


def test_check_for_updates_no_update(mock_port_interface):
    """Test check_for_updates when on latest version."""
    checker = UpdateChecker("0.2.30")

    with patch.object(checker, "_get_ghcr_token", return_value="test-token"):
        with patch.object(
            checker, "_get_ghcr_tags", return_value=["0.2.30", "0.2.29", "0.2.28"]
        ):
            success = checker.check_for_updates()

            assert success is True
            assert checker.update_available is False
            assert checker.latest_version_tag == "0.2.30"
            assert checker.last_check_success is True


def test_check_for_updates_token_failure(mock_port_interface):
    """Test check_for_updates when token retrieval fails."""
    checker = UpdateChecker("0.2.30")

    with patch.object(checker, "_get_ghcr_token", return_value=None):
        success = checker.check_for_updates()

        assert success is False
        assert checker.last_check_success is False
        assert checker.last_error == "Failed to get GHCR authentication token"


def test_check_for_updates_tags_failure(mock_port_interface):
    """Test check_for_updates when tags retrieval fails."""
    checker = UpdateChecker("0.2.30")

    with patch.object(checker, "_get_ghcr_token", return_value="test-token"):
        with patch.object(checker, "_get_ghcr_tags", return_value=None):
            success = checker.check_for_updates()

            assert success is False
            assert checker.last_check_success is False
            assert checker.last_error == "Failed to retrieve tags from GHCR"


def test_check_for_updates_no_matching_versions(mock_port_interface):
    """Test check_for_updates when no matching versions found."""
    checker = UpdateChecker("0.2.30")  # Stable

    with patch.object(checker, "_get_ghcr_token", return_value="test-token"):
        with patch.object(
            checker, "_get_ghcr_tags", return_value=["0.2.31.236-develop"]
        ):
            success = checker.check_for_updates()

            assert success is False
            assert checker.last_check_success is False
            assert "No stable versions found" in checker.last_error


def test_check_for_updates_exception_handling(mock_port_interface):
    """Test check_for_updates exception handling."""
    checker = UpdateChecker("0.2.30")

    with patch.object(
        checker, "_get_ghcr_token", side_effect=Exception("Network error")
    ):
        success = checker.check_for_updates()

        assert success is False
        assert checker.last_check_success is False
        assert "Unexpected error" in checker.last_error


def test_check_for_updates_ha_addon_skips(monkeypatch):
    """Test that check_for_updates returns False for HA Add-on."""
    monkeypatch.setattr(
        "src.interfaces.update_checker.PortInterface.is_running_in_hassio", lambda: True
    )

    checker = UpdateChecker("0.2.30")
    success = checker.check_for_updates()

    assert success is False


def test_shutdown_stops_thread(mock_port_interface):
    """Test that shutdown properly stops the background thread."""
    checker = UpdateChecker("0.2.30")

    # Mock the thread
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    checker._update_thread = mock_thread

    checker.shutdown()

    assert checker._stop_event.is_set()
    assert mock_thread.join.called
