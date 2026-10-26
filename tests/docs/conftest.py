"""Server fixtures for the documentation tests.

The Chromium itself comes from the shared ``browser`` fixture in
``tests/conftest.py`` — there has to be exactly one ``sync_playwright()`` per
process, and two directories each keeping their own broke a run that covered both.

Playwright and a Chromium build are not in ``requirements.txt``, so
``test_docs_rendering.py`` is skipped when the package is missing; it is also
marked ``browser`` and left out of the default run, and CI gives it a job of its
own with a browser installed.  ``test_docs_integrity.py`` is static and always runs.

The docs are served over HTTP rather than opened as ``file://`` because
``configuration.html`` fetches ``assets/data/config_schema.json``, and the file
protocol is treated as an opaque origin — the fetch would be blocked and the
whole parameter reference would be missing.
"""

import functools
import http.server
import os
import shutil
import threading

import pytest

try:
    import playwright.sync_api  # noqa: F401  pylint: disable=unused-import
except ImportError:  # pragma: no cover - depends on the environment
    collect_ignore_glob = ["test_docs_rendering.py"]

DOCS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs"
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler without the per-request logging."""

    def log_message(self, *args):
        pass


@pytest.fixture(name="docs_url", scope="session")
def docs_url_fixture():
    """Serve docs/ on a loopback port for the duration of the session.

    Port 0 lets the OS choose and the server keeps the socket it was handed.
    Picking a free port first, closing it and binding it again leaves a window
    in between for anything else on the machine to take it.
    """
    handler = functools.partial(_QuietHandler, directory=DOCS_DIR)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(name="preview_docs_url", scope="session")
def preview_docs_url_fixture(tmp_path_factory):
    """Serve the docs the way ``.github/workflows/pages.yml`` publishes the preview.

    The develop copy lives one segment down, under ``/develop/``, and that segment is
    the only thing ``site.js`` has to go on when it decides to raise the banner. A
    fixture that served ``docs/`` at the root could not exercise that at all.

    ``build-info.json`` is deliberately *not* staged: the workflow writes it, so the
    banner has to render without it here, which is also what happens for anyone
    opening the docs from a checkout.
    """
    root = tmp_path_factory.mktemp("pages-site")
    shutil.copytree(DOCS_DIR, os.path.join(str(root), "develop"))

    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/develop/"
    finally:
        server.shutdown()
        server.server_close()
