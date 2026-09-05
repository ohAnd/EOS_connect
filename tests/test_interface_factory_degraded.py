"""
Does a degraded interface reach the user?

The factory's error path only fires when a constructor *raises*. An interface that is
merely misconfigured does not raise — by design, so the user can fix it in the web UI
instead of the container crash-looping — and so it used to reach the user as nothing
at all. This covers the hook that closes that gap, and the logger wiring without which
the hook writes into a void.
"""

import logging

import pytest

from src.interface_factory import InterfaceFactory
from src.startup_validator import StartupValidator


class _Recorder:
    """Stand-in for StartupValidator that keeps what it was told."""

    def __init__(self):
        self.errors = []

    def add_error(self, **kwargs):
        self.errors.append(kwargs)


class _Stub:
    def __init__(self, state=None, message=""):
        if state is not None:
            self.configuration_state = state
        self.configuration_message = message


def _create(validator, interface):
    return InterfaceFactory(validator)._create_interface(
        component_name="load_interface",
        category="connectivity",
        critical=False,
        title="Load interface unavailable",
        error_message="Failed to retrieve load data",
        config_link="#load",
        creator_func=lambda: interface,
    )


@pytest.mark.parametrize("state", ["incomplete", "invalid"])
def test_a_degraded_interface_is_reported(state):
    validator = _Recorder()

    _create(validator, _Stub(state, "No load sensor is set."))

    assert len(validator.errors) == 1
    reported = validator.errors[0]
    assert reported["component"] == "load_interface"
    assert reported["title"] == "Incomplete configuration", (
        "a setting that was never made is not a connection that broke"
    )
    assert reported["message"] == "No load sensor is set."
    assert reported["action_required"] is True
    assert reported["config_link"] == "#load"
    assert reported["severity"] == "warning"


def test_a_healthy_interface_is_not_reported():
    validator = _Recorder()

    _create(validator, _Stub("valid", ""))

    assert validator.errors == []


def test_a_state_without_an_explanation_is_left_alone():
    """
    A message is required, not just a state, so an interface that tracks
    configuration_state without explaining it keeps its existing behaviour.
    """
    validator = _Recorder()

    _create(validator, _Stub("incomplete", ""))

    assert validator.errors == []


def test_an_interface_that_tracks_nothing_is_left_alone():
    validator = _Recorder()

    _create(validator, object())

    assert validator.errors == []


def test_a_non_critical_failure_that_returned_none_is_not_probed():
    validator = _Recorder()

    assert _create(validator, None) is None
    assert validator.errors == []


def test_the_validator_logs_where_the_ui_can_see_it():
    """
    eos_connect.py attaches its handlers to the "__main__" logger, not to root. A
    getLogger(__name__) here propagated to a handler-less root, so every registered
    startup error — the metadata the alerts panel is built around — was discarded
    before it reached MemoryLogHandler.
    """
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    main_logger = logging.getLogger("__main__")
    main_logger.addHandler(handler)
    try:
        StartupValidator().add_error(
            category="configuration",
            component="battery_interface",
            severity="warning",
            title="Battery sensor unreachable",
            message="No battery SOC sensor is set.",
            action_required=True,
            config_link="#battery",
        )
    finally:
        main_logger.removeHandler(handler)

    assert len(records) == 1
    message = records[0].getMessage()
    assert "battery_interface" in message
    assert "#battery" in message
    assert "ACTION REQUIRED" in message
