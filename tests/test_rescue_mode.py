"""
A required interface fails to build. Does the application survive it?

Until this change the factory re-raised on a critical failure and the process
ended.  Under the Home Assistant add-on s6 then tore the container down, so the
configuration UI — the only tool that could correct the setting responsible —
disappeared along with it.  That is issue #349 (a bad EOS ``devices`` shape
raising ``KeyError: 0``), and #310 and #83 before it.

The contract now: the factory records the failure as fatal and returns None, the
caller sees ``BOOT.fatal`` and enters rescue mode, and the boot server keeps
serving the configuration API.  Degrading is not the same as hiding — these
tests also pin that the failure is still reported as an error and that the boot
state says so loudly enough to stop the optimizer.
"""

# pylint: disable=protected-access,redefined-outer-name

import pytest

import src.interface_factory as factory_module
from src.boot_state import BootState
from src.interface_factory import InterfaceFactory


class _Recorder:
    """Stand-in for StartupValidator that keeps what it was told."""

    def __init__(self):
        self.errors = []

    def add_error(self, **kwargs):
        self.errors.append(kwargs)


@pytest.fixture
def boot(monkeypatch):
    """A fresh boot state in place of the module singleton."""
    state = BootState()
    monkeypatch.setattr(factory_module, "BOOT", state)
    return state


@pytest.fixture
def validator():
    return _Recorder()


def _boom():
    """What #349 did: a config shape the backend could not read."""
    raise KeyError(0)


def _create(validator, component="optimization_interface", critical=True):
    return InterfaceFactory(validator)._create_interface(
        component_name=component,
        category="initialization",
        critical=critical,
        title="Optimizer backend failed",
        error_message="Failed to load optimizer configuration",
        config_link="#eos",
        creator_func=_boom,
    )


def test_a_critical_failure_no_longer_ends_the_process(boot, validator):
    """The whole point: this used to re-raise and take the container with it."""
    assert _create(validator) is None


def test_a_critical_failure_is_recorded_as_fatal(boot, validator):
    _create(validator)
    fatal = boot.fatal
    assert fatal["component"] == "optimization_interface"
    assert "KeyError" in fatal["message"] or "0" in fatal["message"]


def test_a_critical_failure_is_still_reported_to_the_user(boot, validator):
    """Surviving must not mean going quiet."""
    _create(validator)
    assert len(validator.errors) == 1
    assert validator.errors[0]["component"] == "optimization_interface"


def test_the_error_asks_for_action_and_links_to_the_setting(boot, validator):
    _create(validator)
    error = validator.errors[0]
    assert error["severity"] == "error"
    assert error["action_required"] is True
    assert error["config_link"] == "#eos"


def test_the_failing_step_is_marked_failed(boot, validator):
    _create(validator)
    step = next(s for s in boot.snapshot()["steps"] if s["id"] == "eos")
    assert step["state"] == "failed"
    assert step["detail"]


def test_a_non_critical_failure_is_not_fatal(boot, validator):
    """Only the four interfaces the application cannot work without stop it."""
    _create(validator, component="mqtt_interface", critical=False)
    assert boot.fatal is None


def test_a_successful_creation_marks_its_step_done(boot, validator):
    InterfaceFactory(validator)._create_interface(
        component_name="price_interface",
        category="connectivity",
        critical=False,
        title="",
        error_message="",
        config_link="#price",
        creator_func=lambda: object(),
    )
    step = next(s for s in boot.snapshot()["steps"] if s["id"] == "price")
    assert step["state"] == "ok"
    assert boot.fatal is None
