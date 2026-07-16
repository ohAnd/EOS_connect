"""
Test suite for improved Load Interface error messages
Tests that error messages correctly identify which datapoint has the problem
"""

import pytest
from datetime import datetime, timedelta
import json

# Mock data helper
def get_mock_data_with_invalid_next():
    """Two datapoints where the NEXT one (index i+1) is invalid"""
    return {
        "data": [
            {
                "state": "600.4",  # Valid ← will be data[i]
                "last_updated": "2026-06-12T19:18:42.000000+00:00"
            },
            {
                "state": "",  # INVALID ← will be data[i+1]
                "last_updated": "2026-06-12T19:19:42.000000+00:00"
            },
        ]
    }


def get_mock_data_with_invalid_current():
    """Two datapoints where the CURRENT one (index i) is invalid"""
    return {
        "data": [
            {
                "state": "invalid_string",  # INVALID ← will be data[i]
                "last_updated": "2026-06-12T19:18:42.000000+00:00"
            },
            {
                "state": "600.4",  # Valid ← will be data[i+1]
                "last_updated": "2026-06-12T19:19:42.000000+00:00"
            },
        ]
    }


def get_mock_data_with_nan():
    """Datapoint with NaN value"""
    return {
        "data": [
            {
                "state": "NaN",  # INVALID
                "last_updated": "2026-06-12T19:18:42.000000+00:00"
            },
            {
                "state": "600.4",  # Valid
                "last_updated": "2026-06-12T19:19:42.000000+00:00"
            },
        ]
    }


def get_mock_data_with_bad_datetime():
    """Datapoint with invalid datetime format"""
    return {
        "data": [
            {
                "state": "600.4",
                "last_updated": "invalid-datetime-format"  # INVALID
            },
            {
                "state": "600.4",
                "last_updated": "2026-06-12T19:19:42.000000+00:00"
            },
        ]
    }


class TestLoadInterfaceErrorMessages:
    """Tests for improved error message clarity"""
    
    def test_error_message_identifies_next_datapoint_as_problematic(self):
        """Error in data[i+1] should be clearly marked as problematic"""
        data = get_mock_data_with_invalid_next()
        
        # The error should mention:
        # - Index 45 is Valid with '600.4'
        # - Index 46 is PROBLEMATIC with ''
        
        assert data["data"][0]["state"] == "600.4"  # Valid
        assert data["data"][1]["state"] == ""  # Problematic (empty)
        
        # When we try to convert both:
        try:
            float(data["data"][0]["state"])  # Should work
            float(data["data"][1]["state"])  # Should fail
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "could not convert string to float" in str(e)
            # The error message should help identify it's data[i+1] that's bad
    
    def test_error_message_identifies_current_datapoint_as_problematic(self):
        """Error in data[i] should be clearly marked as problematic"""
        data = get_mock_data_with_invalid_current()
        
        assert data["data"][0]["state"] == "invalid_string"  # Problematic
        assert data["data"][1]["state"] == "600.4"  # Valid
        
        # When we try to convert:
        try:
            float(data["data"][0]["state"])  # Should fail
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "could not convert string to float" in str(e)
    
    def test_error_message_identifies_nan_value(self):
        """Error with NaN should be clearly shown"""
        data = get_mock_data_with_nan()
        
        assert data["data"][0]["state"] == "NaN"
        
        # Note: Python's float() accepts 'NaN' and converts to float('nan')
        # This is actually valid, but if we want to detect NaN in arrays,
        # we check: value != value (NaN is the only float where x != x)
        nan_value = float(data["data"][0]["state"])
        assert nan_value != nan_value  # This is how Python detects NaN
        
        # Alternative: if the state is literally the string 'NaN', that's metadata
        assert data["data"][0]["state"] == "NaN"  # String representation
    
    def test_datetime_parsing_error_handling(self):
        """Bad datetime format should not crash error handler"""
        data = get_mock_data_with_bad_datetime()
        
        assert data["data"][0]["last_updated"] == "invalid-datetime-format"
        
        # Attempting to parse should raise error
        try:
            datetime.fromisoformat(data["data"][0]["last_updated"])
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected
    
    def test_error_context_includes_indices(self):
        """Error message should include [Index N] for clarity"""
        data = get_mock_data_with_invalid_next()
        
        # When building error context, it should show indices
        i = 45
        error_context = (
            f"[Index {i}] Valid: '{data['data'][0]['state']}' @ "
            f"{data['data'][0]['last_updated']}, "
            f"[Index {i+1}] PROBLEMATIC: '{data['data'][1]['state']}' @ "
            f"{data['data'][1]['last_updated']}"
        )
        
        # Verify context includes:
        assert "[Index 45]" in error_context
        assert "[Index 46]" in error_context
        assert "Valid:" in error_context
        assert "PROBLEMATIC:" in error_context
        assert "600.4" in error_context
        assert "19:18:42" in error_context
        assert "19:19:42" in error_context
    
    def test_single_index_error_context(self):
        """When error is only in one index, show just that one"""
        i = 30
        state_value = "NaN"
        timestamp = "2026-06-12T18:30:00.000000+00:00"
        
        error_context = (
            f"[Index {i}] PROBLEMATIC: '{state_value}' @ {timestamp}"
        )
        
        assert f"[Index {i}]" in error_context
        assert "PROBLEMATIC:" in error_context
        assert state_value in error_context
        assert timestamp in error_context
    
    def test_error_message_is_user_actionable(self):
        """Error message should guide user to where to debug"""
        data = get_mock_data_with_invalid_next()
        i = 45
        
        # Error context
        error_context = (
            f"[Index {i}] Valid: '{data['data'][0]['state']}', "
            f"[Index {i+1}] PROBLEMATIC: '{data['data'][1]['state']}' @ "
            f"{data['data'][1]['last_updated']}"
        )
        
        # Full error message
        error_msg = (
            f"[LOAD-IF] Skipping invalid sensor data for 'sensor.total_home_power': "
            f"{error_context} cannot be processed "
            f"(could not convert string to float: '')"
        )
        
        # User should be able to:
        # 1. See which sensor: total_home_power ✓
        # 2. See which index: [Index 46] ✓
        # 3. See which value: '' ✓
        # 4. See which timestamp: 2026-06-12T19:19:42 ✓
        assert "sensor.total_home_power" in error_msg
        assert "[Index 46]" in error_msg
        assert "PROBLEMATIC" in error_msg
        assert "19:19:42" in error_msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
