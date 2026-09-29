"""
Export Config Schema to JSON — Generates docs/assets/data/config_schema.json.

This script is the bridge between the Python schema registry (single source
of truth) and the GitHub Pages documentation. Run it whenever the schema
changes to keep the docs reference tables in sync.

Usage::

    python scripts/export_config_schema.py
"""

import json
import os
import sys

# Add project root to path so we can import from src
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.config_web.schema import ConfigSchema  # noqa: E402
from src.loads import presets  # noqa: E402


def managed_load_presets():
    """The per-type starting values, as JSON.

    A managed load's schema default only mirrors the pool heat pump - the field is
    shared by all four heated stores and a FieldDef holds one default. What a sauna
    actually starts from lives in src/loads/presets.py, so the documentation has to
    read it from there or it would tell a sauna owner to aim for 28 C.

    ``model`` is a class and is dropped; a ``None`` in ``defaults`` is kept, because
    it means "no limit" rather than "unset" (a sauna has no allowed window).
    """
    return {
        name: {
            "label": preset["label"],
            "ambient": preset["ambient"],
            "default_ambient_c": preset["default_ambient_c"],
            "defaults": dict(preset["defaults"]),
        }
        for name, preset in presets.PRESETS.items()
    }


def managed_load_groups():
    """Which types each group of fields applies to.

    ``depends_on`` on the fields already says this, but only field by field. The
    documentation groups whole profiles, so it needs the groupings themselves.
    """
    return {
        "thermal": list(presets.THERMAL_TYPES),
        "contingent": list(presets.CONTINGENT_TYPES),
        "external": list(presets.EXTERNAL_TYPES),
        "cover": list(presets.COVER_TYPES),
        "season": list(presets.SEASON_TYPES),
    }


def export_schema():
    """Export the full config schema to JSON."""
    schema = ConfigSchema()
    data = {
        "fields": schema.to_json(),
        "sections": schema.section_meta(),
        "managed_load_presets": managed_load_presets(),
        "managed_load_groups": managed_load_groups(),
    }

    output_dir = os.path.join(project_root, "docs", "assets", "data")
    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, "config_schema.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)

    print(
        f"Exported {len(data['fields'])} fields and "
        f"{len(data['managed_load_presets'])} managed-load presets to {output_path}"
    )
    return output_path


if __name__ == "__main__":
    export_schema()
