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

Two harnesses live here.  ``server``/``page`` open a *configured* install — the store has
been migrated from a real config, so the dashboard is what a returning user sees.
``fresh_server``/``fresh_page`` open a *first* install: the bootstrap config holds only the
three ``config.yaml`` keys, so migration writes seven defaults and marks neither
``_wizard_completed`` nor ``_migrated_from_yaml`` — which is exactly what makes
``/api/config/wizard-status`` report ``pending`` and the Setup Wizard appear.
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

# What ``ConfigManager`` holds on a first run: config.yaml is bootstrap-only since
# ``fd56222``, so these three keys are the whole of it.  Everything else comes from the
# schema defaults by way of the merger.
BOOTSTRAP_ONLY_CONFIG = {
    "eos_connect_web_port": 8081,
    "time_zone": "Europe/Berlin",
    "log_level": "info",
}


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


class _Server:  # pylint: disable=too-few-public-methods
    """Handle to a running app: its URL and the stores behind it."""

    def __init__(self, url, store, pv_store):
        self.url = url
        self.store = store
        self.pv_store = pv_store


def _serve(tmp_path, db_name, bootstrap_config, *, migrate):
    """
    Start the app on a throwaway database and yield a handle to it.

    ``migrate`` decides which install the tests get.  With a real config it produces a
    configured one; with the bootstrap-only config it produces a first install, because
    ``migrate_yaml_to_store`` then finds no user-configured value and deliberately leaves
    the wizard flags unset (``migration.py:73-97``).
    """
    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / db_name))
    store.open()
    if migrate:
        migrate_yaml_to_store(bootstrap_config, store, schema)

    module = _FakeModule(bootstrap_config, store, schema)
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

    try:
        yield _Server(f"http://127.0.0.1:{port}", store, pv_store)
    finally:
        store.close()


@pytest.fixture(name="server")
def server_fixture(tmp_path):
    """A real HTTP server on a throwaway database, serving the real UI assets."""
    yield from _serve(tmp_path, "ui.db", _sample_config(), migrate=True)


@pytest.fixture(name="fresh_server")
def fresh_server_fixture(tmp_path):
    """
    The same server, on a database that has never seen a configuration.

    This is the first-install state: seven seeded defaults, no wizard flags, an empty
    ``pv_forecast`` list.
    """
    yield from _serve(tmp_path, "fresh.db", dict(BOOTSTRAP_ONLY_CONFIG), migrate=True)


@pytest.fixture(name="offline_timeseries")
def offline_timeseries_fixture(monkeypatch):
    """
    Let a timeseries endpoint validate without one existing.

    Saving a timeseries source runs a real pre-flight fetch against the URL
    (``_check_timeseries_preflight``), which is the right thing for a user — an
    unreachable endpoint is worth hearing about before it is stored — but it makes a
    test that configures one depend on the network.  Tests that are about the wizard
    rather than the probe ask for this.
    """
    from src.config_web import api as config_api  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(config_api, "probe", lambda *a, **kw: {"ok": True, "warnings": []})


def _launch_browser(playwright):
    """Chromium, or a skip if this host cannot start it."""
    try:
        return playwright.chromium.launch()
    # Playwright raises a variety of errors for a browser that will not start
    # (missing libraries, no sandbox, out of memory); all of them mean "skip".
    except Exception as exc:  # pylint: disable=broad-exception-caught
        pytest.skip(f"Chromium could not be launched: {exc}")


def _open_page(browser, url):
    """A page on *url* with the dashboard's CDN traffic blocked."""
    page = browser.new_page()
    # The page pulls FontAwesome, Chart.js and a font from CDNs. Blocking them keeps
    # the tests offline and fast; none of them affect the behaviour under test.
    page.route("**://cdnjs.cloudflare.com/**", lambda route: route.abort())
    page.route("**://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.route("**://fonts.cdnfonts.com/**", lambda route: route.abort())

    # Fail fast: without this a broken selector costs the Playwright default of 30s.
    page.set_default_timeout(8000)

    page.goto(url, wait_until="domcontentloaded")
    return page


@pytest.fixture(name="page")
def page_fixture(server):
    """A browser page with the app open and the dashboard's own noise suppressed."""
    # Imported here, not at module scope: the directory is skipped when Playwright is
    # absent, and a top-level import would run before that check.
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = _open_page(browser, server.url)
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


@pytest.fixture(name="fresh_page")
def fresh_page_fixture(fresh_server):
    """
    A browser page on a first install, with the Setup Wizard already open.

    The startup overlay is left alone here: the wizard hides it itself
    (``wizard.js:107-111``), and whether it does is part of what these tests check.
    ``init()`` polls once a second and calls ``checkWizardStatus()`` on every path, so
    the wizard arrives on its own — no test needs to summon it.
    """
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = _open_page(browser, fresh_server.url)
        page.wait_for_function("typeof showSetupWizard === 'function'")
        page.wait_for_selector(".wizard-container")
        try:
            yield page
        finally:
            browser.close()
