"""
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
