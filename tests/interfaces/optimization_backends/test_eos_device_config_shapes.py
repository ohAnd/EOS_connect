"""
``devices/electric_vehicles`` comes in two shapes, and both have to start.

EOS reorganised its device configuration: what used to be a list of device dicts is
now a mapping keyed by device_id. EOS_connect read the first device by position while
checking the server's EV charge rates, so against a current EOS build startup died
with ``KeyError: 0`` - and because the optimizer interface is built with
critical=True, long before the web server comes up, that took the settings UI down
with it (issue #349).

Kept out of ``test_optimization_backend_eos.py``: that module is already at pylint's
module-size limit.

Fixtures:
    - base_url: Provides the base URL for the EOS server.
    - time_frame_base: Provides the time frame base value.
    - berlin_timezone: Provides a pytz timezone object for Europe/Berlin.

Usage:
    Run with pytest: pytest test_eos_device_config_shapes.py -v
"""

import json
from unittest.mock import Mock, patch
import pytest
import pytz
import requests
from src.interfaces.optimization_backends.optimization_backend_eos import EOSBackend


@pytest.fixture(name="base_url")
def fixture_base_url():
    """
    Provides the base URL for the EOS server.

    Returns:
        str: Base URL for testing.
    """
    return "http://localhost:8503"


@pytest.fixture(name="time_frame_base")
def fixture_time_frame_base():
    """
    Provides the time frame base value.

    Returns:
        int: Time frame base in seconds.
    """
    return 3600


@pytest.fixture(name="berlin_timezone")
def fixture_berlin_timezone():
    """
    Provides a timezone object for Europe/Berlin.

    Returns:
        pytz.timezone: Timezone object.
    """
    return pytz.timezone("Europe/Berlin")


class TestDeviceConfigShapes:
    """
    Tests for the ``devices/electric_vehicles`` handling in ``EOSBackend.__init__``.

    EOS changed that config from a list of device dicts to a mapping keyed by
    device_id with its device rework (0.3.0 dev builds, see issue #349). Both
    shapes have to be read, written back in their own shape, and neither may be
    able to stop the add-on from starting.
    """

    # Dev build the reporter of #349 was running when startup crashed.
    MAPPING_ERA_VERSION = "0.3.0.dev2610201560628681"
    LIST_ERA_VERSION = "0.2.0+dev"

    DEFAULT_RATES = [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _responses(mock_get, mock_put, eos_version, devices, optimization=None):
        """
        Wire up ``requests.get``/``requests.put`` for one EOS server.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            eos_version: Version the server reports on /v1/health.
            devices: JSON the server returns for devices/electric_vehicles,
                or an exception instance to raise instead.
            optimization: JSON for the optimization config; defaults to a
                config that already matches what EOS_connect wants, so no PUT
                is triggered by it.
        """
        if optimization is None:
            optimization = {
                "horizon_hours": 48,
                "genetic": {"individuals": 300, "generations": 400},
            }

        def json_response(payload):
            response = Mock()
            response.json.return_value = payload
            response.raise_for_status = Mock()
            return response

        version_response = json_response({"status": "alive", "version": eos_version})
        optimization_response = json_response(optimization)
        devices_response = json_response(devices)

        def get_side_effect(url, **kwargs):  # pylint: disable=unused-argument
            if "/v1/health" in url:
                return version_response
            if "/v1/config/optimization" in url:
                return optimization_response
            if "/v1/config/devices/electric_vehicles" in url:
                if isinstance(devices, Exception):
                    raise devices
                return devices_response
            return Mock()

        mock_get.side_effect = get_side_effect

        put_response = Mock()
        put_response.raise_for_status = Mock()
        mock_put.return_value = put_response

    @staticmethod
    def _device_puts(mock_put):
        """
        Return the payloads PUT to devices/electric_vehicles, already decoded.

        Args:
            mock_put: Mocked requests.put function.

        Returns:
            list: One decoded JSON body per device config write.
        """
        return [
            json.loads(call.kwargs["data"])
            for call in mock_put.call_args_list
            if "/v1/config/devices/electric_vehicles" in call.args[0]
        ]

    # ------------------------------------------------------------------
    # Mapping shape (current EOS)
    # ------------------------------------------------------------------

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_mapping_with_charge_rates_starts_and_writes_nothing(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        A device mapping that already carries charge rates must be left alone -
        and must not raise. This is the exact shape that produced
        ``KeyError: 0`` and killed startup in #349.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(
            mock_get,
            mock_put,
            self.MAPPING_ERA_VERSION,
            {"ev1": {"device_id": "ev1", "charge_rates": self.DEFAULT_RATES}},
        )

        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        assert backend.eos_version == self.MAPPING_ERA_VERSION
        assert self._device_puts(mock_put) == []

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_mapping_without_charge_rates_is_patched_as_mapping(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        A device mapping missing charge rates gets them filled in, and is written
        back as a mapping - a list would be rejected by a mapping-era server.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(
            mock_get,
            mock_put,
            self.MAPPING_ERA_VERSION,
            {
                "ev1": {"device_id": "ev1", "capacity_wh": 60000},
                "ev2": {"device_id": "ev2", "charge_rates": [0.0, 1.0]},
            },
        )

        EOSBackend(base_url, time_frame_base, berlin_timezone)

        puts = self._device_puts(mock_put)
        assert len(puts) == 1
        written = puts[0]
        assert isinstance(written, dict), "mapping must be written back as a mapping"
        assert written["ev1"]["charge_rates"] == self.DEFAULT_RATES
        assert written["ev1"]["device_id"] == "ev1", "existing fields must survive"
        assert written["ev2"]["charge_rates"] == [0.0, 1.0], "set rates stay untouched"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_empty_mapping_starts_and_writes_nothing(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        An empty device mapping has nothing to patch and must not be indexed into.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(mock_get, mock_put, self.MAPPING_ERA_VERSION, {})

        EOSBackend(base_url, time_frame_base, berlin_timezone)

        assert self._device_puts(mock_put) == []

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_no_devices_on_mapping_era_server_is_left_alone(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        With no EV configured, a mapping-era server supplies its own charge rate
        defaults - pushing a list device into it would only earn a 422.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(mock_get, mock_put, self.MAPPING_ERA_VERSION, None)

        EOSBackend(base_url, time_frame_base, berlin_timezone)

        assert self._device_puts(mock_put) == []

    # ------------------------------------------------------------------
    # List shape (EOS before the device rework)
    # ------------------------------------------------------------------

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_list_without_charge_rates_is_patched_as_list(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        The pre-rework list shape keeps its old behaviour: charge rates filled in
        on the first device, written back as a list.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(
            mock_get, mock_put, self.LIST_ERA_VERSION, [{"device_id": "battery1"}]
        )

        EOSBackend(base_url, time_frame_base, berlin_timezone)

        puts = self._device_puts(mock_put)
        assert len(puts) == 1
        assert isinstance(puts[0], list), "list config must be written back as a list"
        assert puts[0][0]["charge_rates"] == self.DEFAULT_RATES
        assert puts[0][0]["device_id"] == "battery1"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_no_devices_on_list_era_server_still_pushes_rates(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        Servers from before the rework need the charge rates pushed in when no EV
        is configured, so that behaviour has to survive the fix.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(mock_get, mock_put, self.LIST_ERA_VERSION, None)

        EOSBackend(base_url, time_frame_base, berlin_timezone)

        puts = self._device_puts(mock_put)
        assert len(puts) == 1
        assert puts[0] == [{"charge_rates": self.DEFAULT_RATES}]

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_empty_list_starts_and_writes_nothing(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        An empty device list must not be indexed into either.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(mock_get, mock_put, self.LIST_ERA_VERSION, [])

        EOSBackend(base_url, time_frame_base, berlin_timezone)

        assert self._device_puts(mock_put) == []

    # ------------------------------------------------------------------
    # Startup must survive a config it does not understand
    # ------------------------------------------------------------------

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_unexpected_optimization_shape_does_not_stop_startup(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        A config EOS_connect cannot read must not take the backend - and with it
        the settings UI - down. The optimizer interface is created with
        critical=True before the web server starts (#349).

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(
            mock_get,
            mock_put,
            self.MAPPING_ERA_VERSION,
            None,
            optimization=["not", "a", "mapping"],
        )

        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        assert backend.eos_version == self.MAPPING_ERA_VERSION

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_failing_config_request_does_not_stop_startup(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        A config endpoint that errors out (moved, 500, ...) must not stop startup
        either - the version is known, so the backend can still run.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        self._responses(
            mock_get,
            mock_put,
            self.MAPPING_ERA_VERSION,
            requests.exceptions.HTTPError("404 Client Error"),
        )

        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        assert backend.eos_version == self.MAPPING_ERA_VERSION
