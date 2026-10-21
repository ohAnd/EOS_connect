"""
``depends_on`` with a negated value, and the three places that must agree on it.

A field can say "I apply unless the governing key holds *this* value". The EVCC
data source needs it: ``evcc.url`` is the address field on the EVCC page, and it
stands down once Data Source already carries the address - one input, not two.

The form is deliberately narrow. ``"!empty"`` already meant "has any value at
all" before this existed, so the parser has to keep treating that one as its own
case rather than as "anything but the literal string 'empty'". And the backend
has to agree with the two frontends that implement the same rule in JavaScript
(``config.js:_isDependencyHidden`` and ``wizard.js``), because the backend is
what decides whether an unmet field is validated on save.
"""

from src.config_web.api import _dependency_met
from src.config_web.schema import ConfigSchema

SCHEMA = ConfigSchema()


def _evcc_url_applies(data_source_type):
    field = SCHEMA.get("evcc.url")
    assert field.depends_on == {"data_source.type": "!evcc"}, field.depends_on
    return _dependency_met(
        field, {"data_source.type": data_source_type}, {}, "evcc.url"
    )


def test_the_field_stands_down_for_the_negated_value():
    assert _evcc_url_applies("evcc") is False


def test_it_applies_for_everything_else():
    for ds_type in ("homeassistant", "openhab", "default", ""):
        assert _evcc_url_applies(ds_type) is True, ds_type


def test_an_unset_governing_key_leaves_the_field_in_place():
    """A fresh install has no data_source.type yet; the EVCC page still works."""
    field = SCHEMA.get("evcc.url")
    assert _dependency_met(field, {}, {}, "evcc.url") is True


def test_not_empty_keeps_its_own_meaning():
    """``!empty`` is "has a value", not "anything but the string 'empty'".

    Both start with "!", so a parser that handled the new form first would have
    read every ``!empty`` gate as satisfied by a blank field - which is the exact
    opposite of what it asks for.
    """
    field = SCHEMA.get("load.additional_load_1_runtime")
    assert field.depends_on == {"load.additional_load_1_sensor": "!empty"}
    key = "load.additional_load_1_runtime"
    assert (
        _dependency_met(field, {"load.additional_load_1_sensor": ""}, {}, key) is False
    )
    assert (
        _dependency_met(
            field, {"load.additional_load_1_sensor": "group:consumer:X"}, {}, key
        )
        is True
    )
    # And the literal string "empty" is just a value like any other.
    assert (
        _dependency_met(field, {"load.additional_load_1_sensor": "empty"}, {}, key)
        is True
    )


def test_the_stored_config_is_consulted_when_the_payload_is_silent():
    """A save that touches only the EVCC page still has to know the data source."""
    field = SCHEMA.get("evcc.url")
    config = {"data_source": {"type": "evcc"}}
    assert _dependency_met(field, {}, config, "evcc.url") is False
    config = {"data_source": {"type": "homeassistant"}}
    assert _dependency_met(field, {}, config, "evcc.url") is True
