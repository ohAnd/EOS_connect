"""
Something to answer on port 8081 while the application is still being built.

``eos_connect.py`` constructs everything at module level and only then creates its
Flask app, so for the 15–45 s (worst case: minutes) a start takes, there was
nothing listening at all — the browser got *connection refused*, and a start that
died on a bad setting took the configuration UI down with it (issues #349, #310,
#83).

The fix is not to make the real app answer earlier.  Flask locks route
registration the moment an app serves its first request
(``_check_setup_finished``), so letting a request through before the last
``@app.route`` has run would turn a page load into an ``AssertionError`` at
import time.  Instead a second, small app is built in one go here, and a gate
swaps to the real one once it is complete.

What the boot app serves is deliberately not a stripped-down placeholder:

* the same ``index.html``, so the overlay, the startup-error panel and the log
  viewer the user already knows are what they see;
* the whole configuration API — this is the point.  A start that cannot finish
  because of a bad setting leaves the user with the one tool that can fix it,
  and a restart away from a working application.
"""

import logging

from flask import Flask, jsonify

try:  # running from src/ as a script — src/ is on sys.path
    from web_routes import register_log_routes, register_static_routes
except ImportError:  # imported as src.boot_server (tests)
    from .web_routes import register_log_routes, register_static_routes

# See web_routes.py: eos_connect.py owns the "__main__" logger's handlers.
logger = logging.getLogger("__main__")

#: What the dashboard's data endpoints answer with until the application is up.
#: This exact shape is what ``OptimizationScheduler.__init__`` already stores as
#: its first placeholder, which means ``data.js:getErrorInfo()`` renders it with
#: no frontend change: the overlay stays up and reads "starting up".
STARTING_UP = {
    "status": "starting up",
    "message": (
        "EOS Connect is still starting. This page will update by itself once "
        "the first optimization run has completed."
    ),
}

#: Dashboard endpoints the boot app stands in for.  Listed rather than caught by
#: a wildcard so an unknown URL still 404s — a catch-all that answers 200 for
#: everything hides typos and makes the running app and the boot app disagree
#: about what exists.
_PLACEHOLDER_ROUTES = (
    "/json/optimize_request.json",
    "/json/optimize_response.json",
    "/json/managed_loads_request.json",
    "/json/price_info.json",
    "/api/update/status",
    "/api/pv_autoscaling/status",
)


class BootGate:
    """
    One WSGI callable whose answer changes once the application is ready.

    waitress binds this at ``create_server()`` time and never sees the swap: the
    socket, the listening thread and the port stay exactly as they were, so
    there is no hand-over window and no second bind to lose a race with.

    ``target`` is a plain attribute.  Rebinding one in CPython is atomic, so a
    worker thread reads either the boot app or the real app, never a torn state;
    a lock here would serialise every request for no gain.
    """

    def __init__(self, boot_app):
        self.boot_app = boot_app
        self.target = None

    @property
    def ready(self):
        """True once requests are going to the real application."""
        return self.target is not None

    def __call__(self, environ, start_response):
        app = self.target or self.boot_app
        return app(environ, start_response)


def build_boot_app(
    config_web,
    memory_handler,
    base_path,
    time_zone,
    startup_alerts_since,
    boot_state,
):
    """
    Build the stand-in application, complete, in one call.

    Everything is registered before this returns, which is what makes it safe to
    serve immediately — unlike the real app, it has no routes still to come.
    """
    # __name__, not a made-up label: Flask resolves its import_name to find a
    # root path, and pkgutil.get_loader() on a dotted name imports the parent
    # package to do it. "eos_connect.boot" therefore re-executed eos_connect.py
    # from top to bottom — a second config database, a second set of interface
    # threads, and a second bind to the same port. This module is already in
    # sys.modules, so Flask reads it straight from there and imports nothing.
    boot_app = Flask(__name__)
    boot_app.config["JSON_SORT_KEYS"] = False

    register_static_routes(boot_app, base_path)
    register_log_routes(boot_app, memory_handler, time_zone, startup_alerts_since)

    # The reason this app exists. Registering the same blueprint objects on a
    # second Flask app is supported; each app keeps its own URL map, and both
    # end up talking to the one ConfigStore the module globals already hold.
    try:
        config_web.start_api(boot_app)
    except Exception:  # pylint: disable=broad-except
        # Deliberately broad. This runs before anything else is up, and the boot
        # server losing its settings API is bad but survivable — the page, the
        # logs and the boot status still answer. Letting it propagate would take
        # down the one thing that can report what went wrong.
        logger.exception(
            "[Boot] Config API could not be registered on the boot server — "
            "the configuration UI will stay unavailable until startup finishes"
        )

    @boot_app.route("/api/boot/status", methods=["GET"])
    def boot_status():  # pylint: disable=unused-variable
        """How far the start has got. Polled by the startup overlay."""
        return jsonify(boot_state.snapshot())

    for rule in _PLACEHOLDER_ROUTES:
        boot_app.add_url_rule(
            rule,
            endpoint=f"starting_up{rule.replace('/', '_').replace('.', '_')}",
            view_func=_starting_up,
            methods=["GET"],
        )

    @boot_app.route("/json/current_controls.json", methods=["GET"])
    def controls_placeholder():  # pylint: disable=unused-variable
        """
        The one placeholder that needs a shape rather than just a status.

        ``main.js`` reads ``localization.currency`` off this response on every
        poll.  It defaults each field, but only if the key is there — handing it
        a bare status would throw a TypeError and paint "Connection Error" over
        a perfectly healthy start.
        """
        return jsonify({**STARTING_UP, "localization": {}})

    return boot_app


def _starting_up():
    """Shared view for every dashboard endpoint that has no data yet."""
    return jsonify(STARTING_UP)
