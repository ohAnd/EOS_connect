"""
What the boot progress reports while the application is still building itself.

The web server now answers before ``eos_connect.py`` has finished its module-level
sequence, so ``/api/boot/status`` is the only thing standing between the user and a
blank spinner.  These tests pin the two properties that make it readable: the
denominator never moves, and a step that fails says so instead of vanishing.
"""

# pylint: disable=protected-access

import threading

import pytest

from src.boot_state import (
    BootState,
    FAILED,
    OK,
    PENDING,
    RUNNING,
    SKIPPED,
    STEPS,
    UnknownBootStep,
)


@pytest.fixture
def boot():
    """A fresh state — the module singleton is shared and would leak between tests."""
    return BootState()


def test_starts_with_every_step_pending(boot):
    snap = boot.snapshot()
    assert snap["total"] == len(STEPS)
    assert snap["done"] == 0
    assert snap["ready"] is False
    assert snap["fatal"] is None
    assert {s["state"] for s in snap["steps"]} == {PENDING}


def test_steps_keep_the_declared_order(boot):
    assert [s["id"] for s in boot.snapshot()["steps"]] == [sid for sid, _ in STEPS]


def test_step_context_marks_running_then_ok(boot):
    with boot.step("mqtt"):
        running = boot.snapshot()
        assert running["current"] == "mqtt"
        assert running["current_label"] == "MQTT broker"
        assert _state(running, "mqtt") == RUNNING

    done = boot.snapshot()
    assert _state(done, "mqtt") == OK
    assert done["current"] is None
    assert done["done"] == 1


def test_step_context_records_failure_and_reraises(boot):
    with pytest.raises(ValueError):
        with boot.step("eos"):
            raise ValueError("broker said no")

    entry = _entry(boot.snapshot(), "eos")
    assert entry["state"] == FAILED
    assert "ValueError: broker said no" == entry["detail"]


def test_failure_alone_is_not_fatal(boot):
    """A failed step degrades; only set_fatal stops the boot."""
    boot.fail("price", "no token")
    assert boot.snapshot()["fatal"] is None


def test_skipped_counts_towards_done_so_the_denominator_holds(boot):
    """A configuration that never reaches a step must still reach 100%."""
    for step_id, _ in STEPS:
        boot.skip(step_id, "not configured")

    snap = boot.snapshot()
    assert snap["done"] == snap["total"]
    assert {s["state"] for s in snap["steps"]} == {SKIPPED}


def test_duration_is_recorded_for_finished_steps(boot):
    with boot.step("load"):
        pass
    entry = _entry(boot.snapshot(), "load")
    assert isinstance(entry["duration_ms"], int)
    assert entry["duration_ms"] >= 0


def test_pending_step_has_no_duration(boot):
    assert _entry(boot.snapshot(), "load")["duration_ms"] is None


def test_first_fatal_wins_and_later_ones_are_kept_as_context(boot):
    boot.set_fatal("optimization_interface", "KeyError: 0")
    boot.set_fatal("battery_interface", "follow-on damage")

    fatal = boot.snapshot()["fatal"]
    assert fatal["component"] == "optimization_interface"
    assert fatal["message"] == "KeyError: 0"
    assert fatal["also"] == [
        {"component": "battery_interface", "message": "follow-on damage"}
    ]


def test_set_ready_clears_the_current_step(boot):
    boot.begin("routes")
    boot.set_ready()
    snap = boot.snapshot()
    assert snap["ready"] is True
    assert snap["current"] is None


def test_unknown_step_id_is_rejected(boot):
    """A typo must not become a milestone that silently never reports."""
    with pytest.raises(UnknownBootStep):
        boot.begin("mqqt")


def test_snapshot_does_not_alias_live_state(boot):
    """Callers get plain data; mutating it must not corrupt the real state."""
    snap = boot.snapshot()
    snap["steps"][0]["state"] = "tampered"
    snap["ready"] = True
    assert _state(boot.snapshot(), STEPS[0][0]) == PENDING
    assert boot.ready is False


def test_concurrent_writers_do_not_lose_steps(boot):
    """waitress answers /api/boot/status from worker threads while main writes."""
    ids = [step_id for step_id, _ in STEPS]
    errors = []

    def worker(step_id):
        try:
            with boot.step(step_id):
                boot.snapshot()
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert boot.snapshot()["done"] == len(ids)


def _entry(snapshot, step_id):
    return next(s for s in snapshot["steps"] if s["id"] == step_id)


def _state(snapshot, step_id):
    return _entry(snapshot, step_id)["state"]
