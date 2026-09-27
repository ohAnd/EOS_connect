"""The documentation base URL must follow the build that is running.

The docs are published twice (``.github/workflows/pages.yml``): the released site at
the root, the develop state under ``/develop/``. A develop build linking into the
released site sends the user to documentation that predates the feature they are
looking at, which is the bug this module exists to prevent.
"""

import pytest

from src.docs_links import (
    DOCS_BASE_PREVIEW,
    DOCS_BASE_RELEASE,
    docs_base_url,
    docs_url,
    is_develop_version,
)

# Shapes produced by the two pipelines: docker_main.yml builds "0.3.<run>", and
# docker_develop.yml appends "-develop" to "0.3.39.<run>".
RELEASE_VERSIONS = ["0.3.345", "0.3.39", "1.0.0"]
DEVELOP_VERSIONS = ["0.3.39.345-develop", "0.3.39.1-develop"]


@pytest.mark.parametrize("version", DEVELOP_VERSIONS)
def test_develop_build_links_to_the_preview_site(version):
    assert is_develop_version(version) is True
    assert docs_base_url(version) == DOCS_BASE_PREVIEW


@pytest.mark.parametrize("version", RELEASE_VERSIONS)
def test_release_build_links_to_the_published_site(version):
    assert is_develop_version(version) is False
    assert docs_base_url(version) == DOCS_BASE_RELEASE


@pytest.mark.parametrize("version", [None, ""])
def test_missing_version_falls_back_to_the_release_site(version):
    """A build with no version must not send users to preview documentation."""
    assert docs_base_url(version) == DOCS_BASE_RELEASE


def test_preview_base_is_the_release_base_plus_one_segment():
    """The banner in docs/assets/js/site.js keys off exactly this path segment."""
    assert DOCS_BASE_PREVIEW == DOCS_BASE_RELEASE + "develop/"


@pytest.mark.parametrize("base", [DOCS_BASE_RELEASE, DOCS_BASE_PREVIEW])
def test_bases_end_in_a_slash(base):
    """``docsUrl`` in src/web/js/constants.js concatenates without inserting one."""
    assert base.endswith("/")


def test_docs_url_joins_without_doubling_the_slash():
    assert docs_url("0.3.345", "user-guide/configuration.html#load") == (
        DOCS_BASE_RELEASE + "user-guide/configuration.html#load"
    )
    # Tolerated so a caller that writes a leading slash does not produce "//".
    assert docs_url("0.3.345", "/user-guide/index.html") == (
        DOCS_BASE_RELEASE + "user-guide/index.html"
    )


def test_update_checker_agrees_on_what_a_develop_build_is():
    """``UpdateChecker`` used to inline this test; both must stay in step, or the
    update banner and the documentation links would disagree about the same build."""
    from src.interfaces.update_checker import UpdateChecker

    for version in DEVELOP_VERSIONS + RELEASE_VERSIONS:
        checker = UpdateChecker(current_version=version)
        assert checker.is_develop is is_develop_version(version)


def test_timeseries_error_points_at_a_resolvable_page():
    """The message used to carry a bare "configuration.html#…", which named no site."""
    from src.interfaces.timeseries_normalizer import TEMPLATE_DOCS_URL

    assert TEMPLATE_DOCS_URL.startswith(("https://", "http://"))
    assert TEMPLATE_DOCS_URL.endswith("user-guide/configuration.html#timeseries-templates")
