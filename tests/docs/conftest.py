"""Browser fixtures for the documentation tests.

Same constraints as ``tests/web``: Playwright and a Chromium build are not in
``requirements.txt`` and CI has no browser, so the browser-driven module is
skipped when either is missing.  ``test_docs_integrity.py`` is static and always
runs.

The docs are served over HTTP rather than opened as ``file://`` because
``configuration.html`` fetches ``assets/data/config_schema.json``, and the file
protocol is treated as an opaque origin — the fetch would be blocked and the
whole parameter reference would be missing.
"""

import functools
import http.server
import os
import socket
import threading

import pytest

try:
    import playwright.sync_api  # noqa: F401  pylint: disable=unused-import
except ImportError:  # pragma: no cover - depends on the environment
    collect_ignore_glob = ["test_docs_rendering.py"]

# Shared libraries unpacked by scripts/install_playwright_deps_userspace.sh, for
# hosts where Chromium's dependencies cannot be apt-installed.
_USER_LIBS = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright-deps", "lib")
if os.path.isdir(_USER_LIBS):
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
        [_USER_LIBS, os.environ.get("LD_LIBRARY_PATH", "")]
    ).rstrip(os.pathsep)

DOCS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs"
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler without the per-request logging."""

    def log_message(self, *args):
        pass


@pytest.fixture(name="docs_url", scope="session")
def docs_url_fixture():
    """Serve docs/ on a loopback port for the duration of the session."""
    port = _free_port()
    handler = functools.partial(_QuietHandler, directory=DOCS_DIR)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(name="browser")
def browser_fixture():
    """Chromium for one test, following the pattern in ``tests/web/conftest.py``.

    Deliberately function-scoped. Holding ``sync_playwright()`` open across the
    session leaves an asyncio event loop in place that later async tests pick
    up, which breaks ``tests/interfaces/test_pv_interface.py`` with "coroutine
    was never awaited". Opening and closing it per test keeps the suite
    order-independent.
    """
    # Imported here, not at module scope: the module is skipped when Playwright
    # is absent, and a top-level import would run before that check.
    from playwright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

    with sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        # Playwright raises a variety of errors for a browser that will not
        # start (missing libraries, no sandbox, out of memory); all mean "skip".
        except Exception as exc:  # pylint: disable=broad-exception-caught
            pytest.skip(f"Chromium could not be launched: {exc}")
        try:
            yield browser
        finally:
            browser.close()
