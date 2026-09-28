"""The UI's documentation links must follow the build that is running.

The docs are published twice (``.github/workflows/pages.yml``): the released site at
the root and the develop state under ``/develop/``. ``eos_connect.py:main_page``
resolves which applies from ``src/version.py`` and writes it onto
``<body data-docs-base>``; every link in the UI is then built from it by ``docsUrl``
in ``src/web/js/constants.js``.

Nothing asserted on these links before, and they were two hardcoded literals — a
develop build sent users to documentation for a release that did not describe the
feature they were reading about.
"""

import pytest

from src.docs_links import DOCS_BASE_PREVIEW, DOCS_BASE_RELEASE

def _documentation_target(page):
    """The URL the main menu's Documentation entry would open.

    The version argument is deliberately a develop-shaped string in both cases: it
    must not be what decides the link. Only ``data-docs-base`` may.
    """
    page.evaluate("() => showMainMenu('0.0.0-develop', 'eos', 3600)")
    page.wait_for_selector("#main-dropdown-menu")
    return page.evaluate(
        """() => {
            const item = [...document.querySelectorAll('#main-dropdown-menu div')]
                .find(d => d.textContent.trim().startsWith('Documentation'));
            const m = item && item.getAttribute('onclick').match(/window\\.open\\('([^']+)'/);
            return m ? m[1] : null;
        }"""
    )


def test_release_build_links_to_the_published_docs(page):
    """``server`` serves a release base, as a production container would."""
    assert page.evaluate("() => document.body.dataset.docsBase") == DOCS_BASE_RELEASE
    assert _documentation_target(page) == DOCS_BASE_RELEASE


def test_develop_build_links_to_the_preview_docs(develop_page):
    """The same menu entry, on a build whose version carries ``-develop``."""
    assert develop_page.evaluate("() => document.body.dataset.docsBase") == DOCS_BASE_PREVIEW
    assert _documentation_target(develop_page) == DOCS_BASE_PREVIEW


def test_docs_url_helper_joins_paths_against_the_injected_base(develop_page):
    """``docsUrl`` is what every link in the UI goes through."""
    assert develop_page.evaluate("() => docsUrl()") == DOCS_BASE_PREVIEW
    assert develop_page.evaluate("() => docsUrl('user-guide/configuration.html')") == (
        DOCS_BASE_PREVIEW + "user-guide/configuration.html"
    )


def _open_config_help(page):
    """Open the config panel. Every field with a description renders its help text
    and, where the schema gives a ``help_url``, its Learn more link."""
    page.evaluate("() => showConfigurationMenu()")
    page.wait_for_selector(".config-field")
    page.wait_for_selector(".config-help-text a", state="attached")


@pytest.mark.parametrize(
    "fixture_name,expected",
    [("page", DOCS_BASE_RELEASE), ("develop_page", DOCS_BASE_PREVIEW)],
)
def test_learn_more_links_follow_the_build(request, fixture_name, expected):
    """The per-field "Learn more" links are built by config.js from the same base.

    ``help_url`` is read in exactly one place in the frontend, so this one renderer
    covers all 169 schema fields that carry one.
    """
    page = request.getfixturevalue(fixture_name)
    _open_config_help(page)

    hrefs = page.evaluate(
        "() => [...document.querySelectorAll('.config-help-text a')].map(a => a.href)"
    )
    assert hrefs, "no Learn more links rendered"
    for href in hrefs:
        assert href.startswith(expected + "user-guide/"), (
            f"Learn more link {href} does not point into {expected}"
        )
        # The disclosure level is carried across because the docs site cannot read
        # this origin's localStorage (config.js:515-518).
        assert "level=" in href


def test_learn_more_survives_a_dynamic_description_rerender(develop_page):
    """``config.js`` re-renders a dynamic description by preserving the existing <a>.

    It does that with a regex over the help text's innerHTML
    (``config.js:1561``/``:1587``) rather than rebuilding the link, which is why
    those paths need no change — but also why a link could silently vanish there
    without any other test noticing. Only ``price`` and ``pv_forecast_source``
    carry ``description_map`` fields, so the default section cannot exercise this.
    """
    _open_config_help(develop_page)
    develop_page.evaluate("() => configurationManager._selectSection('price')")
    develop_page.wait_for_selector(".config-help-text a", state="attached")

    dynamic_keys = develop_page.evaluate(
        "() => configurationManager._fieldsForSection('price')"
        "        .filter(f => f.description_map && f.help_url).map(f => f.key)"
    )
    assert dynamic_keys, "no dynamic-description field with a help_url to test against"

    def hrefs():
        return develop_page.evaluate(
            "() => [...document.querySelectorAll('.config-help-text a')].map(a => a.href)"
        )

    before = hrefs()
    assert before, "no Learn more links rendered in the price section"
    develop_page.evaluate(
        "() => configurationManager._initializeDynamicDescriptions('price')"
    )
    after = hrefs()

    assert after == before, "a Learn more link was lost or rewritten by the re-render"
    for href in after:
        assert href.startswith(DOCS_BASE_PREVIEW), (
            f"{href} fell back to the released docs after re-rendering"
        )
