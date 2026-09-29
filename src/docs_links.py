"""Where the documentation lives for the build that is running.

The site is published twice (.github/workflows/pages.yml):

    https://ohand.github.io/EOS_connect/           release docs, from main
    https://ohand.github.io/EOS_connect/develop/   preview docs, from develop

A develop build must link into the preview copy, or its in-app help points at
documentation for a release that does not describe it yet. The two are told apart
by the ``-develop`` suffix CI writes into ``src/version.py``
(``.github/workflows/docker_develop.yml``), which means neither this module nor
its callers need a build flag that would have to be flipped on release.
"""

try:  # running from src/ as a script — src/ is on sys.path
    from version import __version__
except ImportError:  # imported as src.docs_links (tests)
    from .version import __version__

DOCS_BASE_RELEASE = "https://ohand.github.io/EOS_connect/"
DOCS_BASE_PREVIEW = "https://ohand.github.io/EOS_connect/develop/"

# Written by docker_develop.yml; docker_main.yml builds the same version without it.
DEVELOP_SUFFIX = "-develop"


def is_develop_version(version):
    """True for a version string produced by the develop pipeline."""
    return DEVELOP_SUFFIX in (version or "")


def docs_base_url(version):
    """Base URL of the documentation matching ``version``, with a trailing slash."""
    return DOCS_BASE_PREVIEW if is_develop_version(version) else DOCS_BASE_RELEASE


def docs_url(version, path):
    """Absolute URL of a documentation page for ``version``.

    ``path`` is site-relative and must not start with a slash, e.g.
    ``"user-guide/configuration.html#timeseries-templates"``.
    """
    return docs_base_url(version) + path.lstrip("/")


def current_docs_url(path):
    """``docs_url`` for the version this process is running.

    Lets a caller emit a documentation link without having to import the version
    and decide what a develop build means.
    """
    return docs_url(__version__, path)
