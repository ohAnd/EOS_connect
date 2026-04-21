"""
Tests for the config schema export script.

Verifies that the export script produces valid JSON that matches
the schema registry, and that the exported file is consumable by
the GitHub Pages documentation.
"""

import json
import os
import tempfile

import pytest

from src.config_web.schema import ConfigSchema


class TestExportSchema:
    """Tests for the schema export pipeline."""

    def setup_method(self):
        """Create a fresh schema instance."""
        self.schema = ConfigSchema()

    def test_exported_json_exists(self):
        """The exported JSON file should exist in docs/assets/data/."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        json_path = os.path.join(
            project_root, "docs", "assets", "data", "config_schema.json"
        )
        assert os.path.exists(json_path), (
            f"config_schema.json not found at {json_path}. "
            "Run: python scripts/export_config_schema.py"
        )

    def test_exported_json_is_valid(self):
        """The exported JSON should be parseable and non-empty."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        json_path = os.path.join(
            project_root, "docs", "assets", "data", "config_schema.json"
        )
        if not os.path.exists(json_path):
            pytest.skip("config_schema.json not found; run export script first")

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        assert isinstance(data, dict)
        assert "fields" in data
        assert "sections" in data
        assert len(data["fields"]) > 50

    def test_exported_json_matches_schema(self):
        """Every field in the schema registry should appear in the export."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        json_path = os.path.join(
            project_root, "docs", "assets", "data", "config_schema.json"
        )
        if not os.path.exists(json_path):
            pytest.skip("config_schema.json not found; run export script first")

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        fields = data["fields"] if isinstance(data, dict) else data
        exported_keys = {entry["key"] for entry in fields}
        schema_keys = {f.key for f in self.schema.all_fields()}

        assert exported_keys == schema_keys, (
            f"Mismatch between export and schema. "
            f"Missing from export: {schema_keys - exported_keys}. "
            f"Extra in export: {exported_keys - schema_keys}"
        )

    def test_exported_fields_have_required_keys(self):
        """Each exported field must have the keys needed by the docs JS."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        json_path = os.path.join(
            project_root, "docs", "assets", "data", "config_schema.json"
        )
        if not os.path.exists(json_path):
            pytest.skip("config_schema.json not found; run export script first")

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        fields = data["fields"] if isinstance(data, dict) else data
        required_keys = {"key", "type", "default", "section", "level", "description"}
        for entry in fields:
            missing = required_keys - set(entry.keys())
            assert not missing, (
                f"Field '{entry.get('key', '?')}' missing keys: {missing}"
            )

    def test_export_roundtrip(self):
        """to_json() output should survive JSON serialization roundtrip."""
        schema_json = self.schema.to_json()
        serialized = json.dumps(schema_json, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert len(deserialized) == len(schema_json)
        assert deserialized[0]["key"] == schema_json[0]["key"]

    def test_section_meta_covers_all_sections(self):
        """SECTION_META must have an entry for every section defined in schema fields."""
        from src.config_web.schema import SECTION_META
        schema_sections = set(self.schema.sections())
        meta_sections = set(SECTION_META.keys())
        missing = schema_sections - meta_sections
        assert not missing, (
            f"SECTION_META missing entries for sections: {missing}"
        )

    def test_section_meta_has_icon_and_label(self):
        """Every SECTION_META entry must have both 'icon' and 'label'."""
        from src.config_web.schema import SECTION_META
        for section, meta in SECTION_META.items():
            assert "icon" in meta, f"SECTION_META['{section}'] missing 'icon'"
            assert "label" in meta, f"SECTION_META['{section}'] missing 'label'"

    def test_exported_json_includes_sections(self):
        """The exported JSON should contain section metadata."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        json_path = os.path.join(
            project_root, "docs", "assets", "data", "config_schema.json"
        )
        if not os.path.exists(json_path):
            pytest.skip("config_schema.json not found; run export script first")

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        assert "sections" in data
        assert isinstance(data["sections"], dict)
        assert "data_source" in data["sections"]
        assert data["sections"]["data_source"]["icon"] == "fa-plug"
