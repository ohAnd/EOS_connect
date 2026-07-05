"""Tests for LoadInterface forward-fill functionality."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
import logging

# Mock the logger at module load time
with patch("logging.getLogger") as mock_get_logger:
    mock_logger = Mock()
    mock_get_logger.return_value = mock_logger

from src.interfaces.load_interface import LoadInterface


class TestLoadInterfaceForwardFill:
    """Test suite for forward-fill (LOCF) functionality in LoadInterface."""

    @pytest.fixture
    def config(self):
        """Minimal valid config for LoadInterface."""
        return {
            "source": "homeassistant",
            "url": "http://localhost:8123",
            "load_sensor": "sensor.total_home_power",
            "access_token": "test_token",
        }

    @pytest.fixture
    def load_interface(self, config):
        """Create a LoadInterface instance."""
        with patch("src.interfaces.load_interface.logger"):
            return LoadInterface(config, time_frame_base=3600)

    def get_mock_data(self, states):
        """Helper to create mock sensor data with given states.
        
        Args:
            states: List of state values (can be strings, floats, empty strings, None, etc.)
        
        Returns:
            dict with 'data' key containing list of mock datapoints
        """
        base_time = datetime.fromisoformat("2026-06-12T00:00:00")
        data = []
        for i, state in enumerate(states):
            data.append({
                "state": state,
                "last_updated": (base_time + timedelta(minutes=i*5)).isoformat()
            })
        return {"data": data}

    def test_forward_fill_empty_strings(self, load_interface):
        """Empty strings should be filled with last known value."""
        data = self.get_mock_data(["100.0", "110.5", "", "", "120.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        assert filled_data["data"][2]["state"] == "110.5"
        assert filled_data["data"][3]["state"] == "110.5"
        assert filled_data["data"][4]["state"] == "120.0"

    def test_forward_fill_unavailable_states(self, load_interface):
        """'unavailable' states should be filled."""
        data = self.get_mock_data(["100.0", "unavailable", "110.0", "unknown", "120.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        assert filled_data["data"][1]["state"] == "100.0"
        assert filled_data["data"][3]["state"] == "110.0"

    def test_forward_fill_none_values(self, load_interface):
        """None values should be filled."""
        data = self.get_mock_data(["100.0", None, "110.0", None, "120.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        assert filled_data["data"][1]["state"] == "100.0"
        assert filled_data["data"][3]["state"] == "110.0"

    def test_forward_fill_nan_values(self, load_interface):
        """NaN values should be filled."""
        data = self.get_mock_data(["100.0", float('nan'), "110.0", float('nan'), "120.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        assert filled_data["data"][1]["state"] == "100.0"
        assert filled_data["data"][3]["state"] == "110.0"

    def test_forward_fill_mixed_invalid(self, load_interface):
        """Mixed invalid values should all be filled."""
        data = self.get_mock_data(["150.5", "", None, "unavailable", "160.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        # All middle values should be filled with 150.5
        assert filled_data["data"][1]["state"] == "150.5"
        assert filled_data["data"][2]["state"] == "150.5"
        assert filled_data["data"][3]["state"] == "150.5"

    def test_forward_fill_no_previous_valid_value(self, load_interface):
        """If first values are invalid and no previous valid value, skip filling."""
        data = self.get_mock_data(["", "", "100.0", "", "110.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        # First two cannot be filled (no previous valid)
        assert filled_data["data"][0]["state"] == ""
        assert filled_data["data"][1]["state"] == ""
        # Third onwards can be filled
        assert filled_data["data"][3]["state"] == "100.0"

    def test_forward_fill_logging_debug(self, load_interface):
        """Forward-fill should log a debug message with filled indices."""
        data = self.get_mock_data(["100.0", "", "110.0", "", "120.0"])
        
        with patch("src.interfaces.load_interface.logger") as mock_logger:
            load_interface._LoadInterface__fill_missing_values_in_data(data, "sensor.test")
            
            # Should call debug (not warning)
            assert mock_logger.debug.called
            call_args = mock_logger.debug.call_args[0]
            # Check that debug message mentions filled values and indices
            assert "Filled" in call_args[0]
            assert "indices" in call_args[0]

    def test_forward_fill_empty_data(self, load_interface):
        """Empty data should be returned unchanged."""
        data = {"data": []}
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        assert filled_data == data

    def test_forward_fill_none_data(self, load_interface):
        """None data should be returned as is."""
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(None)
        assert filled_data is None

    def test_forward_fill_single_valid_value(self, load_interface):
        """Single valid value should remain unchanged."""
        data = self.get_mock_data(["100.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        assert filled_data["data"][0]["state"] == "100.0"

    def test_forward_fill_consecutive_gaps(self, load_interface):
        """Multiple consecutive gaps should all be filled with same value."""
        data = self.get_mock_data(["100.0", "", "", "", "", "150.0"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        # All gaps should be filled with 100.0
        for i in range(1, 5):
            assert filled_data["data"][i]["state"] == "100.0"

    def test_forward_fill_preserves_numeric_values(self, load_interface):
        """Valid numeric values should not be modified."""
        data = self.get_mock_data(["100.5", "110.2", "120.9"])
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        assert filled_data["data"][0]["state"] == "100.5"
        assert filled_data["data"][1]["state"] == "110.2"
        assert filled_data["data"][2]["state"] == "120.9"

    def test_energy_calculation_with_forward_fill(self, load_interface):
        """Energy calculation should work with forward-filled data."""
        # Create data with some gaps
        data = self.get_mock_data([
            "100.0",  # 0: 100W
            "",       # 1: filled with 100W
            "200.0",  # 2: 200W
            "",       # 3: filled with 200W
            "150.0"   # 4: 150W
        ])
        
        with patch("src.interfaces.load_interface.logger"):
            # This should not raise an exception
            energy = load_interface._LoadInterface__process_energy_data(data, "sensor.test")
            
            # Should calculate energy without errors
            assert energy > 0
            assert isinstance(energy, float)

    def test_forward_fill_indices_logging_truncation(self, load_interface):
        """Long filled_indices list should be truncated in logging."""
        # Create 20 filled indices
        states = ["100.0"] + [""] * 20 + ["200.0"]
        data = self.get_mock_data(states)
        
        with patch("src.interfaces.load_interface.logger") as mock_logger:
            load_interface._LoadInterface__fill_missing_values_in_data(data, "sensor.test")
            
            # Should show truncated list with "..."
            call_args = mock_logger.debug.call_args[0]
            assert "..." in str(call_args)

    def test_forward_fill_with_float_states(self, load_interface):
        """Forward-fill should work with float values."""
        data = {
            "data": [
                {"state": 100.5, "last_updated": "2026-06-12T00:00:00"},
                {"state": "", "last_updated": "2026-06-12T00:05:00"},
                {"state": 110.0, "last_updated": "2026-06-12T00:10:00"},
            ]
        }
        
        filled_data = load_interface._LoadInterface__fill_missing_values_in_data(data)
        
        # Empty string should be filled with previous float value
        assert filled_data["data"][1]["state"] == 100.5
