"""
Browser-driven tests for the web UI.

These need Playwright and a Chromium build, neither of which is in ``requirements.txt``
— they are marked ``browser`` and left out of the default run.  To run them locally::

    pip install playwright          # NOT pytest-playwright, see below
    playwright install chromium
    pytest tests/web -m browser

The ``-m browser`` is not optional: ``pytest.ini`` defaults to ``-m "not browser"``,
so a bare ``pytest tests/web`` deselects every test in this directory and reports
that nothing ran.

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
import threading
from zoneinfo import ZoneInfo

import pytest
from flask import Flask, jsonify, make_response, render_template_string, send_from_directory
from werkzeug.serving import make_server

from src.config_web.api import config_bp, init_api
from src.docs_links import DOCS_BASE_PREVIEW, DOCS_BASE_RELEASE
from src.config_web.backup import backup_bp, init_backup
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.loads.api import init_api as init_loads_api, loads_bp
from src.loads.manager import ManagedLoadManager, ManagedLoadSources
from src.persistence import PvYieldStore

from tests.config_web.test_api import _FakeModule, _sample_config

try:
    import playwright.sync_api  # noqa: F401  pylint: disable=unused-import
except ImportError:  # pragma: no cover - depends on the environment
    # CI installs requirements.txt plus pytest and nothing else, so skip the whole
    # directory rather than failing collection.
    collect_ignore_glob = ["test_*.py"]

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


def _build_app(store, schema, module, docs_base=DOCS_BASE_RELEASE):
    """A minimal stand-in for the real server: the same blueprints and the same assets."""
    app = Flask(__name__)

    @app.route("/")
    def index():
        # ``docs_base`` mirrors what eos_connect.py:main_page resolves from
        # src/version.py. The UI builds every documentation link from it
        # (constants.js:docsUrl), so leaving it out here would silently exercise
        # the fallback instead of the real wiring.
        with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as fh:
            return make_response(
                render_template_string(
                    fh.read(), asset_version="test", docs_base_url=docs_base
                )
            )

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

    # A real managed-load manager, so the overlay is exercised against the same JSON
    # the app serves rather than against a stub that cannot get its shape wrong. The
    # sensors are fixed values: this is about the UI, not about reading Home Assistant.
    app.register_blueprint(loads_bp)
    init_loads_api(_managed_load_manager())
    return app


def _managed_load_manager():
    """A pool and a sauna, warm enough to have something to plan."""
    entries = [
        {
            "id": "pool", "type": "pool_heatpump", "enabled": True,
            "temp_sensor": "sensor.pool", "power_sensor": "sensor.pool_power",
            "target_temp": 28.0, "window_start": None, "window_end": None,
            "season_start": None, "season_end": None, "min_ambient_temp_c": None,
        },
        {
            "id": "sauna", "type": "sauna", "enabled": True,
            "temp_sensor": "sensor.sauna", "target_temp": 90.0,
        },
    ]
    readings = {"sensor.pool": 24.2, "sensor.pool_power": 0.0, "sensor.sauna": 88.5}
    manager = ManagedLoadManager(
        entries,
        time_frame_base=3600,
        time_zone=ZoneInfo("Europe/Berlin"),
        sources=ManagedLoadSources(read_sensor=readings.get),
    )
    manager.run_cycle()
    return manager


class _Server:  # pylint: disable=too-few-public-methods
    """Handle to a running app: its URL and the stores behind it."""

    def __init__(self, url, store, pv_store):
        self.url = url
        self.store = store
        self.pv_store = pv_store


def _serve(tmp_path, db_name, bootstrap_config, *, migrate, docs_base=DOCS_BASE_RELEASE):
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

    app = _build_app(store, schema, module, docs_base=docs_base)
    # The dashboard polls several endpoints a second; its request log drowns out the
    # test output without saying anything useful.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # Bound here rather than inside the thread, and on port 0 rather than one
    # picked beforehand. Starting a thread is not the same as being ready to
    # accept, so the first page.goto could arrive before Flask was listening and
    # fail with ERR_CONNECTION_REFUSED - seen twice in CI, green on a re-run.
    # Asking for a port, closing it and letting the server bind it again left a
    # second gap in between for anything else on the machine to take it.
    # threaded=True is what app.run defaults to; the dashboard issues several
    # requests at once and a serial server would queue them.
    server = make_server("127.0.0.1", 0, app, threaded=True)
    # The poll interval is how long shutdown() waits to be noticed, and this
    # fixture is function scoped: at the default half-second it added some three
    # minutes across the browser lane for nothing.
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()

    try:
        yield _Server(f"http://127.0.0.1:{server.port}", store, pv_store)
    finally:
        # Every browser test gets its own server - the fixture is function
        # scoped - and none of them used to end. Each left a listening socket
        # and a thread alive for the rest of the session, several hundred of
        # them by the end of the lane, which is exactly the condition under
        # which a server is slow to come up.
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
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


def _open_page(browser, url):
    """A page on *url*, in its own context, with the dashboard's CDN traffic blocked.

    The context is what keeps tests apart now that the browser is shared across the
    session: storage, cookies and routes all belong to it, and closing it in the
    fixture teardown leaves nothing behind for the next test.
    """
    # Pinned, because the dashboard reads the clock. `chart.js` turns the server's
    # timestamp into a slot index with `Date.getHours()`, which answers in the
    # *browser's* zone - so a chart assertion that holds on a developer's machine in
    # Berlin fails on a CI runner in UTC, six hours off and entirely plausible looking.
    # UTC is the choice because that is what CI already runs under: pinning it makes a
    # local run agree with the one that decides whether the branch is green.
    context = browser.new_context(timezone_id="UTC")
    page = context.new_page()
    # The page pulls FontAwesome, Chart.js and a font from CDNs. Blocking them keeps
    # the tests offline and fast; none of them affect the behaviour under test.
    page.route("**://cdnjs.cloudflare.com/**", lambda route: route.abort())
    page.route("**://cdn.jsdelivr.net/**", lambda route: route.abort())
    page.route("**://fonts.cdnfonts.com/**", lambda route: route.abort())

    # Fail fast: without this a broken selector costs the Playwright default of 30s.
    page.set_default_timeout(8000)

    page.goto(url, wait_until="domcontentloaded")
    return page


def _suppress_startup_overlay(page):
    """Keep the boot overlay down for the rest of the test.

    Setting ``style.display`` once is not enough: ``main.js`` puts the overlay back
    with ``display = 'flex'`` whenever a data response carries an error, and this
    harness answers ``/api/*`` and ``/json/*`` with ``{}``, so the dashboard's
    one-second poll re-raises it. Any test that took longer than that to reach its
    first click then failed on "<div id=\"overlay\"> intercepts pointer events",
    which is a race in the harness and nothing to do with what is under test.

    A stylesheet wins over the inline style the app sets, so this holds. Only
    ``page`` and ``develop_page`` get it — ``fresh_page`` must keep the real
    behaviour, because the wizard hiding the overlay is itself under test.
    """
    page.add_style_tag(content="#overlay { display: none !important; }")


@pytest.fixture(name="page")
def page_fixture(browser, server):
    """A browser page with the app open and the dashboard's own noise suppressed."""
    page = _open_page(browser, server.url)
    page.wait_for_function("typeof showBackupMenu === 'function'")

    # The startup overlay only clears once the dashboard has rendered its chart from
    # real optimizer output, which this harness does not serve. It covers the menu
    # icon, so dismiss it — booting the dashboard is not what these tests are about.
    _suppress_startup_overlay(page)
    try:
        yield page
    finally:
        page.context.close()


@pytest.fixture(name="fresh_page")
def fresh_page_fixture(browser, fresh_server):
    """
    A browser page on a first install, with the Setup Wizard already open.

    The startup overlay is left alone here: the wizard hides it itself
    (``wizard.js:107-111``), and whether it does is part of what these tests check.
    ``init()`` polls once a second and calls ``checkWizardStatus()`` on every path, so
    the wizard arrives on its own — no test needs to summon it.
    """
    page = _open_page(browser, fresh_server.url)
    page.wait_for_function("typeof showSetupWizard === 'function'")
    page.wait_for_selector(".wizard-container")
    try:
        yield page
    finally:
        page.context.close()


@pytest.fixture(name="develop_server")
def develop_server_fixture(tmp_path):
    """The same app as ``server``, but serving a develop build's documentation base.

    ``eos_connect.py:main_page`` derives this from the ``-develop`` suffix in
    ``src/version.py``; here it is passed in directly, so the test does not depend on
    which branch the checkout happens to sit on.
    """
    yield from _serve(
        tmp_path, "develop.db", _sample_config(), migrate=True, docs_base=DOCS_BASE_PREVIEW
    )


@pytest.fixture(name="develop_page")
def develop_page_fixture(browser, develop_server):
    """A browser page on a develop build, dashboard noise suppressed as in ``page``."""
    page = _open_page(browser, develop_server.url)
    page.wait_for_function("typeof showBackupMenu === 'function'")
    _suppress_startup_overlay(page)
    try:
        yield page
    finally:
        page.context.close()
