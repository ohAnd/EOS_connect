"""
The routes both the boot server and the running application serve.

``eos_connect.py`` builds its Flask app at the very end of a start that can take
minutes, so it cannot be the thing that answers while that start is still going
on.  ``boot_server.py`` stands in until then — and it has to serve the *same*
page, not a stripped-down placeholder: ``index.html`` already carries the
startup overlay, the startup-error panel and the log viewer, which is exactly
what a user wants to look at while the application is coming up.

Rather than duplicate those handlers, both apps register them from here.  The
functions are plain closures over what they need; registering the same ones on
two Flask apps is fine because each app keeps its own URL map.
"""

import json
import logging
import os
import time
from datetime import datetime

from flask import (
    Response,
    make_response,
    render_template_string,
    request,
    send_from_directory,
)
from werkzeug.exceptions import NotFound

try:  # running from src/ as a script — src/ is on sys.path
    from version import __version__
    from docs_links import docs_base_url
except ImportError:  # imported as src.web_routes (tests)
    from .version import __version__
    from .docs_links import docs_base_url

# eos_connect.py attaches its handlers to the "__main__" logger, not to root, so
# a logging.getLogger(__name__) here would propagate to a handler-less root and
# every message would be silently discarded — including the ones the log viewer
# exists to show.  Same reasoning as in startup_validator.py.
logger = logging.getLogger("__main__")

#: Static assets are fingerprinted with ``asset_version``, so they could be
#: cached forever.  They are not: the handlers below send no-cache headers on
#: top, because an add-on update replaces the files underneath a browser that
#: still holds the old page.  The value is kept as the ``send_from_directory``
#: max_age for the one case the headers do not cover — a conditional request
#: Flask answers before the response headers are applied.
ASSET_CACHE_MAX_AGE_SECONDS = 31536000


def _no_store(response):
    """Mark a response as must-revalidate, for all three header generations."""
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def register_static_routes(flask_app, base_path):
    """
    Register ``/``, ``/js/<filename>`` and ``/css/<filename>`` on ``flask_app``.

    ``base_path`` is the directory holding ``web/`` — ``src/`` in every
    deployment, passed in rather than derived so a caller is never surprised by
    which file's ``__file__`` won.

    Resolved to an absolute path before use: ``send_from_directory`` takes a
    relative directory as relative to the Flask app's ``root_path``, which
    differs between the boot app and the real one, so a relative ``base_path``
    would serve the page from one and 404 the scripts from the other.
    """
    web_dir = os.path.join(os.path.abspath(base_path), "web")

    @flask_app.route("/", methods=["GET"])
    def main_page():  # pylint: disable=unused-variable
        """
        Render the single page the whole UI lives in.

        ``docs_base_url`` is resolved here rather than in the browser: the
        version only reaches the frontend through /json/current_controls.json,
        which lands after the menu and the config panel have already built
        their documentation links.
        """
        with open(
            os.path.join(web_dir, "index.html"), "r", encoding="utf-8"
        ) as html_file:
            rendered_html = render_template_string(
                html_file.read(),
                asset_version=f"{__version__}-{int(time.time())}",
                docs_base_url=docs_base_url(__version__),
            )
        return _no_store(make_response(rendered_html))

    @flask_app.route("/js/<filename>")
    def serve_js_files(filename):  # pylint: disable=unused-variable
        """Serve a JavaScript module, so adding one needs no server change."""
        return _serve_asset(os.path.join(web_dir, "js"), filename, ".js",
                            "application/javascript", "JavaScript")

    @flask_app.route("/css/<filename>")
    def serve_css_files(filename):  # pylint: disable=unused-variable
        """Serve a stylesheet."""
        return _serve_asset(os.path.join(web_dir, "css"), filename, ".css",
                            "text/css", "CSS")


def _serve_asset(directory, filename, suffix, mimetype, kind):
    """
    One guarded ``send_from_directory`` for both asset kinds.

    The missing-file case is left to ``send_from_directory``, which already
    answers it with ``NotFound``, rather than being pre-empted by an
    ``os.path.exists`` on a path built from ``filename``: that join was the one
    place the URL reached a path expression unfiltered.  It was never
    exploitable — the route converter rejects slashes and ``send_from_directory``
    joins through ``safe_join`` — but both branches of the check returned the
    same 404 anyway, so the check bought nothing in exchange for being the one
    thing a reader (and CodeQL) has to reason about.  Traversal attempts land in
    the same ``NotFound`` as a plain typo.
    """
    try:
        if not filename.endswith(suffix):
            logger.warning(
                "[Web] Blocked attempt to serve non-%s file: %s", kind, filename
            )
            return "Not Found", 404

        try:
            response = send_from_directory(
                directory,
                filename,
                mimetype=mimetype,
                max_age=ASSET_CACHE_MAX_AGE_SECONDS,
            )
        except NotFound:
            logger.warning("[Web] %s file not found: %s", kind, filename)
            return "Not Found", 404

        return _no_store(response)

    except (OSError, IOError, ValueError) as e:
        logger.error("[Web] Error serving %s file %s: %s", kind, filename, e)
        return "Server Error", 500


def register_log_routes(flask_app, memory_handler, time_zone, startup_alerts_since):
    """
    Register the ``/logs`` family on ``flask_app``.

    Registered on the boot server too, and deliberately including the two
    clearing routes: when a start is slow or has failed, the log viewer is the
    main thing the user has to work with, and a read-only half of it would be a
    worse version of the panel they already know.

    ``startup_alerts_since`` is the ISO timestamp marking this process' start;
    it is fixed for the life of the process, so it is passed by value.
    """

    @flask_app.route("/logs", methods=["GET"])
    def get_logs():  # pylint: disable=unused-variable
        """
        Retrieve application logs with optional filtering.

        Query parameters:
        - level: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        - limit: Maximum number of records to return (default: 100)
        - since: ISO timestamp to get logs since that time
        """
        try:
            level_filter = request.args.get("level")
            limit = int(request.args.get("limit", 100))
            since = request.args.get("since")

            logs = memory_handler.get_logs(
                level_filter=level_filter, limit=limit, since=since
            )

            return _json(
                {
                    "logs": logs,
                    "total_count": len(logs),
                    "timestamp": datetime.now(time_zone).isoformat(),
                    "filters_applied": {
                        "level": level_filter,
                        "limit": limit,
                        "since": since,
                    },
                }
            )

        except (ValueError, TypeError, KeyError) as e:
            logger.error("[Web] Error retrieving logs: %s", e)
            return _json({"error": "Failed to retrieve logs"}, status=500)

    @flask_app.route("/logs/alerts", methods=["GET"])
    def get_alerts():  # pylint: disable=unused-variable
        """
        Retrieve warning and error logs for alert system.

        Query parameters:
        - startup_only: if true/1/yes, only return alerts since current process start
        - since: optional ISO timestamp override for custom filtering
        - limit: optional maximum number of returned alerts
        """
        try:
            startup_only_arg = request.args.get("startup_only", "false").strip().lower()
            startup_only = startup_only_arg in {"1", "true", "yes", "on"}

            # Explicit override wins; otherwise use the start marker if asked for.
            since = request.args.get("since")
            if startup_only and not since:
                since = startup_alerts_since

            limit_arg = request.args.get("limit")
            limit = int(limit_arg) if limit_arg else None

            alerts = memory_handler.get_alerts(since=since, limit=limit)

            grouped_alerts = {
                "WARNING": [a for a in alerts if a["level"] == "WARNING"],
                "ERROR": [a for a in alerts if a["level"] == "ERROR"],
                "CRITICAL": [a for a in alerts if a["level"] == "CRITICAL"],
            }

            return _json(
                {
                    "alerts": alerts,
                    "grouped_alerts": grouped_alerts,
                    "alert_counts": {
                        level: len(items) for level, items in grouped_alerts.items()
                    },
                    "filters_applied": {
                        "startup_only": startup_only,
                        "since": since,
                        "limit": limit,
                    },
                    "startup_since": startup_alerts_since,
                    "timestamp": datetime.now(time_zone).isoformat(),
                }
            )

        except (ValueError, TypeError, KeyError) as e:
            logger.error("[Web] Error retrieving alerts: %s", e)
            return _json({"error": "Failed to retrieve alerts"}, status=500)

    @flask_app.route("/logs/clear", methods=["POST"])
    def clear_logs():  # pylint: disable=unused-variable
        """Clear all stored logs from memory (file logs remain intact)."""
        try:
            memory_handler.clear_logs()
            logger.info("[Web] Memory logs cleared via web API")
            return _json({"status": "success", "message": "Logs cleared"}, indent=None)

        except (RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.error("[Web] Error clearing logs: %s", e)
            return _json({"error": "Failed to clear logs"}, status=500, indent=None)

    @flask_app.route("/logs/alerts/clear", methods=["POST"])
    def clear_alerts_only():  # pylint: disable=unused-variable
        """Clear only alert logs from memory, keeping regular logs intact."""
        try:
            memory_handler.clear_alerts_only()
            logger.info("[Web] Alert logs cleared via web API")
            return _json(
                {"status": "success", "message": "Alert logs cleared"}, indent=None
            )

        except (RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.error("[Web] Error clearing alert logs: %s", e)
            return _json(
                {"error": "Failed to clear alert logs"}, status=500, indent=None
            )

    @flask_app.route("/logs/stats", methods=["GET"])
    def get_log_stats():  # pylint: disable=unused-variable
        """Get buffer usage statistics."""
        try:
            return _json(
                {
                    "buffer_stats": memory_handler.get_buffer_stats(),
                    "timestamp": datetime.now(time_zone).isoformat(),
                }
            )

        except (ValueError, TypeError, KeyError) as e:
            logger.error("[Web] Error retrieving buffer stats: %s", e)
            return _json({"error": "Failed to retrieve buffer stats"}, status=500)


def _json(payload, status=200, indent=2):
    """The raw-``Response`` JSON shape the ``eos_connect`` app routes all use."""
    return Response(
        json.dumps(payload, indent=indent),
        status=status,
        content_type="application/json",
    )
