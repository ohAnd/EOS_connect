"""
What a user sees while the application is still starting.

The web server now answers about a second after launch, which means the browser
reaches a page whose data endpoints have nothing behind them yet.  Two things
have to be true for that to be an improvement rather than a confusing blank
spinner: the overlay has to say how far the start has got, and a start that has
failed has to say so and offer the way out — the configuration UI, which the
boot server keeps serving precisely for this.
"""

# pylint: disable=redefined-outer-name

import logging
import threading
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from werkzeug.serving import make_server

from src.boot_server import build_boot_app
from src.boot_state import BootState
from src.config_web.api import config_bp, init_api
from src.config_web.backup import backup_bp, init_backup
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore

from tests.config_web.test_api import _FakeModule, _sample_config
from tests.web.conftest import WEB_DIR, _open_page

SRC_DIR = WEB_DIR.rsplit("/web", 1)[0]


class _ConfigWeb:
    """Mirrors ConfigWebModule.start_api, so the real blueprints are mounted."""

    def __init__(self, store, schema, module):
        self._store, self._schema, self._module = store, schema, module

    def start_api(self, flask_app):
        init_api(self._store, self._schema, self._module)
        init_backup(self._store, self._schema, self._module)
        flask_app.register_blueprint(config_bp)
        flask_app.register_blueprint(backup_bp)


@pytest.fixture
def boot_server(tmp_path):
    """The real boot app on a throwaway database, with its BootState exposed."""
    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "boot_ui.db"))
    store.open()
    config = _sample_config()
    migrate_yaml_to_store(config, store, schema)
    module = _FakeModule(config, store, schema)

    state = BootState()
    memory_handler = Mock()
    memory_handler.get_logs.return_value = []
    memory_handler.get_alerts.return_value = []
    memory_handler.get_buffer_stats.return_value = {}

    app = build_boot_app(
        config_web=_ConfigWeb(store, schema, module),
        memory_handler=memory_handler,
        base_path=SRC_DIR,
        time_zone=ZoneInfo("Europe/Berlin"),
        startup_alerts_since="2026-11-01T08:00:00+01:00",
        boot_state=state,
    )

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


@pytest.mark.browser
def test_the_overlay_reports_which_step_is_running(browser, boot_server):
    url, state = boot_server
    state.begin("mqtt")

    page = _open_page(browser, url)
    try:
        page.wait_for_selector("#boot-progress", state="visible")
        label = page.text_content("#boot-progress-label")
        assert "MQTT broker" in label
        assert "Step" in label
        # The denominator is the whole catalogue, so the bar never grows its own scale.
        assert "of 21" in label
        assert page.text_content("#waiting_text").strip() == "Starting up ..."
    finally:
        page.context.close()


@pytest.mark.browser
def test_the_bar_advances_as_steps_complete(browser, boot_server):
    url, state = boot_server
    state.begin("config_db")

    page = _open_page(browser, url)
    try:
        page.wait_for_selector("#boot-progress", state="visible")
        first = page.eval_on_selector("#boot-progress-bar", "el => el.style.width")

        for step_id in ("config_db", "eos", "base_control", "inverter", "load"):
            state.finish(step_id)
        state.begin("battery")

        page.wait_for_function(
            "before => document.getElementById('boot-progress-bar').style.width !== before",
            arg=first,
        )
        assert "Battery" in page.text_content("#boot-progress-label")
    finally:
        page.context.close()


@pytest.mark.browser
def test_a_failed_start_names_the_component_and_offers_the_configuration(
    browser, boot_server
):
    """Issue #349: the one thing the user needs now is the settings UI."""
    url, state = boot_server
    state.set_fatal(
        "optimization_interface", "Failed to load optimizer configuration: 0"
    )

    page = _open_page(browser, url)
    try:
        page.wait_for_selector("#boot-fatal", state="visible")
        message = page.text_content("#boot-fatal-message")
        assert "optimization_interface" in message
        assert "Failed to load optimizer configuration" in message
        assert page.text_content("#waiting_text").strip() == "Startup stopped"
        # The progress bar gives way to the error: there is no more progress coming.
        assert not page.is_visible("#boot-progress")
        assert page.is_visible("#boot-fatal-config-btn")
    finally:
        page.context.close()


@pytest.mark.browser
def test_the_configuration_ui_opens_from_the_failed_start(browser, boot_server):
    """Rescue mode is only worth anything if the button actually gets there."""
    url, state = boot_server
    state.set_fatal("optimization_interface", "KeyError: 0")

    page = _open_page(browser, url)
    try:
        page.wait_for_function("typeof showConfigurationMenu === 'function'")
        page.wait_for_selector("#boot-fatal-config-btn", state="visible")
        page.click("#boot-fatal-config-btn")
        # The settings panel reads the schema straight off the boot server.
        page.wait_for_selector("text=Configuration", state="visible")
    finally:
        page.context.close()
