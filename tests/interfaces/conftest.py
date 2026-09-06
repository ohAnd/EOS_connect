"""
Shared safety net for the interface tests.

The interfaces are the layer that talks to the outside world, so a test here that stops
patching the right thing does not fail - it makes a real request. That happened: when
the outside-temperature provider became a setting, two tests carried on patching the
Akkudoktor fetch, which was no longer the default, and reached the live Open-Meteo
endpoint instead. They passed on a machine that could not complete the call and failed
in CI, where it could.

Blocking the temperature provider's HTTP client by default turns that into an immediate,
obvious failure. A test that genuinely wants to exercise the request patches
``requests.get`` in the module itself, which replaces this stub.
"""

import types

import pytest
import requests as real_requests


@pytest.fixture(autouse=True)
def no_live_temperature_requests(monkeypatch):
    """
    Fail loudly rather than quietly calling a real weather API.

    The stub replaces the *module reference* the temperature module holds, not
    ``requests.get`` itself: ``requests`` is one shared module object, so patching its
    ``get`` reaches every other caller in the process. Doing that broke fourteen
    unrelated tests that talk to a local stub server.
    """
    from src.interfaces import temperature_forecast

    def refuse(url, **_kwargs):
        raise AssertionError(
            "A test reached the live temperature provider at "
            f"{url}. Patch the fetch it is meant to exercise -- most likely "
            "PvInterface._PvInterface__fetch_temperature, which is where the provider "
            "is chosen."
        )

    stub = types.SimpleNamespace(get=refuse, exceptions=real_requests.exceptions)
    monkeypatch.setattr(temperature_forecast, "requests", stub)
