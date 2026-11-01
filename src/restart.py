"""
Restarting EOS Connect from the web interface.

Sixty-six settings are marked ``restart_required``, the UI has shown a banner
about them for a long time, and until now the only way to act on it was to log
into the host (issue #336).

**The restart replaces this process in place, with ``os.execv``.**  The obvious
alternative — exit and let something restart us — fails on two of the three ways
EOS Connect is run:

===================  ========================  ==================================
Run mode             PID 1                     On process exit
===================  ========================  ==================================
``docker compose``   python                    ``restart: unless-stopped`` → back
``docker run``       python                    **stays dead** (documented with
                                               no ``--restart`` flag)
HA add-on            s6-overlay ``/init``      **stays dead** — s6 tears the
                                               container down, and no add-on
                                               manifest declares ``watchdog:``
===================  ========================  ==================================

Calling the Supervisor API instead was ruled out for the same reason it would
not help: it does nothing for Docker, ``SUPERVISOR_TOKEN`` is never read, and the
add-on manifests grant neither ``hassio_api`` nor ``hassio_role`` — that would be
a change in a second repository.

``execv`` sidesteps all of it.  The process image is replaced, the PID does not
change, the container never exits and s6 never sees a service stop.  One
mechanism, identical in all three run modes.  It is also how you verify a restart
actually happened: the log shows the startup banner again while ``docker ps``
reports unchanged container uptime.

Two things make it safe:

* the command line is frozen at import, before anything can ``chdir`` — the
  repository's Dockerfile starts the app with a *relative* path
  (``CMD ["python", "eos_connect.py"]``), and ``sys.argv[1]`` is a config
  directory the user may have passed;
* the teardown is given a deadline.  ``optimization_scheduler.shutdown()`` joins
  its threads without one, and a run blocked in an EOS request can sit there for
  the backend's full three-minute timeout.  Waiting that long after someone
  pressed a button reads as a hang, so the teardown runs in a worker and the
  deadline wins.  Nothing leaks by cutting it short: ``execv`` replaces the whole
  process image, and every thread goes with it.
"""

import logging
import os
import sys
import threading

# See web_routes.py: eos_connect.py owns the "__main__" logger's handlers.
logger = logging.getLogger("__main__")

#: How long the teardown gets before the restart goes ahead regardless.
DEFAULT_TEARDOWN_DEADLINE_S = 10.0


def _frozen_argv():
    """
    The command that started this process, resolved and safe to re-run later.

    ``sys.executable`` covers both ``python`` (Docker image) and
    ``/opt/venv/bin/python`` (add-on image).  ``sys.argv[0]`` is made absolute
    because the repository Dockerfile passes it relative and ``execv`` resolves
    it against the working directory, which is not ours to assume by then.
    ``sys.argv[1:]`` is carried over because eos_connect.py accepts an optional
    config directory there.
    """
    return [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]


class RestartManager:
    """
    Holds the request, bounds the teardown, and replaces the process.

    Deliberately owns no teardown sequence of its own: ``eos_connect.py``
    already has one in ``_shutdown_all()``, used by Ctrl-C and SIGTERM, and a
    second list here would be a second thing to forget a component in.  The
    caller hands its teardown to :meth:`run`.
    """

    def __init__(self, exec_argv=None):
        self._lock = threading.Lock()
        self._requested = False
        self._reason = None
        self._source = None
        #: Frozen now, at import time, for the reasons in :func:`_frozen_argv`.
        self._argv = exec_argv if exec_argv is not None else _frozen_argv()
        #: Set by eos_connect.py to wake the main thread out of its wait.
        self.on_request = None

    @property
    def requested(self):
        """True once a restart has been asked for."""
        with self._lock:
            return self._requested

    @property
    def details(self):
        """What was asked for and by whom, or ``None``."""
        with self._lock:
            if not self._requested:
                return None
            return {"reason": self._reason, "source": self._source}

    @property
    def argv(self):
        """The command this manager would exec. Exposed for tests and logging."""
        return list(self._argv)

    def request(self, reason, source):
        """
        Ask for a restart.  Returns False if one is already under way.

        The caller answers the HTTP request off the back of this, *before* the
        teardown starts — the response has to reach the browser while the socket
        it arrived on is still open.
        """
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            self._reason = reason
            self._source = source

        logger.info("[Restart] Requested by %s: %s", source, reason)
        if self.on_request is not None:
            self.on_request()
        return True

    def run(self, teardown, deadline_s=DEFAULT_TEARDOWN_DEADLINE_S):
        """
        Run ``teardown`` under a deadline, then replace this process. Never returns.
        """
        self._teardown_within(teardown, deadline_s)
        self.exec_replace()

    def _teardown_within(self, teardown, deadline_s):
        """Run ``teardown`` in a worker and stop waiting after ``deadline_s``."""
        failure = []

        def _run():
            try:
                teardown()
            except Exception as exc:  # pylint: disable=broad-except
                failure.append(exc)

        worker = threading.Thread(target=_run, name="RestartTeardown", daemon=True)
        worker.start()
        worker.join(timeout=deadline_s)

        if worker.is_alive():
            logger.warning(
                "[Restart] Shutdown did not finish within %.0f s — restarting "
                "anyway. A component is still busy (most likely an optimization "
                "run waiting on its backend); it goes away with the process image.",
                deadline_s,
            )
        elif failure:
            logger.exception(
                "[Restart] Error during shutdown, restarting anyway", exc_info=failure[0]
            )
        else:
            logger.info("[Restart] Shutdown complete")

    def exec_replace(self):
        """
        Replace this process with a fresh one. Never returns on success.

        Logging is flushed first: ``execv`` does not unwind anything, so a
        buffered handler would lose the last lines, and those are the ones that
        explain why the log suddenly starts again from the top.
        """
        argv = self.argv
        logger.info("[Restart] Restarting now: %s", " ".join(argv))
        logging.shutdown()

        try:
            os.execv(argv[0], argv)
        except OSError:
            # execv only returns by failing. There is no process left to run in
            # any useful state, so make the exit loud and non-zero: a restart
            # policy, where one exists, is the last thing that can help.
            logger.exception("[Restart] Could not restart: %s", argv)
            sys.exit(1)


#: The instance the application uses.  Created at import, which is what freezes
#: the command line early enough to be trustworthy.
RESTART = RestartManager()
