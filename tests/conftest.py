"""
Test staging, and a session-wide safety net against leaked battery threads.

Staging
-------
The suite is cheap except for the browser tests: 604 of 3665 tests drive Chromium
and account for about nine tenths of the wall clock. They are marked ``browser``
here so the two halves can run as separate CI jobs, and so the default local run is
the fast one. ``pytest.ini`` sets ``-m "not browser"``; ``-m browser`` and ``-m ""``
on the command line override it.

Session-wide safety net: no test may leave a battery update thread running.

``BatteryInterface.__init__`` starts a background thread that polls the SOC, the
temperature and the price sensor every 30 seconds. Tests create the interface ~40
times and only a third of them call ``shutdown()``, so the rest kept polling for the
remainder of the run - against whatever ``requests.get`` the *currently* running test
happened to have patched in.

That is how a crash in the battery interface surfaced as three
``PytestUnhandledThreadExceptionWarning``s pinned on an unrelated load-interface test:
a leaked thread asked that test's fake Home Assistant for ``/api/states/<soc sensor>``,
got a payload with no ``state`` key, and died. The interface has been fixed, but the
leak is worth closing on its own - it makes test failures land on the test that caused
them.
"""

from unittest.mock import patch

import pytest

from src.interfaces.battery_interface import BatteryInterface

# Marked by path rather than by fixture name. The obvious-looking alternative --
# "everything that requests ``page``" -- is wrong: tests/docs/test_docs_integrity.py
# parametrises over a ``page`` argument that is a ``pathlib.Path``, and parametrised
# arguments show up in ``item.fixturenames``. That would push 136 static tests that
# run in 0.06s into the browser job.
BROWSER_PATHS = ("tests/web/", "tests/docs/test_docs_rendering.py")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):  # pylint: disable=unused-argument
    """
    Mark the browser-driven tests so ``-m`` can select or skip them as a group.

    ``tryfirst`` is load-bearing: ``_pytest.mark`` does its ``-m`` deselection in this
    same hook and carries no ordering of its own, so without it whether the marks are
    applied before they are filtered on comes down to plugin registration order.
    """
    for item in items:
        # Only the file part of the nodeid. The whole of it ends in the parametrise
        # id, and tests/test_staging_markers.py parametrises over BROWSER_PATHS
        # itself -- matching against that marked the guard as a browser test and
        # deselected it from the lane it exists to protect.
        module = item.nodeid.split("::", 1)[0]
        if any(path in module for path in BROWSER_PATHS):
            item.add_marker("browser")


def pytest_configure(config):
    """
    Decide here, before collection, whether one Chromium may serve the whole run.

    It has to be settled this early: pytest resolves a fixture's scope while it builds
    each item's fixture closure, which is part of collection. Deciding it afterwards
    from the deselected item list -- the obvious reading -- is too late, and the
    fixture silently stays function-scoped.

    The ``-m`` expression is a sound proxy. ``pytest.ini`` excludes ``browser`` by
    default, so asking for the browser tests at all means passing a ``-m``, and the
    only expression that selects them *and nothing else* is exactly ``browser``.
    Anything more elaborate falls back to a browser per test, which is always correct,
    only slower.
    """
    config._browser_only = (  # pylint: disable=protected-access
        config.getoption("-m", default="") or ""
    ).strip() == "browser"


def browser_scope(*, fixture_name, config):  # pylint: disable=unused-argument
    """
    Share one Chromium across the session, but only when it is safe to.

    An open ``sync_playwright()`` holds an event loop in this thread, and
    ``src/interfaces/pv_interface.py`` calls ``asyncio.run()``, which refuses to run
    inside one -- so a session-wide browser breaks the PV tests whenever they share a
    process with it. They never do in the browser CI jobs, which is what makes the
    sharing safe there; a full local run (``-m ""``) falls back to one browser per
    test.
    """
    return "session" if getattr(config, "_browser_only", False) else "function"


@pytest.fixture(autouse=True)
def no_leaked_battery_threads():
    """Shut down every battery update thread a test started."""
    started = []
    original = BatteryInterface.start_update_service

    def remember(self):
        # The loop restarts itself on a set stop event, so the same instance can
        # register more than once; shutting it down twice is a no-op.
        started.append(self)
        return original(self)

    with patch.object(BatteryInterface, "start_update_service", remember):
        yield

    for interface in started:
        interface.shutdown()
