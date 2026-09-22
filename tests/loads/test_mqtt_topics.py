"""
The MQTT surface: which entities each managed load gets, and what the commands do.

No broker involved - `build_topics` returns plain dicts and the handlers are closures
over the manager, which is the whole reason the mapping lives outside the interface.
"""

import json

import pytest

from src.loads import mqtt_topics
from src.loads.mqtt_topics import PREFIX, TOTAL_TOPIC, build_topics, build_values
from src.loads.presets import (
    TYPE_EXTERNAL_CONTINGENT,
    TYPE_EXTERNAL_PROFILE,
    TYPE_POOL_HEATPUMP,
)

POOL = {
    "id": "pool", "type": TYPE_POOL_HEATPUMP,
    "temp_sensor": "sensor.pool", "power_sensor": "sensor.pool_power",
    "window_start": None, "window_end": None, "season_start": None,
    "season_end": None, "min_ambient_temp_c": None,
}
HEATING = {"id": "heating", "type": TYPE_EXTERNAL_PROFILE}
BUDGET = {"id": "budget", "type": TYPE_EXTERNAL_CONTINGENT, "rated_power_w": 2000}


@pytest.fixture(name="manager")
def manager_fixture(make_manager, installation):
    installation.sensors["sensor.pool"] = 24.0
    installation.sensors["sensor.pool_power"] = 0.0
    return make_manager([POOL, HEATING, BUDGET])


# --- topic layout ----------------------------------------------------------------------

def test_a_gated_load_gets_a_release_binary_sensor(manager):
    topics = build_topics(manager)
    entry = topics[f"{PREFIX}/pool/released"]
    assert entry["type"] == "binary_sensor"
    assert entry["name"] == "Pool Released"
    assert "ON" in entry["value_template"]


def test_a_pushed_profile_gets_no_release_topics(manager):
    """Nothing to gate: the sender already decided when the energy is drawn."""
    topics = build_topics(manager)
    assert f"{PREFIX}/heating/released" not in topics
    assert f"{PREFIX}/heating/energy_needed_wh" in topics


def test_only_thermal_loads_get_temperature_topics(manager):
    topics = build_topics(manager)
    assert f"{PREFIX}/pool/temperature" in topics
    assert f"{PREFIX}/pool/calibration_confidence" in topics
    assert f"{PREFIX}/heating/temperature" not in topics


def test_only_external_loads_get_a_push_command(manager):
    topics = build_topics(manager)
    assert topics[f"{PREFIX}/heating"]["command_topic"] == f"{PREFIX}/heating/set"
    assert f"{PREFIX}/pool" not in topics


def test_a_push_topic_accepts_retained_messages(manager):
    """A pushed forecast is not a config value - losing it on reconnect costs hours."""
    assert build_topics(manager)[f"{PREFIX}/heating"]["accept_retained"] is True


def test_the_override_topic_does_not_accept_retained_messages(manager):
    """A retained override would silently re-apply itself on every reconnect."""
    assert build_topics(manager)[f"{PREFIX}/pool/override"].get("accept_retained") is not True


def test_every_topic_carries_what_discovery_needs(manager):
    """`__publish_discovery_for` indexes these with [] - a missing key is a KeyError."""
    for topic, entry in build_topics(manager).items():
        for key in ("name", "type", "unit", "device_class"):
            assert key in entry or key in ("unit", "device_class"), f"{topic} lacks {key}"
        assert entry["name"], topic
        assert entry["type"], topic


def test_the_summed_contribution_is_published(manager):
    assert TOTAL_TOPIC in build_topics(manager)


# --- values ------------------------------------------------------------------------------

def test_values_report_the_release_state(manager):
    manager.run_cycle()
    values = build_values(manager)
    assert values[f"{PREFIX}/pool/released"]["value"] == "true"
    assert values[f"{PREFIX}/pool/state"]["value"] == "released"
    assert values[f"{PREFIX}/pool/temperature"]["value"] == 24.0
    assert values[f"{PREFIX}/pool/target_temperature"]["value"] == 28.0


def test_calibration_confidence_is_reported_as_a_percentage(manager):
    manager.run_cycle()
    value = build_values(manager)[f"{PREFIX}/pool/calibration_confidence"]["value"]
    assert 0.0 <= value <= 100.0


def test_a_load_that_has_not_run_yet_publishes_nothing_misleading(manager):
    """A zero would read as "no demand" rather than "not computed"."""
    values = build_values(manager)
    assert f"{PREFIX}/pool/released" not in values
    assert f"{PREFIX}/pool/energy_needed_wh" not in values


def test_the_total_tracks_the_contributions(manager):
    manager.push("heating", {"value_wh": 500})
    manager.run_cycle()
    assert build_values(manager)[TOTAL_TOPIC]["value"] > 0


# --- push command ---------------------------------------------------------------------------

def _handler(manager, topic):
    return build_topics(manager)[topic]["on_command"]


def test_publishing_a_json_object_pushes_a_forecast(manager):
    _handler(manager, f"{PREFIX}/heating")(json.dumps({"values": [1000.0] * 24}))
    manager.run_cycle()
    assert manager.apply([100.0] * 48)[0] == pytest.approx(1100.0)


def test_publishing_a_bare_number_pushes_an_hourly_average(manager):
    _handler(manager, f"{PREFIX}/heating")("750")
    manager.run_cycle()
    assert manager.apply([0.0] * 48)[0] == pytest.approx(750.0)


def test_a_broken_push_is_logged_rather_than_raised(manager, caplog):
    """A published message has nowhere to return an error to."""
    with caplog.at_level("ERROR", logger="__main__"):
        _handler(manager, f"{PREFIX}/heating")("not json at all")
    assert any("not valid JSON" in r.getMessage() for r in caplog.records)


def test_a_refused_push_is_logged(manager, caplog):
    with caplog.at_level("ERROR", logger="__main__"):
        _handler(manager, f"{PREFIX}/heating")(json.dumps({"values": [1.0] * 7}))
    assert any("refused" in r.getMessage() for r in caplog.records)


def test_a_none_payload_is_ignored(manager):
    _handler(manager, f"{PREFIX}/heating")(None)
    manager.run_cycle()
    assert "heating" not in [c["id"] for c in manager.registry.snapshot()]


# --- override command -----------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ("release", "release"),
    ("BLOCK", "block"),
    ('{"mode": "release", "minutes": 30}', "release"),
])
def test_an_override_can_be_published(manager, payload, expected):
    _handler(manager, f"{PREFIX}/pool/override")(payload)
    assert manager.instance("pool").gate.status()["override"] == expected


def test_an_override_can_be_cleared_by_publishing(manager):
    _handler(manager, f"{PREFIX}/pool/override")("block")
    _handler(manager, f"{PREFIX}/pool/override")("clear")
    assert manager.instance("pool").gate.status()["override"] is None


@pytest.mark.parametrize("payload", ["maybe", "{bad json", '{"mode": "release", "minutes": "soon"}'])
def test_an_unusable_override_is_logged_not_applied(manager, payload, caplog):
    with caplog.at_level("ERROR", logger="__main__"):
        _handler(manager, f"{PREFIX}/pool/override")(payload)
    assert any("not understood" in r.getMessage() for r in caplog.records)
    assert manager.instance("pool").gate.status()["override"] is None


def test_override_minutes_are_clamped(manager):
    _handler(manager, f"{PREFIX}/pool/override")('{"mode": "release", "minutes": 99999}')
    assert manager.instance("pool").gate.status()["override"] == "release"


def test_parse_override_rejects_a_json_array():
    assert mqtt_topics._parse_override("[1,2,3]") == (False, 0)  # pylint: disable=protected-access


# --- registering against the interface -------------------------------------------------

def test_registering_topics_is_a_no_op_when_mqtt_is_disabled():
    """
    With MQTT off, ``MqttInterface.__init__`` returns before the registry is built.

    The caller registers unconditionally on purpose - whether MQTT is enabled is the
    interface's business - so this has to be safe rather than an AttributeError at
    start-up, which is exactly how it failed the first time it was run.
    """
    from src.interfaces.mqtt_interface import MqttInterface

    interface = MqttInterface(config_mqtt={"enabled": False}, on_mqtt_command=None)
    assert interface.register_topics({"managed_load/pool/released": {"name": "x"}}) == {}


class _RecordingClient:
    """Stand-in for the paho client - connecting is not what this tests."""

    def __init__(self):
        self.subscribed = []
        self.published = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))


def _bare_interface():
    from src.interfaces.mqtt_interface import MqttInterface

    interface = MqttInterface.__new__(MqttInterface)
    interface.enable_mqtt = True
    interface.ha_auto_discovery = False
    interface.base_topic = "eos_connect"
    interface.topics_publish = {}
    interface.topics_publish_last = {}
    interface.client = _RecordingClient()
    return interface


def test_registered_topics_reach_the_registry_and_are_not_overwritten(manager):
    from src.interfaces.mqtt_interface import MqttInterface

    interface = _bare_interface()
    topics = build_topics(manager)

    added = MqttInterface.register_topics(interface, topics)
    assert set(added) == set(topics)
    assert interface.topics_publish_last.keys() == interface.topics_publish.keys()

    again = MqttInterface.register_topics(interface, topics)
    assert again == {}


def test_command_topics_are_subscribed_on_registration(manager):
    """Subscription follows from the registry entry, as it does for built-in topics."""
    from src.interfaces.mqtt_interface import MqttInterface

    interface = _bare_interface()
    MqttInterface.register_topics(interface, build_topics(manager))

    assert "eos_connect/managed_load/heating/set" in interface.client.subscribed
    assert "eos_connect/managed_load/pool/override/set" in interface.client.subscribed


def test_registered_values_publish_through_the_normal_path(manager):
    """`update_publish_topics` has to accept the new topics like any other."""
    from src.interfaces.mqtt_interface import MqttInterface

    interface = _bare_interface()
    MqttInterface.register_topics(interface, build_topics(manager))
    manager.run_cycle()

    MqttInterface.update_publish_topics(interface, build_values(manager))
    assert interface.topics_publish[f"{PREFIX}/pool/released"]["value"] == "true"

    # And a change is actually put on the wire, through the same diffing path the
    # built-in topics use.
    published = dict(interface.client.published)
    assert published["eos_connect/managed_load/pool/released"] == "true"
