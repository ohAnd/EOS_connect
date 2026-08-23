"""
Browser-driven tests for the web UI.

These need Playwright and a Chromium build, neither of which is in ``requirements.txt``
— CI has no browser, so the whole directory is skipped there.  To run them locally::

    pip install playwright          # NOT pytest-playwright, see below
    playwright install chromium
    pytest tests/web

Install ``playwright`` alone.  ``pytest-playwright`` depends on ``pytest-base-url``,
whose ``base_url`` fixture is session-scoped and collides with the function-scoped
``base_url`` in ``tests/interfaces/optimization_backends/test_optimization_backend_eos.py``,
erroring out 44 unrelated tests.  Nothing here needs that plugin.

Chromium also needs a few shared libraries.  With root that is
``playwright install-deps chromium``; without it, run
``scripts/install_playwright_deps_userspace.sh``, which unpacks them under
``~/.cache/ms-playwright-deps`` — picked up automatically below.

The rest of the suite exercises the REST layer; this exercises what the browser actually
does with it.
"""

import logging
import os
import socket
import threading

import pytest
from flask import Flask, jsonify, make_response, render_template_string, send_from_directory

from src.config_web.api import config_bp, init_api
from src.config_web.backup import backup_bp, init_backup
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.persistence import PvYieldStore

from tests.config_web.test_api import _FakeModule, _sample_config

try:
    import playwright.sync_api  # noqa: F401  pylint: disable=unused-import
except ImportError:  # pragma: no cover - depends on the environment
    # CI installs requirements.txt plus pytest and nothing else, so skip the whole
    # directory rather than failing collection.
    collect_ignore_glob = ["test_*.py"]

# Shared libraries unpacked by scripts/install_playwright_deps_userspace.sh, for hosts
# where Chromium's dependencies cannot be apt-installed.  Chromium is a subprocess, so
# it inherits this; harmless when the directory does not exist.
_USER_LIBS = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright-deps", "lib")
if os.path.isdir(_USER_LIBS):
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
        [_USER_LIBS, os.environ.get("LD_LIBRARY_PATH", "")]
    ).rstrip(os.pathsep)

WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "web"
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_app(store, schema, module):
    """A minimal stand-in for the real server: the same blueprints and the same assets."""
    app = Flask(__name__)

    @app.route("/")
    def index():
        with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as fh:
            return make_response(render_template_string(fh.read(), asset_version="test"))

    @app.route("/js/<path:filename>")
    def js(filename):
        return send_from_directory(
            os.path.join(WEB_DIR, "js"), filename, mimetype="application/javascript"
        )

    @app.route("/css/<path:filename>")
    def css(filename):
        return send_from_directory(os.path.join(WEB_DIR, "css"), filename, mimetype="text/css")

    # The dashboard polls these on load. They are not what these tests are about, but an
    # unhandled 404 storm makes the console unreadable, so answer them minimally.
    @app.route("/json/<path:_any>")
    @app.route("/api/<path:_any>")
    def stub(_any):
        return jsonify({})

    init_api(store, schema, module)
    init_backup(store, schema, module)
    app.register_blueprint(config_bp)
    app.register_blueprint(backup_bp)
    return app


@pytest.fixture(name="server")
def server_fixture(tmp_path):
    """A real HTTP server on a throwaway database, serving the real UI assets."""
    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "ui.db"))
    store.open()
    migrate_yaml_to_store(_sample_config(), store, schema)

    module = _FakeModule(_sample_config(), store, schema)
    pv_store = PvYieldStore(store)
    pv_store.ensure_schema()
    module.pv_yield_store = pv_store

    port = _free_port()
    app = _build_app(store, schema, module)
    # The dashboard polls several endpoints a second; its request log drowns out the
    # test output without saying anything useful.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()

    class Server:  # pylint: disable=too-few-public-methods
        """Handle to the running app: its URL and the stores behind it."""

        url = f"http://127.0.0.1:{port}"

    Server.store = store
    Server.pv_store = pv_store
    try:
        yield Server
    finally:
        store.close()


@pytest.fixture(name="page")
def page_fixture(server):
    """A browser page with the app open and the dashboard's own noise suppressed."""
    # Imported here, not at module scope: the directory is skipped when Playwright is
    # absent, and a top-level import would run before that check.
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        # Playwright raises a variety of errors for a browser that will not start
        # (missing libraries, no sandbox, out of memory); all of them mean "skip".
        except Exception as exc:  # pylint: disable=broad-exception-caught
            pytest.skip(f"Chromium could not be launched: {exc}")

        page = browser.new_page()
        # The page pulls FontAwesome, Chart.js and a font from CDNs. Blocking them keeps
        # the tests offline and fast; none of them affect the behaviour under test.
        page.route("**://cdnjs.cloudflare.com/**", lambda route: route.abort())
        page.route("**://cdn.jsdelivr.net/**", lambda route: route.abort())
        page.route("**://fonts.cdnfonts.com/**", lambda route: route.abort())

        # Fail fast: without this a broken selector costs the Playwright default of 30s.
        page.set_default_timeout(8000)

        page.goto(server.url, wait_until="domcontentloaded")
        page.wait_for_function("typeof showBackupMenu === 'function'")

        # The startup overlay only clears once the dashboard has rendered its chart from
        # real optimizer output, which this harness does not serve. It covers the menu
        # icon, so dismiss it — booting the dashboard is not what these tests are about.
        page.evaluate(
            "() => { const o = document.getElementById('overlay');"
            " if (o) { o.style.display = 'none'; } }"
        )
        try:
            yield page
        finally:
            browser.close()
