"""
How far the application has got, from the moment the web server can answer.

``eos_connect.py`` is a script: everything it needs is built at module level, and
until that finishes there is nothing to serve.  The web server now binds before
that work starts (see ``boot_server.py``), which leaves a window of seconds to
minutes in which the UI is reachable but the application is not ready.  This
module is what the UI shows during that window.

The step catalogue below is declared up front rather than discovered as the run
progresses.  A caller that only appends as it goes cannot answer "step 7 of how
many?", and a progress bar that keeps growing its own denominator reads as a
hang.  Steps that a given configuration never reaches are marked ``skipped``, so
the denominator stays the same for everyone.

Durations come from ``time.monotonic()``: a start can straddle an NTP correction
or the time-zone switch the logger performs a few lines in, and a wall-clock
delta would then report a step as having taken an hour.
"""

import threading
import time
from contextlib import contextmanager

#: Step states. ``failed`` does not by itself stop the boot — only a step that
#: also calls :meth:`BootState.set_fatal` does.
PENDING = "pending"
RUNNING = "running"
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"

#: The ordered milestones of a start, with the label the UI shows.  The order
#: mirrors the module-level sequence in ``eos_connect.py``; the ids are what the
#: ``with BOOT.step(...)`` call sites use.
STEPS = (
    ("config_db", "Configuration database"),
    ("eos", "Optimizer backend"),
    ("base_control", "Control state"),
    ("inverter", "Inverter"),
    ("managed_loads", "Managed loads"),
    ("load", "Load source"),
    ("load_profile", "Load profile"),
    ("battery", "Battery"),
    ("mqtt", "MQTT broker"),
    ("evcc", "EVCC"),
    ("price", "Electricity price"),
    ("feed_in_price", "Feed-in price"),
    ("pv", "PV forecast"),
    ("pv_autoscale", "PV auto-scaling"),
    ("load_sensors", "Managed load sensors"),
    ("settle", "Waiting for interfaces"),
    ("pv_wait", "Waiting for PV forecast"),
    ("battery_price", "Battery price"),
    ("update_checker", "Update check"),
    ("scheduler", "Optimization scheduler"),
    ("routes", "Web interface"),
)

_LABELS = dict(STEPS)


class UnknownBootStep(KeyError):
    """A step id that is not in :data:`STEPS`.

    Raised rather than tolerated: a typo in a ``BOOT.step("mqqt")`` call would
    otherwise sit in the code as a milestone that never reports, and the bar
    would stall at a step the user cannot name.
    """


class BootState:
    """
    Thread-safe record of the current start.

    Written from the main thread as it walks the module-level sequence, read
    from waitress' worker threads answering ``/api/boot/status``.  Every public
    method takes the lock; :meth:`snapshot` returns plain data so a caller never
    holds a reference into the live state.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._started_wall = time.time()
        self._started_mono = time.monotonic()
        self._ready = False
        self._fatal = None
        self._current = None
        self._steps = {
            step_id: {
                "id": step_id,
                "label": label,
                "state": PENDING,
                "duration_ms": None,
                "detail": None,
            }
            for step_id, label in STEPS
        }
        self._running_since = {}

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    @contextmanager
    def step(self, step_id, detail=None):
        """
        Mark ``step_id`` running for the duration of the block.

        On a clean exit the step becomes ``ok``.  On an exception it becomes
        ``failed`` with the exception text as its detail, **and the exception is
        re-raised** — this context manager reports, it does not swallow.  Whether
        a failure is survivable is the caller's call, expressed by catching (or
        not) and by calling :meth:`set_fatal`.
        """
        self.begin(step_id, detail)
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 - reported, then re-raised
            self.fail(step_id, f"{type(exc).__name__}: {exc}")
            raise
        else:
            self.finish(step_id)

    def begin(self, step_id, detail=None):
        """Mark a step running. Use :meth:`step` unless the block cannot nest."""
        with self._lock:
            entry = self._entry(step_id)
            entry["state"] = RUNNING
            entry["detail"] = detail
            self._current = step_id
            self._running_since[step_id] = time.monotonic()

    def finish(self, step_id, detail=None):
        """Mark a running step done."""
        with self._lock:
            entry = self._entry(step_id)
            entry["state"] = OK
            if detail is not None:
                entry["detail"] = detail
            self._stamp(step_id, entry)

    def fail(self, step_id, detail=None):
        """Mark a step failed. Survivable unless :meth:`set_fatal` follows."""
        with self._lock:
            entry = self._entry(step_id)
            entry["state"] = FAILED
            entry["detail"] = detail
            self._stamp(step_id, entry)

    def skip(self, step_id, detail=None):
        """Mark a step as not applicable to this configuration."""
        with self._lock:
            entry = self._entry(step_id)
            entry["state"] = SKIPPED
            entry["detail"] = detail
            self._stamp(step_id, entry)

    def set_ready(self):
        """The application is fully built and the real routes are live."""
        with self._lock:
            self._ready = True
            self._current = None

    def set_fatal(self, component, message):
        """
        Record that the start cannot complete.

        The process stays alive in rescue mode: the web server keeps serving the
        configuration UI and the logs so the user can correct what broke and
        restart.  The first fatal wins — a later one is kept only as extra
        detail, because the first is the one that caused the rest.
        """
        with self._lock:
            if self._fatal is None:
                self._fatal = {"component": component, "message": message, "also": []}
            else:
                self._fatal["also"].append(
                    {"component": component, "message": message}
                )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def ready(self):
        """True once the application is fully built."""
        with self._lock:
            return self._ready

    @property
    def fatal(self):
        """The first unrecoverable failure, or ``None``."""
        with self._lock:
            return None if self._fatal is None else dict(self._fatal)

    def snapshot(self):
        """
        Plain-data view of the start, shaped for ``/api/boot/status``.

        ``done`` counts every step that will not run again, skipped ones
        included, so ``done``/``total`` is a progress fraction the UI can show
        directly without knowing which steps this configuration uses.
        """
        with self._lock:
            steps = [dict(self._steps[step_id]) for step_id, _ in STEPS]
            done = sum(1 for s in steps if s["state"] in (OK, FAILED, SKIPPED))
            return {
                "ready": self._ready,
                "fatal": None if self._fatal is None else dict(self._fatal),
                "current": self._current,
                "current_label": _LABELS.get(self._current),
                "done": done,
                "total": len(steps),
                "started_at": self._started_wall,
                "elapsed_s": round(time.monotonic() - self._started_mono, 1),
                "steps": steps,
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _entry(self, step_id):
        try:
            return self._steps[step_id]
        except KeyError:
            raise UnknownBootStep(step_id) from None

    def _stamp(self, step_id, entry):
        started = self._running_since.pop(step_id, None)
        if started is not None:
            entry["duration_ms"] = int((time.monotonic() - started) * 1000)
        if self._current == step_id:
            self._current = None


#: The one instance the application uses.  A module-level singleton rather than
#: something threaded through two dozen constructors: the boot sequence is a
#: property of the process, and ``eos_connect.py`` writes to it from the top
#: level where there is nothing to thread it through.
BOOT = BootState()
