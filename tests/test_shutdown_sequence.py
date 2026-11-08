"""
Does the teardown call methods that actually exist?

``_shutdown_all()`` in eos_connect.py runs on every way out of the process, and
it is the one piece of the application that nothing else exercises: it only runs
when the process is already on its way down, where an ``AttributeError`` is
logged and then lost in the exit.  That is how ``feed_in_price_interface`` came
to be missing from the sequence for as long as it was, and how re-adding it
reached a live run calling ``shutdown()`` on an object whose method is
``stop()``.

eos_connect.py cannot be imported — doing so starts the whole application — so
the sequence is read out of the source instead and checked against the real
classes.
"""

import ast
import pathlib

import pytest

from src.config_web import ConfigWebModule
from src.interfaces.base_control import BaseControl
from src.interfaces.battery_interface import BatteryInterface
from src.interfaces.evcc_interface import EvccInterface
from src.interfaces.feed_in_price_interface import FeedInPriceInterface
from src.interfaces.mqtt_interface import MqttInterface
from src.interfaces.port_interface import _WaitressServerAdapter
from src.interfaces.price_interface import PriceInterface
from src.interfaces.pv_autoscaler import PvAutoscaler
from src.interfaces.pv_interface import PvInterface
from src.interfaces.update_checker import UpdateChecker
from src.loads.manager import ManagedLoadManager

SOURCE = pathlib.Path("src/eos_connect.py")

#: The module-level name each component is bound to, and the class behind it.
#: ``optimization_scheduler`` and ``inverter_interface`` are left out on purpose:
#: the first is a class defined inside eos_connect.py itself, the second is one
#: of several inverter types chosen at runtime.
COMPONENTS = {
    "base_control": BaseControl,
    "load_manager": ManagedLoadManager,
    "http_server": _WaitressServerAdapter,
    "pv_interface": PvInterface,
    "pv_autoscaler": PvAutoscaler,
    "price_interface": PriceInterface,
    "feed_in_price_interface": FeedInPriceInterface,
    "mqtt_interface": MqttInterface,
    "evcc_interface": EvccInterface,
    "battery_interface": BatteryInterface,
    "update_checker": UpdateChecker,
    "config_web": ConfigWebModule,
}


def _shutdown_calls():
    """Every ``name.method()`` call inside ``_shutdown_all``, as (name, method)."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_shutdown_all"
    )
    return {
        (node.func.value.id, node.func.attr)
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }


CALLS = sorted(_shutdown_calls())


def test_the_sequence_was_found_at_all():
    """Guard against the parser silently matching nothing after a rename."""
    assert len(CALLS) >= 10


@pytest.mark.parametrize("name,method", CALLS, ids=lambda v: str(v))
def test_shutdown_calls_a_method_that_exists(name, method):
    cls = COMPONENTS.get(name)
    if cls is None:
        pytest.skip(f"{name} is not a directly importable component")
    assert hasattr(cls, method), (
        f"_shutdown_all() calls {name}.{method}(), but "
        f"{cls.__name__} has no such method"
    )


@pytest.mark.parametrize("name", sorted(COMPONENTS))
def test_every_component_is_shut_down(name):
    """
    Each component that owns a thread or a file handle has to be stopped.

    Adding one and forgetting it here is the omission this file exists to stop;
    it stayed invisible for so long only because every worker thread is a
    daemon and the process was exiting anyway.
    """
    assert any(called == name for called, _ in CALLS), (
        f"{name} is never stopped by _shutdown_all()"
    )


class TestDisabledComponentsStillShutDown:
    """
    Shutting down something that was never started must not abort the teardown.

    ``_shutdown_all`` runs the components in one sequence, so the first one that
    raises takes every component after it down with it — the database close is
    last, which makes this the difference between a clean stop and a half-written
    store.
    """

    def test_mqtt_shutdown_is_safe_when_disabled(self):
        """
        ``MqttInterface.__init__`` returns before creating ``self.client`` when
        MQTT is off, so ``shutdown()`` used to raise AttributeError — and with
        MQTT disabled being the default, that was most installations.
        """
        from src.interfaces.mqtt_interface import (  # noqa: PLC0415
            MqttInterface,
        )

        interface = MqttInterface(
            config_mqtt={"enabled": False}, on_mqtt_command=None
        )
        interface.shutdown()  # must not raise
