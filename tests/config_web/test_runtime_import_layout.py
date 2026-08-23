"""
Guard against imports that only break outside the test suite.

Every other test imports ``src.config_web``, where ``src`` is a package and a relative
``from ..other_package import x`` resolves fine.  At runtime that is not the layout: the
Dockerfile copies ``src/`` to ``/app`` and the app starts as ``python eos_connect.py``,
so ``config_web``, ``persistence`` and ``interfaces`` are *sibling top-level* packages
and any relative import across them raises ImportError.

That break has happened before — ``config_web/__init__.py`` carries a try/except
fallback for exactly this reason — and the suite structurally cannot see it.  This runs
the real layout in a subprocess instead.
"""

import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src")


def _import_in_runtime_layout(module: str):
    """Import *module* the way the container does: from inside src/, as top-level."""
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=SRC,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_config_web_imports_as_a_top_level_package():
    result = _import_in_runtime_layout("config_web")
    assert result.returncode == 0, result.stderr


def test_persistence_imports_as_a_top_level_package():
    result = _import_in_runtime_layout("persistence")
    assert result.returncode == 0, result.stderr
