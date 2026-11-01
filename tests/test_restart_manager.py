"""
Restarting in place, and the two ways that can go wrong.

The mechanism is ``os.execv``, chosen because exiting and hoping something
restarts us leaves the container dead in two of the three supported run modes
(see ``src/restart.py``).  That makes two things load-bearing, and both are easy
to break without noticing because neither shows up until a real restart on a
real install:

* **the command line has to survive.**  The repository Dockerfile starts the app
  with a relative path and the user may have passed a config directory as
  ``sys.argv[1]``.  Lose either and the new process starts wrong or not at all.
* **the teardown has to be bounded.**  ``optimization_scheduler.shutdown()``
  joins without a timeout, and an optimization run blocked on its backend can
  hold that for three minutes.
"""

# pylint: disable=protected-access,redefined-outer-name

import os
import sys
import threading
from unittest.mock import patch

import pytest

from src.restart import RestartManager, _frozen_argv


@pytest.fixture
def manager():
    return RestartManager(exec_argv=["/usr/bin/python3", "/app/eos_connect.py"])


# ----------------------------------------------------------------------
# The command line
# ----------------------------------------------------------------------


def test_argv_names_the_running_interpreter():
    """``python`` in the Docker image, ``/opt/venv/bin/python`` in the add-on."""
    with patch.object(sys, "argv", ["eos_connect.py"]):
        assert _frozen_argv()[0] == sys.executable


def test_argv_makes_the_script_path_absolute():
    """
    The repository Dockerfile runs ``CMD ["python", "eos_connect.py"]`` with a
    working directory of /app. execv resolves a relative path against the
    working directory, which is not ours to assume by the time we restart.
    """
    with patch.object(sys, "argv", ["eos_connect.py"]):
        assert os.path.isabs(_frozen_argv()[1])
        assert _frozen_argv()[1].endswith("eos_connect.py")


def test_argv_keeps_the_config_directory_argument():
    """eos_connect.py:109 takes an optional config directory as argv[1]."""
    with patch.object(sys, "argv", ["eos_connect.py", "/config/eos"]):
        assert _frozen_argv()[2:] == ["/config/eos"]


def test_argv_is_frozen_at_construction(tmp_path, monkeypatch):
    """A later chdir must not change where the restart looks for the script."""
    monkeypatch.chdir(tmp_path)
    with patch.object(sys, "argv", ["eos_connect.py"]):
        frozen = RestartManager()
    before = frozen.argv

    nested = tmp_path / "elsewhere"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert frozen.argv == before


def test_argv_is_a_copy(manager):
    """A caller mutating what it got back must not arm a different command."""
    manager.argv.append("--inject")
    assert "--inject" not in manager.argv


# ----------------------------------------------------------------------
# Requesting
# ----------------------------------------------------------------------


def test_first_request_is_accepted(manager):
    assert manager.request("settings changed", "web ui") is True
    assert manager.requested is True
    assert manager.details == {"reason": "settings changed", "source": "web ui"}


def test_a_second_request_is_refused(manager):
    """The endpoint turns this into a 409 rather than tearing down twice."""
    manager.request("first", "web ui")
    assert manager.request("second", "web ui") is False
    assert manager.details["reason"] == "first"


def test_requesting_wakes_the_main_thread(manager):
    """eos_connect.py parks the main thread on an Event; this is what frees it."""
    woken = threading.Event()
    manager.on_request = woken.set
    manager.request("settings changed", "web ui")
    assert woken.is_set()


def test_nothing_is_requested_initially(manager):
    assert manager.requested is False
    assert manager.details is None


# ----------------------------------------------------------------------
# The bounded teardown
# ----------------------------------------------------------------------


def test_teardown_runs_before_the_restart(manager):
    ran = []
    with patch("src.restart.os.execv") as execv:
        manager.run(lambda: ran.append("teardown"), deadline_s=5)
    assert ran == ["teardown"]
    execv.assert_called_once()


def test_a_hanging_teardown_does_not_block_the_restart(manager):
    """
    An optimization run waiting on its backend holds the scheduler's unbounded
    join for up to three minutes. Someone who pressed a button must not wait it
    out - the stuck thread disappears with the process image anyway.
    """
    release = threading.Event()
    try:
        with patch("src.restart.os.execv") as execv:
            manager.run(lambda: release.wait(30), deadline_s=0.2)
        execv.assert_called_once()
    finally:
        release.set()


def test_a_failing_teardown_does_not_block_the_restart(manager):
    def _boom():
        raise RuntimeError("inverter went away mid-shutdown")

    with patch("src.restart.os.execv") as execv:
        manager.run(_boom, deadline_s=5)
    execv.assert_called_once()


# ----------------------------------------------------------------------
# The exec itself
# ----------------------------------------------------------------------


def test_exec_replaces_the_process_with_the_frozen_command(manager):
    with patch("src.restart.os.execv") as execv:
        manager.exec_replace()
    execv.assert_called_once_with(
        "/usr/bin/python3", ["/usr/bin/python3", "/app/eos_connect.py"]
    )


def test_a_failed_exec_exits_non_zero(manager):
    """
    execv only returns by failing, and there is nothing left to run. Exiting
    loudly at least gives a restart policy, where one exists, something to act on.
    """
    with patch("src.restart.os.execv", side_effect=OSError("no such file")):
        with pytest.raises(SystemExit) as exit_info:
            manager.exec_replace()
    assert exit_info.value.code == 1
