"""
Import-layer guards for the `loads` package.

Two properties the rest of the suite structurally cannot see:

- the core must not pull in Flask, because it runs on the optimizer path and inside the
  poll thread, and `persistence` was already kept out of `config_web` for this reason;
- at runtime `loads` is a *sibling* top-level package, not `src.loads`, so any relative
  import reaching across to `interfaces` or `config_web` raises only in the container.

The second mirrors `tests/config_web/test_runtime_import_layout.py`.
"""

import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src")

CORE_MODULES = (
    "loads",
    "loads.manager",
    "loads.instance",
    "loads.planner",
    "loads.gate",
    "loads.presets",
    "loads.injection",
    "loads.models.thermal",
    "loads.models.external",
)


def _run(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SRC, capture_output=True, text=True, check=False, timeout=120,
    )


def test_the_package_imports_in_the_runtime_layout():
    """`loads` must import with src/ as the root, as the container runs it."""
    result = _run("import loads")
    assert result.returncode == 0, result.stderr


def test_every_core_module_imports_in_the_runtime_layout():
    result = _run("; ".join(f"import {name}" for name in CORE_MODULES))
    assert result.returncode == 0, result.stderr


def test_the_core_does_not_drag_in_flask():
    """
    The manager runs on the optimizer path and in a poll thread; a web framework has no
    business there, and importing one would also break a headless import of the package.
    """
    result = _run(
        "import sys; "
        + "; ".join(f"import {name}" for name in CORE_MODULES)
        + "; assert 'flask' not in sys.modules, sorted(m for m in sys.modules "
          "if 'flask' in m)"
    )
    assert result.returncode == 0, result.stderr


def test_the_core_does_not_import_config_web_or_interfaces():
    """`loads` receives plain dicts and injected callables - nothing more."""
    result = _run(
        "import sys; "
        + "; ".join(f"import {name}" for name in CORE_MODULES)
        + "; leaked = [m for m in sys.modules if m.split('.')[0] in "
          "('config_web', 'interfaces')]; assert not leaked, leaked"
    )
    assert result.returncode == 0, result.stderr
