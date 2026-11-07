"""
Does the forecast band the chart draws actually get produced?

Issue #362: a managed load of a type the solver never places - a pushed or pulled
profile - was inside ``gesamtlast`` with nothing on the chart to say so. The fix gives
the chart its own series for those loads and takes the same energy back out of the Load
bar, which only works if three things stay true at once:

- the request builder asks for the split (``apply(..., per_load=True)``);
- the split is discounted for the partial slot exactly as ``gesamtlast`` is, or the two
  disagree in the current slot and the Load bar goes to zero under a band that is too
  tall;
- the key Python writes is the key JavaScript reads.

eos_connect.py cannot be imported - doing so starts the whole application - so the
first two are read out of the source, in the manner of ``test_shutdown_sequence.py``.
The third is a string on both sides of a language boundary, with nothing but this test
between them.
"""

import ast
import pathlib

import pytest

SOURCE = pathlib.Path("src/eos_connect.py")
CHART = pathlib.Path("src/web/js/chart.js")

#: The response key carrying ``{id: [Wh per slot]}`` for the loads that are part of the
#: household forecast rather than something the optimizer placed.
KEY = "managed_load_forecast"


@pytest.fixture(name="tree", scope="module")
def tree_fixture():
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from {SOURCE}")


def test_the_request_builder_asks_for_the_split(tree):
    calls = [
        node for node in ast.walk(_function(tree, "get_ems_data"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "apply"
    ]
    assert len(calls) == 1, "managed loads join the profile in exactly one place"
    assert [kw.arg for kw in calls[0].keywords].count("per_load") == 1
    assert calls[0].keywords[-1].value.value is True


def _partial_slot_branch(tree):
    """The branch that discounts the in-progress slot, found by what it tests."""
    for node in ast.walk(_function(tree, "get_ems_data")):
        if isinstance(node, ast.If) and "evopt" in ast.unparse(node.test):
            return node
    raise AssertionError("the partial-slot branch is gone")


def test_the_contributions_are_discounted_with_the_profile_they_are_in(tree):
    """
    Both or neither. A band built from undiscounted contributions is taller than the
    slot it is subtracted from, and the Load bar underneath it clamps to zero.
    """
    scalings = [
        node for node in ast.walk(_partial_slot_branch(tree))
        if isinstance(node, ast.AugAssign)
        and isinstance(node.op, ast.Mult)
        and ast.unparse(node.value) == "scale_factor"
    ]
    assert len(scalings) == 2, "the base load is scaled; the contributions must be too"
    targets = {ast.unparse(node.target) for node in scalings}
    assert len(targets) == 2, f"both scalings write the same series: {targets}"


def test_the_split_is_published_after_the_branch_has_had_it(tree):
    """
    Ordering, because it is invisible when wrong: publishing the dict before the
    discount would hand the chart the undiscounted copy on every run.
    """
    body = _function(tree, "get_ems_data").body
    branch = body.index(_partial_slot_branch(tree))
    published = [
        index for index, node in enumerate(body)
        if isinstance(node, ast.Assign) and "last_managed_forecast" in ast.unparse(node)
    ]
    assert published, "nothing publishes the split"
    assert min(published) > branch


def test_the_response_carries_the_split_to_the_chart(tree):
    assigned = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and KEY in ast.unparse(node)
    ]
    assert any(
        f'optimized_response[\'{KEY}\']' in line for line in assigned
    ), f"nothing puts {KEY} on the response"


def test_the_chart_reads_the_key_python_writes():
    """A rename on one side of this boundary is a silently empty band on the other."""
    assert f'"{KEY}"' in CHART.read_text(encoding="utf-8")
