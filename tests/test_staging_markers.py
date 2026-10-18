"""
Keep the ``browser`` marker pointing at the tests it is supposed to cover.

``tests/conftest.py`` marks the browser-driven tests by path, and ``pytest.ini``
leaves that marker out of the default run. Both failure modes of that arrangement are
silent, which is why they are checked here rather than left to be noticed in CI:

* a path in ``BROWSER_PATHS`` is renamed, nothing matches it any more, and 218 tests
  that need Chromium quietly move into the fast lane -- where CI installs no browser;
* a new browser test is added somewhere else, is never marked, and does the same.

The second check deliberately subtracts ``parametrize`` argument names before looking
for fixtures. ``tests/docs/test_docs_integrity.py`` parametrises over a ``page``
argument that is a ``pathlib.Path``, and parametrised arguments are indistinguishable
from fixtures in a bare signature -- taking them at face value would condemn 136
static tests that run in 0.06s to the browser job.
"""

import ast
from pathlib import Path

import pytest

from tests.conftest import BROWSER_PATHS

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

# The fixtures that hand a test a real Chromium, from tests/web/conftest.py and
# tests/docs/conftest.py (plus the module-level ``page`` in test_docs_rendering.py).
BROWSER_FIXTURES = {"browser", "page", "fresh_page", "develop_page"}


def _test_modules():
    return sorted(TESTS.rglob("test_*.py"))


def _parametrized_names(node):
    """Argument names this test gets from ``parametrize``, not from a fixture."""
    names = set()
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        target = decorator.func
        if not (isinstance(target, ast.Attribute) and target.attr == "parametrize"):
            continue
        argnames = decorator.args[0]
        if isinstance(argnames, ast.Constant) and isinstance(argnames.value, str):
            names.update(part.strip() for part in argnames.value.split(","))
    return names


def _browser_tests_in(path):
    """Names of the tests in *path* that actually request a browser fixture."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        params = {a.arg for a in node.args.args} - _parametrized_names(node)
        if params & BROWSER_FIXTURES:
            found.append(node.name)
    return found


def _is_marked(path):
    nodeid = path.relative_to(REPO).as_posix()
    return any(marked in nodeid for marked in BROWSER_PATHS)


@pytest.mark.parametrize("marked", BROWSER_PATHS)
def test_every_marked_path_still_exists(marked):
    """A rename must not silently empty the browser marker."""
    target = REPO / marked
    assert target.exists(), (
        f"tests/conftest.py marks {marked!r} as browser-driven, but nothing is there. "
        "Renaming it leaves those tests unmarked, so they run in the fast lane - "
        "where CI installs no Chromium."
    )


@pytest.mark.parametrize(
    "module", _test_modules(), ids=lambda p: p.relative_to(TESTS).as_posix()
)
def test_browser_tests_live_under_a_marked_path(module):
    """Every test that asks for a browser must be somewhere the marker reaches."""
    using = _browser_tests_in(module)
    if not using:
        return
    assert _is_marked(module), (
        f"{module.relative_to(REPO).as_posix()} requests browser fixtures "
        f"({', '.join(sorted(using))}) but is not covered by BROWSER_PATHS in "
        "tests/conftest.py, so it would not be marked 'browser' - it would run in "
        "the default lane and fail where no Chromium is installed."
    )


def test_the_marker_actually_covers_something():
    """Guard the guard: if nothing matches, both checks above pass vacuously."""
    covered = [m for m in _test_modules() if _is_marked(m)]
    assert len(covered) >= 10, (
        f"only {len(covered)} test modules fall under BROWSER_PATHS; the browser "
        "marker has probably stopped matching."
    )
