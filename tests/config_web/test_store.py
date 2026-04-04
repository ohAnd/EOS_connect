"""
Unit tests for the SQLite Config Store.
"""

import os
import tempfile
import pytest
from src.config_web.store import ConfigStore


@pytest.fixture
def store(tmp_path):
    """Create a temporary ConfigStore for each test."""
    db_path = os.path.join(str(tmp_path), "test.db")
    s = ConfigStore(db_path)
    s.open()
    yield s
    s.close()


class TestConfigStore:
    """Tests for ConfigStore class."""

    def test_open_creates_file(self, tmp_path):
        """Opening a store should create the database file."""
        db_path = os.path.join(str(tmp_path), "sub", "test.db")
        s = ConfigStore(db_path)
        s.open()
        assert os.path.exists(db_path)
        s.close()

    def test_set_and_get(self, store):
        """Should store and retrieve values."""
        store.set("test.key", "hello")
        assert store.get("test.key") == "hello"

    def test_get_default(self, store):
        """Should return default for missing keys."""
        assert store.get("missing", "fallback") == "fallback"
        assert store.get("also_missing") is None

    def test_set_overwrites(self, store):
        """Setting the same key twice should overwrite."""
        store.set("k", 1)
        store.set("k", 2)
        assert store.get("k") == 2

    def test_stores_various_types(self, store):
        """Should handle int, float, bool, str, list, dict."""
        store.set("int_val", 42)
        store.set("float_val", 3.14)
        store.set("bool_val", True)
        store.set("str_val", "hello")
        store.set("list_val", [1, 2, 3])
        store.set("dict_val", {"a": 1})

        assert store.get("int_val") == 42
        assert store.get("float_val") == 3.14
        assert store.get("bool_val") is True
        assert store.get("str_val") == "hello"
        assert store.get("list_val") == [1, 2, 3]
        assert store.get("dict_val") == {"a": 1}

    def test_delete(self, store):
        """Should delete a key and return True, or False if missing."""
        store.set("to_delete", "val")
        assert store.delete("to_delete") is True
        assert store.get("to_delete") is None
        assert store.delete("nonexistent") is False

    def test_get_all(self, store):
        """Should return all stored key/value pairs."""
        store.set("a", 1)
        store.set("b", 2)
        all_data = store.get_all()
        assert all_data == {"a": 1, "b": 2}

    def test_is_empty(self, store):
        """Should report empty/non-empty correctly."""
        assert store.is_empty() is True
        store.set("k", "v")
        assert store.is_empty() is False

    def test_has_key(self, store):
        """Should check key existence."""
        assert store.has_key("nope") is False
        store.set("nope", "yes")
        assert store.has_key("nope") is True

    def test_import_dict(self, store):
        """Bulk import should store all entries."""
        data = {"x.a": 1, "x.b": 2, "y": "hello"}
        count = store.import_dict(data)
        assert count == 3
        assert store.get("x.a") == 1
        assert store.get("y") == "hello"

    def test_export_dict(self, store):
        """Export should return all stored data."""
        store.set("m", 10)
        store.set("n", 20)
        exported = store.export_dict()
        assert exported == {"m": 10, "n": 20}

    def test_change_callback(self, store):
        """Change callbacks should fire when a value changes."""
        changes = []
        store.register_change_callback(
            lambda key, old, new: changes.append((key, old, new))
        )

        store.set("cb_key", "val1")
        assert len(changes) == 1
        assert changes[0] == ("cb_key", None, "val1")

    def test_set_batch_basic(self, store):
        """set_batch should atomically write multiple keys."""
        count = store.set_batch({"x": 1, "y": "two", "z": [3]})
        assert count == 3
        assert store.get("x") == 1
        assert store.get("y") == "two"
        assert store.get("z") == [3]

    def test_set_batch_empty(self, store):
        """set_batch with empty dict writes nothing."""
        count = store.set_batch({})
        assert count == 0
        assert store.is_empty()

    def test_set_batch_overwrite(self, store):
        """set_batch should overwrite existing keys."""
        store.set("x", "old")
        store.set_batch({"x": "new", "y": 42})
        assert store.get("x") == "new"
        assert store.get("y") == 42

    def test_change_callback_multiple(self, store):
        """Change callbacks should fire on subsequent changes."""
        changes = []
        store.register_change_callback(
            lambda key, old, new: changes.append((key, old, new))
        )
        store.set("cb_key", "val1")
        store.set("cb_key", "val2")
        assert len(changes) == 2
        assert changes[1] == ("cb_key", "val1", "val2")

    def test_callback_not_fired_on_same_value(self, store):
        """Callback should NOT fire if value doesn't actually change."""
        changes = []
        store.register_change_callback(
            lambda key, old, new: changes.append((key, old, new))
        )

        store.set("same", 42)
        store.set("same", 42)  # same value again
        assert len(changes) == 1  # only the first set fires

    def test_callback_error_doesnt_crash(self, store):
        """A failing callback should not prevent storage."""
        def bad_callback(key, old, new):
            raise ValueError("boom")

        store.register_change_callback(bad_callback)
        store.set("safe_key", "value")
        assert store.get("safe_key") == "value"
