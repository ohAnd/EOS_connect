"""
The restart button from issue #336, as a browser actually reaches it.

Three entry points, for three moments a user wants one: the banner that appears
after changing a setting that needs a restart, the settings header (#336 asks
for one that does not depend on anything being pending), and the wizard's
closing screen, whose text has told people to restart for as long as it has
existed without offering a way to.

The request itself must never reach the real RestartManager here — the server
runs in this process, and a flag set on the module singleton would leak into
every later test as a 409.
"""

# pylint: disable=redefined-outer-name

from unittest.mock import patch

import pytest

from src.restart import RestartManager


@pytest.fixture
def restart_manager():
    """A throwaway manager behind the endpoint, so nothing leaks between tests."""
    fresh = RestartManager(exec_argv=["/usr/bin/python3", "/app/eos_connect.py"])
    with patch("src.config_web.api.RESTART", fresh):
        yield fresh


def _open_config(page):
    page.evaluate("() => showConfigurationMenu()")
    page.wait_for_selector(".config-field")


@pytest.mark.browser
def test_the_settings_header_offers_a_restart(page):
    """Always available — #336 wants it without a pending change."""
    _open_config(page)
    button = page.query_selector('button[title="Restart EOS Connect"]')
    assert button is not None
    assert button.is_visible()


@pytest.mark.browser
def test_the_restart_banner_carries_the_button(page):
    """The banner has pointed at a restart for a long time; now it performs one."""
    _open_config(page)
    page.evaluate(
        "() => { configurationManager.restartFields = ['mqtt.broker'];"
        "        configurationManager._showRestartBanner(); }"
    )
    page.wait_for_selector("#cfg-restart-banner.visible")
    button = page.query_selector(".config-restart-banner-btn")
    assert button is not None
    assert "Restart now" in button.inner_text()


@pytest.mark.browser
def test_the_button_asks_before_restarting(page):
    """No window.confirm anywhere in this project; it uses the shared overlay."""
    _open_config(page)
    page.evaluate("() => configurationManager.restartNow('test')")
    page.wait_for_selector("#cfg-restart-confirm", state="visible")
    assert "Cancel" in page.inner_text("#full_screen_content")


@pytest.mark.browser
def test_the_pending_settings_are_named_in_the_confirmation(page):
    """So nobody restarts wondering what it is about to apply."""
    _open_config(page)
    page.evaluate(
        "() => { configurationManager.restartFields = ['mqtt.broker', 'eos.source'];"
        "        configurationManager.restartNow('test'); }"
    )
    page.wait_for_selector("#cfg-restart-confirm", state="visible")
    content = page.inner_text("#full_screen_content")
    assert "mqtt.broker" in content
    assert "eos.source" in content


@pytest.mark.browser
def test_cancelling_restarts_nothing(page, restart_manager):
    _open_config(page)
    page.evaluate("() => configurationManager.restartNow('test')")
    page.wait_for_selector("#cfg-restart-confirm", state="visible")
    page.click("#full_screen_content button:has-text('Cancel')")
    # closeFullScreenOverlay hides the host; it does not empty it.
    page.wait_for_selector("#cfg-restart-confirm", state="hidden")
    assert restart_manager.requested is False


@pytest.mark.browser
def test_confirming_posts_the_restart(page, restart_manager):
    _open_config(page)
    page.evaluate("() => configurationManager.restartNow('applied changed settings')")
    page.wait_for_selector("#cfg-restart-confirm", state="visible")

    with page.expect_response("**/api/config/restart") as response:
        page.click("#cfg-restart-confirm")
    assert response.value.status == 202
    assert restart_manager.requested is True
    assert restart_manager.details["reason"] == "applied changed settings"


@pytest.mark.browser
def test_the_page_says_it_is_restarting_and_watches_for_the_comeback(
    page, restart_manager
):
    """
    Behind Home Assistant ingress the proxy answers 502 while the process is
    away, so the page has to put its own waiting state up before that happens
    and then watch for the application to return.

    ``bootProgress`` latches itself off once the application is up, which is
    what keeps it from polling a dead route forever. Re-arming it is what lets
    the page follow the next start — without it the restart overlay would sit
    there until someone reloaded by hand.

    The overlay element itself is not asserted on: this harness pins it hidden
    with an ``!important`` stylesheet (see conftest._suppress_startup_overlay).
    """
    _open_config(page)
    page.evaluate("() => window.bootProgress._done = true")

    page.evaluate("() => configurationManager.restartNow('test')")
    page.wait_for_selector("#cfg-restart-confirm", state="visible")
    with page.expect_response("**/api/config/restart"):
        page.click("#cfg-restart-confirm")

    page.wait_for_function(
        "() => document.getElementById('waiting_text').innerText.includes('Restarting')"
    )
    assert page.evaluate("() => window.bootProgress._done") is False


@pytest.mark.browser
def test_a_restart_already_running_is_reported_not_repeated(page, restart_manager):
    restart_manager.request("something else", "signal")

    _open_config(page)
    page.evaluate("() => configurationManager.restartNow('test')")
    page.wait_for_selector("#cfg-restart-confirm", state="visible")
    with page.expect_response("**/api/config/restart") as response:
        page.click("#cfg-restart-confirm")

    assert response.value.status == 409
    assert restart_manager.details["reason"] == "something else"


@pytest.mark.browser
def test_the_wizard_closing_screen_offers_a_restart(fresh_page):
    """
    Its text has said "restart EOS Connect" since the wizard existed. The
    manager may never have been created at that point - a first run reaches the
    wizard without ever opening the settings - which is what restartAfterWizard
    handles.
    """
    assert fresh_page.evaluate("() => typeof restartAfterWizard === 'function'")
