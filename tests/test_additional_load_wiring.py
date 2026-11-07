"""
Is the Additional Load only taken out of the forecast where something puts it back?

The appliance is handed to the optimizer as a shiftable device and its measured
history leaves the household profile, so the same energy is not counted twice. Only
EOS does the placing: EVopt reads no such device and returns a zero appliance array,
so under the built-in optimizer - the default - the subtraction had nothing on the
other side of it and the appliance's consumption simply disappeared from the forecast.

The arithmetic is covered in ``test_load_interface_managed_subtraction.py`` and the
capability in ``tests/interfaces/test_optimization_interface_managed_loads.py``. What
neither can see is the wiring between them, because eos_connect.py cannot be imported
- doing so starts the whole application - so it is read out of the source, in the
manner of ``test_shutdown_sequence.py``.
"""

import ast
import pathlib

import pytest

SOURCE = pathlib.Path("src/eos_connect.py")


@pytest.fixture(name="tree", scope="module")
def tree_fixture():
    """The parsed module, shared across the file."""
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def _load_interface_call(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_load_interface"
        ):
            return node
    raise AssertionError("the load interface is no longer built here")


def test_the_subtraction_follows_the_optimizer_rather_than_a_default(tree):
    """
    Passed explicitly, both ways round. Forgetting it leaves the flag on its
    conservative default and an EOS install counts the appliance twice; hard-coding it
    true brings back the disappearing energy under EVopt.
    """
    keywords = {kw.arg: ast.unparse(kw.value) for kw in _load_interface_call(tree).keywords}

    assert "places_additional_load" in keywords, "the load interface is not told"
    assert keywords["places_additional_load"] == "eos_interface.places_home_appliance"


def test_a_setting_that_cannot_work_is_said_out_loud(tree):
    """
    The energy is no longer lost, but the shifting the setting promises does not
    happen either, and nothing else on the page would tell anyone that.
    """
    guard = _warning_guard(tree)
    warnings = [
        node for node in ast.walk(guard)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_error"
    ]
    assert len(warnings) == 1, "nothing reports a configured but unplaced appliance"
    assert "Managed Load" in ast.unparse(warnings[0]), "it does not say what to do instead"


def _warning_guard(tree):
    """The `if` that decides whether to warn - the condition is the claim."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and "additional_load_1_sensor" in ast.unparse(node.test)
        ):
            return node
    raise AssertionError("the warning is no longer guarded by the configuration")


def test_the_warning_fires_only_where_the_appliance_is_not_placed(tree):
    guard = ast.unparse(_warning_guard(tree).test)
    assert "not eos_interface.places_home_appliance" in guard
