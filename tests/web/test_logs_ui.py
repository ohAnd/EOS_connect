"""
Browser test for the Logs panel's layout.

The panel's content is covered by the logging API tests.  What is checked here is the
filter row: its input is ``width: 100%`` with padding and a border, so without
``box-sizing: border-box`` it rendered 18px wider than the column it sits in and pushed
a horizontal scrollbar onto the whole panel on every phone.
"""

import json

import pytest

PHONE = {"width": 402, "height": 874}
DESKTOP = {"width": 1920, "height": 1080}

# Two entries is enough to render the list; the filter row is what is under test.
_LOGS = {
    "logs": [
        {
            "timestamp": "2026-09-05T10:44:54",
            "level": "DEBUG",
            "component": "Main",
            "message": "Memory logging initialised",
        },
        {
            "timestamp": "2026-09-05T10:44:55",
            "level": "ERROR",
            "component": "Interface",
            "message": "a message long enough to need wrapping on a narrow screen " * 3,
        },
    ]
}


def _open_logs(page, viewport):
    # The panel fetches ``logs?limit=…`` relative to the page — not under ``/api``,
    # which is the only prefix the harness stubs, so it 404s without this.
    page.route(
        "**/logs?*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_LOGS)
        ),
    )
    for endpoint in ("**/logs/stats", "**/logs/alerts"):
        page.route(
            endpoint,
            lambda route: route.fulfill(
                status=200, content_type="application/json", body="{}"
            ),
        )
    page.set_viewport_size(viewport)
    page.wait_for_function("typeof showLogsMenu === 'function'")
    page.evaluate("() => showLogsMenu()")
    page.wait_for_selector("#text-filter-input")


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_nothing_overflows_horizontally(page, viewport):
    """Guards the 18px the filter input used to add to its own column."""
    _open_logs(page, viewport)

    overflowing = page.evaluate(
        """() => [...document.getElementById('full_screen_content').querySelectorAll('*')]
              .filter(e => e.scrollWidth > e.clientWidth + 1)
              .map(e => e.tagName + '#' + e.id)"""
    )
    assert overflowing == []


def test_the_filter_input_fits_its_column_on_a_phone(page):
    """It should fill the width, not exceed it — the bug was 100% *plus* padding."""
    _open_logs(page, PHONE)

    box = page.eval_on_selector(
        "#text-filter-input",
        "e => ({ input: e.getBoundingClientRect().width,"
        "        column: e.parentElement.getBoundingClientRect().width })",
    )
    assert box["input"] <= box["column"] + 1
    assert box["input"] > box["column"] * 0.95
