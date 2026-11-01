"""
What ``/js/<filename>`` and ``/css/<filename>`` will and will not hand out.

The handler used to stat the joined path itself before delegating to
``send_from_directory``.  That stat was the only place a URL segment reached a
path expression without going through ``safe_join``, and CodeQL flagged it
(``py/path-injection``).  It was never a way out of ``web/`` — the route
converter rejects slashes and ``send_from_directory`` joins safely — so removing
it had to change nothing a client can observe.  These tests pin that down: the
status code, the log line and the refusals are all the same with the stat gone.
"""

# pylint: disable=protected-access,redefined-outer-name

import os

import pytest
from flask import Flask

from src.web_routes import _serve_asset, register_static_routes

BASE_PATH = "src"
#: ``send_from_directory`` resolves a relative directory against the Flask
#: app's ``root_path``, which for an app built in this module is ``tests/`` —
#: the same trap ``register_static_routes`` sidesteps with ``abspath``.
JS_DIR = os.path.abspath(os.path.join(BASE_PATH, "web", "js"))


@pytest.fixture
def client():
    """The asset routes alone — no boot state, no config store needed."""
    app = Flask(__name__)
    register_static_routes(app, BASE_PATH)
    return app.test_client()


def test_real_assets_are_served(client):
    """The happy path both apps depend on."""
    js = client.get("/js/main.js")
    assert js.status_code == 200
    assert js.headers["Content-Type"].startswith("application/javascript")

    css = client.get("/css/style.css")
    assert css.status_code == 200
    assert css.headers["Content-Type"].startswith("text/css")


def test_served_assets_must_revalidate(client):
    """
    An add-on update replaces these files under a browser still holding the old
    page, so the fingerprint in the URL is not allowed to be the only guard.
    """
    headers = client.get("/js/main.js").headers
    assert headers["Cache-Control"] == "no-cache, must-revalidate"
    assert headers["Pragma"] == "no-cache"


def test_missing_asset_404s_and_is_logged(client, caplog):
    """``send_from_directory`` raises ``NotFound``; the handler owns the log."""
    with caplog.at_level("WARNING", logger="__main__"):
        assert client.get("/js/no_such_module.js").status_code == 404
    assert "JavaScript file not found: no_such_module.js" in caplog.text


def test_wrong_suffix_is_refused_before_the_filesystem(client, caplog):
    """
    ``index.html`` sits one level above ``js/`` and is readable; the suffix
    check, not the directory, is what keeps it out of these two routes.
    """
    with caplog.at_level("WARNING", logger="__main__"):
        assert client.get("/js/main.css").status_code == 404
        assert client.get("/css/style.js").status_code == 404
    assert "Blocked attempt to serve non-JavaScript file" in caplog.text
    assert "Blocked attempt to serve non-CSS file" in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "/js/../version.js",
        "/js/../../version.js",
        "/js/..%2fversion.js",
        "/js/%2e%2e%2f%2e%2e%2fversion.js",
        "/js/....//version.js",
        "/js/..%2f..%2fweb%2fjs%2fmain.js",
        "/css/../js/main.css",
        "/js/%2fetc%2fpasswd.js",
    ],
)
def test_traversal_attempts_never_leave_the_asset_directory(client, url):
    """
    Every shape that could walk out of ``web/js`` — bare, encoded, doubled and
    one that would land back on a file that really exists.  A 200 here would
    mean the route serves something the directory does not hold.
    """
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    "filename",
    [
        # Resolves straight back into the asset directory, so the file at the
        # end of it really is readable: a plain ``os.path.join`` would answer
        # 200 here and only ``safe_join`` refuses it.  This is the case that
        # tells the two apart — the rest point at nothing and would 404 either
        # way.
        "../js/main.js",
        "../version.js",
        "../../eos_connect.js",
        "..\\version.js",
        "/etc/passwd.js",
    ],
)
def test_handler_refuses_an_escaping_name_on_its_own(filename):
    """
    The routes above never pass a slash through — Werkzeug's ``string``
    converter stops those at the URL map, so they prove nothing about the
    handler.  Called directly, with the escape already in the name, the handler
    still has to refuse: ``send_from_directory`` joins through ``safe_join``,
    and that is now the only thing between the name and the filesystem.
    """
    app = Flask(__name__)
    with app.test_request_context():
        assert _serve_asset(
            JS_DIR, filename, ".js", "application/javascript", "JS"
        ) == ("Not Found", 404)


def test_handler_serves_a_plain_name():
    """The counterpart: with no escape in it, the same call succeeds."""
    app = Flask(__name__)
    with app.test_request_context():
        response = _serve_asset(
            JS_DIR, "main.js", ".js", "application/javascript", "JS"
        )
    assert response.status_code == 200
