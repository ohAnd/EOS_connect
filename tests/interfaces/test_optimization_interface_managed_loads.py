"""
Managed loads reaching the backend *through* the facade the application calls.

This file exists because of a bug the rest of the suite could not see. Every test for
the feature drove `LocalEVOptBackend` directly, and the application does not: it holds
an `OptimizationInterface` wrapping one of three backends. The capability flag and the
extra argument both stopped at that wrapper, so the whole path was dead on the live
install while every test passed - the load quietly fell back to the per-slot rule.

The lesson generalises: a capability read with `getattr` off whatever object the caller
happens to hold degrades to "no" in exactly the case nobody tests.
"""

from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.interfaces.optimization_interface import OptimizationInterface

BERLIN = ZoneInfo("Europe/Berlin")

RECORDS = [{"id": "pool", "demand_wh": 6000.0, "max_power_w": 1500.0,
            "value_eur_per_wh": 0.00025, "feasible": [True] * 48,
            "min_runtime_slots": 1, "urgent_wh": 0.0}]


def _interface(source, backend=None):
    """An interface with its backend swapped out, so no solver or network is needed."""
    interface = OptimizationInterface.__new__(OptimizationInterface)
    interface.eos_source = source
    interface.time_frame_base = 3600
    interface.time_zone = BERLIN
    interface.timeout = 60
    interface.last_eos_request = None
    interface.backend = backend if backend is not None else MagicMock()
    interface.backend.optimize.return_value = ({"ac_charge": []}, 1.0)
    return interface


def test_the_capability_is_read_through_to_the_backend():
    backend = MagicMock()
    backend.schedules_managed_loads = True
    assert _interface("local_evopt", backend).schedules_managed_loads is True


def test_a_backend_that_cannot_schedule_says_so():
    backend = MagicMock()
    del backend.schedules_managed_loads          # as EOSBackend and EVOptBackend are
    assert _interface("eos_server", backend).schedules_managed_loads is False


def test_the_loads_reach_a_backend_that_can_take_them():
    backend = MagicMock()
    backend.schedules_managed_loads = True
    backend.optimize.return_value = ({"ac_charge": []}, 1.0)

    _interface("local_evopt", backend).optimize({"ems": {}}, managed_loads=RECORDS)

    _, kwargs = backend.optimize.call_args
    assert kwargs["managed_loads"] == RECORDS


def test_a_backend_that_cannot_take_them_is_never_offered_them():
    """
    It would be a TypeError at best. At worst - for a backend that forwards what it is
    given - it would put an unknown key on the wire to a server that validates.
    """
    backend = MagicMock()
    del backend.schedules_managed_loads
    backend.optimize.return_value = ({"ac_charge": []}, 1.0)

    _interface("eos_server", backend).optimize({"ems": {}}, managed_loads=RECORDS)

    _, kwargs = backend.optimize.call_args
    assert "managed_loads" not in kwargs


def test_no_loads_calls_the_backend_exactly_as_before():
    backend = MagicMock()
    backend.schedules_managed_loads = True
    backend.optimize.return_value = ({"ac_charge": []}, 1.0)

    _interface("local_evopt", backend).optimize({"ems": {}})

    args, kwargs = backend.optimize.call_args
    assert "managed_loads" not in kwargs
    assert args[0] == {"ems": {}}


def test_the_request_itself_never_carries_them():
    """The whole reason they are a separate argument."""
    backend = MagicMock()
    backend.schedules_managed_loads = True
    backend.optimize.return_value = ({"ac_charge": []}, 1.0)

    request = {"ems": {"gesamtlast": [1.0]}}
    _interface("local_evopt", backend).optimize(request, managed_loads=RECORDS)

    sent = backend.optimize.call_args[0][0]
    assert "managed_loads" not in sent
    assert sent == request


@pytest.mark.parametrize("source", ["eos_server", "evopt"])
def test_the_real_external_backends_do_not_claim_the_capability(source):
    """
    Read off the classes themselves rather than a mock, so a backend that grows the
    attribute later cannot pass this by accident.
    """
    from src.interfaces.optimization_backends.optimization_backend_eos import (  # noqa: E402
        EOSBackend,
    )
    from src.interfaces.optimization_backends.optimization_backend_evopt import (  # noqa: E402
        EVOptBackend,
    )

    backend_class = EOSBackend if source == "eos_server" else EVOptBackend
    assert getattr(backend_class, "schedules_managed_loads", False) is False


def test_the_local_backend_does_claim_it():
    from src.interfaces.optimization_backends.optimization_backend_local_evopt import (  # noqa: E402
        LocalEVOptBackend,
    )

    assert LocalEVOptBackend.schedules_managed_loads is True
