"""
What answers on port 8081 before the application has finished building itself.

The whole point of the boot server is that the configuration UI survives a start
that cannot complete (#349).  Two things have to hold for that: the config
blueprints must tolerate being registered on a second Flask app, and the gate
must hand over to the real app without the real app ever seeing an earlier
request — Flask bars route registration once an app has served one.
"""

# pylint: disable=protected-access,redefined-outer-name

import sys
from unittest.mock import Mock, patch

import pytest
import pytz
from flask import Flask

from src.boot_server import STARTING_UP, BootGate, build_boot_app
from src.boot_state import BootState
from src.config_web.api import config_bp, init_api
from src.config_web.backup import backup_bp, init_backup
from src.config_web.merger import build_merged_config
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.restart import RestartManager

from tests.config_web.test_api import _FakeModule, _sample_config

BASE_PATH = "src"
TIME_ZONE = pytz.timezone("Europe/Berlin")
STARTUP_SINCE = "2026-11-01T08:00:00+01:00"


class _FakeConfigWeb:
    """Mirrors exactly what ``ConfigWebModule.start_api`` does to a Flask app."""

    def __init__(self, store, schema, module):
        self._store = store
        self._schema = schema
        self._module = module
        self.apps = []

    def start_api(self, flask_app):
        """Register both config blueprints, as the real facade does."""
        init_api(self._store, self._schema, self._module)
        init_backup(self._store, self._schema, self._module)
        flask_app.register_blueprint(config_bp)
        flask_app.register_blueprint(backup_bp)
        self.apps.append(flask_app)


@pytest.fixture
def config_web(tmp_path):
    """A stand-in facade backed by a real store, so the API really answers."""
    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "boot.db"))
    store.open()
    config = _sample_config()
    migrate_yaml_to_store(config, store, schema)
    module = _FakeModule(config, store, schema)
    yield _FakeConfigWeb(store, schema, module)
    store.close()


@pytest.fixture
def memory_handler():
    """The log buffer the /logs routes read."""
    handler = Mock()
    handler.get_logs.return_value = []
    handler.get_alerts.return_value = []
    handler.get_buffer_stats.return_value = {"records": 0}
    return handler


@pytest.fixture
def boot_state():
    return BootState()


@pytest.fixture
def boot_app(config_web, memory_handler, boot_state):
    app = build_boot_app(
        config_web=config_web,
        memory_handler=memory_handler,
        base_path=BASE_PATH,
        time_zone=TIME_ZONE,
        startup_alerts_since=STARTUP_SINCE,
        boot_state=boot_state,
    )
    app.config["TESTING"] = True
    return app


# ----------------------------------------------------------------------
# The assumption the whole design rests on
# ----------------------------------------------------------------------


def test_config_blueprints_register_on_two_apps(config_web):
    """
    The boot app and the real app both carry the configuration API.

    If Flask refused a second registration, the boot server could not offer the
    one thing it exists for.
    """
    first, second = Flask("first"), Flask("second")
    config_web.start_api(first)
    config_web.start_api(second)

    for app in (first, second):
        app.config["TESTING"] = True
        with app.test_client() as client:
            assert client.get("/api/config/").status_code == 200
            assert client.get("/api/config/restart-required").status_code == 200


def test_real_app_is_never_asked_before_handover():
    """
    Flask bars route registration after the first request (``_got_first_request``),
    so a late ``@app.route`` would blow up at import time if the gate leaked a
    request through early. The gate must keep the real app untouched.
    """
    boot = Flask("boot")

    @boot.route("/")
    def _boot_index():
        return "booting"

    real = Flask("real")
    gate = BootGate(boot)

    client = _wsgi_client(gate)
    assert client("/") == "booting"
    assert real._got_first_request is False

    # Routes can still be added — this is what would fail on a leaked request.
    @real.route("/")
    def _real_index():
        return "ready"

    gate.target = real
    assert client("/") == "ready"


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


def test_boot_app_import_name_is_already_imported(boot_app):
    """
    Flask resolves ``import_name`` to find its root path, and ``pkgutil.get_loader``
    on a dotted name *imports the parent package* to do it. Naming the boot app
    "eos_connect.boot" therefore re-ran eos_connect.py end to end: a second config
    database, a second set of interface threads, and a second bind to the same
    port, which then failed as a port conflict. Keeping the name to a module that
    is already loaded means Flask reads sys.modules and imports nothing.
    """
    assert boot_app.import_name in sys.modules


def test_gate_reports_not_ready_until_target_is_set():
    gate = BootGate(Flask("boot"))
    assert gate.ready is False
    gate.target = Flask("real")
    assert gate.ready is True


# ----------------------------------------------------------------------
# What the boot app serves
# ----------------------------------------------------------------------


def test_boot_status_reports_progress(boot_app, boot_state):
    with boot_state.step("mqtt"):
        with boot_app.test_client() as client:
            body = client.get("/api/boot/status").get_json()

    assert body["ready"] is False
    assert body["current"] == "mqtt"
    assert body["current_label"] == "MQTT broker"
    assert body["total"] > 0
    assert any(s["id"] == "mqtt" for s in body["steps"])


def test_boot_status_reports_a_fatal_start(boot_app, boot_state):
    boot_state.set_fatal("optimization_interface", "KeyError: 0")
    with boot_app.test_client() as client:
        body = client.get("/api/boot/status").get_json()
    assert body["fatal"]["component"] == "optimization_interface"


def test_config_api_answers_during_boot(boot_app):
    """The reason the boot server exists: a broken start is still fixable."""
    with boot_app.test_client() as client:
        assert client.get("/api/config/").status_code == 200
        assert client.get("/api/config/schema").status_code == 200
        assert client.get("/api/backup/info").status_code == 200


def test_the_restart_endpoint_is_reachable_during_boot(boot_app):
    """
    The loop #349 leaves open, closed: the start failed on a setting, the boot
    server is still serving the configuration UI, and the restart that applies
    the correction has to be reachable from there too. It rides along on
    config_bp for exactly that reason.
    """
    with boot_app.test_client() as client:
        with patch("src.config_web.api.RESTART", RestartManager(exec_argv=["x"])):
            assert client.post("/api/config/restart").status_code == 202


def test_index_and_assets_are_the_real_ones(boot_app):
    """Same page as the running app — overlay, error panel and log viewer."""
    with boot_app.test_client() as client:
        page = client.get("/")
        assert page.status_code == 200
        assert b"startup-errors-panel" in page.data
        assert client.get("/js/main.js").status_code == 200
        assert client.get("/css/style.css").status_code == 200


def test_log_routes_answer_during_boot(boot_app):
    with boot_app.test_client() as client:
        assert client.get("/logs").status_code == 200
        alerts = client.get("/logs/alerts?startup_only=1").get_json()
        assert alerts["startup_since"] == STARTUP_SINCE
        assert client.post("/logs/clear").status_code == 200


@pytest.mark.parametrize(
    "route",
    [
        "/json/optimize_request.json",
        "/json/optimize_response.json",
        "/json/managed_loads_request.json",
        "/json/price_info.json",
        "/api/update/status",
        "/api/pv_autoscaling/status",
    ],
)
def test_dashboard_endpoints_return_the_starting_up_placeholder(boot_app, route):
    """``data.js:getErrorInfo()`` renders this shape with no frontend change."""
    with boot_app.test_client() as client:
        body = client.get(route).get_json()
    assert body["status"] == STARTING_UP["status"]
    assert body["message"]


def test_current_controls_placeholder_carries_localization(boot_app):
    """
    ``main.js`` reads ``localization.currency`` off this every second. A bare
    status would throw and paint "Connection Error" over a healthy start.
    """
    with boot_app.test_client() as client:
        body = client.get("/json/current_controls.json").get_json()
    assert body["status"] == "starting up"
    assert body["localization"] == {}


def test_unknown_url_still_404s(boot_app):
    """No blanket catch-all: the two apps must agree on what exists."""
    with boot_app.test_client() as client:
        assert client.get("/json/not_a_real_endpoint.json").status_code == 404


def _wsgi_client(wsgi_app):
    """Call a bare WSGI callable and return the body as text."""
    from werkzeug.test import Client  # pylint: disable=import-outside-toplevel

    client = Client(wsgi_app)

    def get(path):
        return client.get(path).get_data(as_text=True)

    return get
