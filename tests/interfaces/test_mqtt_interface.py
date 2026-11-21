"""
Tests for the MQTT topic registry when it grows while it is being walked.

``MqttInterface`` starts the paho network thread in ``__init__``. When the broker
accepts the connection, ``__on_connect`` runs on that thread and walks the whole
registry to subscribe and send the discovery messages. The managed-load topics are
added later by ``register_topics`` on the main thread, so the two can overlap. Each
test below lets ``register_topics`` land in the middle of one of those walks, the
way the main thread does when the timing is unlucky.
"""

import threading

from src.interfaces.mqtt_interface import MqttInterface

LATE_TOPICS = {
    "managed_load/pool/released": {"name": "Pool Released"},
    "managed_load/pool/set": {
        "name": "Pool Set",
        "command_topic": "managed_load/pool/set/set",
    },
}


class _InterleavingClient:
    """
    Stand-in for the paho client that runs the queued callbacks from inside the
    first ``subscribe`` or ``publish`` - the point where the registry is being walked.
    """

    def __init__(self, hook_on):
        self.hook_on = hook_on
        self.interleave = []
        self.subscribed = []
        self.published = []

    def _run_interleaved(self, method):
        if method == self.hook_on:
            pending, self.interleave = self.interleave, []
            for callback in pending:
                callback()

    def subscribe(self, topic, qos=0):
        """Record the subscription, then let the other thread in."""
        self.subscribed.append((topic, qos))
        self._run_interleaved("subscribe")

    def publish(self, topic, payload, qos=0, retain=False):
        """Record the message, then let the other thread in."""
        self.published.append((topic, payload, qos, retain))
        self._run_interleaved("publish")

    def is_connected(self):
        """Discovery messages are only sent while connected."""
        return True

    def subscribed_topics(self):
        """Topics subscribed so far."""
        return [topic for topic, _ in self.subscribed]

    def published_payloads(self):
        """Last payload per topic."""
        return {topic: payload for topic, payload, _, _ in self.published}


def _builtin(name, item_type, command_topic, unit=None):
    return {
        "name": name,
        "type": item_type,
        "unit": unit,
        "device_class": None,
        "command_topic": command_topic,
        "value": None,
        "qos": 0,
        "retain": True,
    }


def _interface(hook_on, ha_auto_discovery=False):
    """An interface with two built-in topics and no broker behind it."""
    interface = MqttInterface.__new__(MqttInterface)
    interface.enable_mqtt = True
    interface._registry_lock = threading.Lock()  # pylint: disable=protected-access
    interface.ha_auto_discovery = ha_auto_discovery
    interface.auto_discover_topic = "homeassistant"
    interface.base_topic = "eos_connect"
    interface.broker = "localhost"
    interface.port = 1883
    interface.topics_publish = {
        "control/overall_state": _builtin(
            "Current State", "select", "control/overall_state/set"
        ),
        "battery/soc_max": _builtin(
            "Battery max SOC", "number", "battery/soc_max/set", unit="%"
        ),
    }
    interface.topics_publish_last = {
        topic: entry.copy() for topic, entry in interface.topics_publish.items()
    }
    interface.client = _InterleavingClient(hook_on)
    interface.client.interleave.append(lambda: interface.register_topics(LATE_TOPICS))
    return interface


def _on_connect(interface):
    interface._MqttInterface__on_connect(  # pylint: disable=protected-access
        interface.client, None, {}, 0
    )


def test_registering_while_on_connect_subscribes_keeps_the_network_thread_alive():
    """
    The case from the field: the main thread registered the managed-load topics while
    ``__on_connect`` was subscribing. Before the fix this raised
    ``RuntimeError: dictionary changed size during iteration`` on the paho thread,
    which ended it - no message was received or sent until the next restart.
    """
    interface = _interface(hook_on="subscribe")

    _on_connect(interface)

    # Nothing is lost: the built-in command topics come from __on_connect, the late
    # one from register_topics itself.
    subscribed = interface.client.subscribed_topics()
    assert "eos_connect/control/overall_state/set" in subscribed
    assert "eos_connect/battery/soc_max/set" in subscribed
    assert "eos_connect/managed_load/pool/set/set" in subscribed
    assert set(LATE_TOPICS) <= set(interface.topics_publish)


def test_registering_while_on_connect_sends_discovery_keeps_the_network_thread_alive():
    """The same overlap during the discovery pass that follows the subscriptions."""
    interface = _interface(hook_on="publish", ha_auto_discovery=True)

    _on_connect(interface)

    sent = list(interface.client.published_payloads())
    for unique_id in (
        "eos_connect_control_overall_state",
        "eos_connect_battery_soc_max",
        "eos_connect_managed_load_pool_released",
        "eos_connect_managed_load_pool_set",
    ):
        assert any(unique_id in topic for topic in sent), unique_id


def test_registering_while_publishing_changes_does_not_break_the_publish():
    """``__publish_topics_on_change`` walks the same registry from the scheduler."""
    interface = _interface(hook_on="publish")
    # The other thread really is another thread here: it has to wait for the lock
    # the publish pass holds, instead of running inside it.
    late = threading.Thread(target=interface.register_topics, args=(LATE_TOPICS,))
    interface.client.interleave = [late.start]

    interface.update_publish_topics(
        {"control/overall_state": {"value": "1"}, "battery/soc_max": {"value": 95}}
    )

    published = interface.client.published_payloads()
    assert published["eos_connect/control/overall_state"] == "1"
    assert published["eos_connect/battery/soc_max"] == 95
    late.join(timeout=5)
    assert not late.is_alive()
    assert set(LATE_TOPICS) <= set(interface.topics_publish)
