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


def export_schema():
    """Export the full config schema to JSON."""
    schema = ConfigSchema()
    data = {
        "fields": schema.to_json(),
        "sections": schema.section_meta(),
    }

    output_dir = os.path.join(project_root, "docs", "assets", "data")
    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, "config_schema.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)

    print(f"Exported {len(data['fields'])} fields to {output_path}")
    return output_path


if __name__ == "__main__":
    export_schema()
