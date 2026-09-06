"""Sample history and persisted calibration for managed loads."""

from datetime import datetime, timedelta, timezone

import pytest

from src.config_web.store import ConfigStore
from src.persistence import ManagedLoadStore

NOW = datetime.now(timezone.utc)


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    config_store = ConfigStore(str(tmp_path / "config.db"))
    config_store.open()
    load_store = ManagedLoadStore(config_store)
    load_store.ensure_schema()
    try:
        yield load_store
    finally:
        config_store.close()


def _sample(minutes_ago=0, **overrides):
    sample = {
        "timestamp": NOW - timedelta(minutes=minutes_ago),
        "medium_c": 26.0,
        "ambient_c": 18.0,
        "power_w": 0.0,
        "cover_factor": 1.0,
    }
    sample.update(overrides)
    return sample


def test_ensure_schema_is_idempotent(store):
    store.ensure_schema()
    store.ensure_schema()
    assert store.sample_count("pool") == 0


def test_samples_round_trip_with_their_payload(store):
    store.record_sample("pool", _sample(medium_c=27.5, power_w=1800.0))
    loaded = store.load_samples("pool")

    assert len(loaded) == 1
    assert loaded[0]["medium_c"] == 27.5
    assert loaded[0]["power_w"] == 1800.0
    assert isinstance(loaded[0]["timestamp"], datetime)
    assert loaded[0]["timestamp"].tzinfo is not None


def test_samples_come_back_oldest_first(store):
    """The calibrator pairs consecutive samples, so the order is not cosmetic."""
    for minutes in (60, 15, 45, 30):
        store.record_sample("pool", _sample(minutes_ago=minutes, medium_c=minutes))
    loaded = store.load_samples("pool")
    assert [s["medium_c"] for s in loaded] == [60, 45, 30, 15]


def test_samples_are_scoped_per_load(store):
    store.record_sample("pool", _sample())
    store.record_sample("sauna", _sample())
    assert len(store.load_samples("pool")) == 1
    assert len(store.load_samples("sauna")) == 1


def test_a_sample_without_a_timestamp_is_refused(store):
    assert store.record_sample("pool", {"medium_c": 20.0}) is False
    assert store.sample_count("pool") == 0


def test_a_naive_timestamp_is_stored_as_utc(store):
    store.record_sample("pool", {"timestamp": datetime(2026, 6, 1, 12, 0), "medium_c": 20.0})
    loaded = store.load_samples("pool", days=100000)
    assert loaded[0]["timestamp"].tzinfo is not None


def test_the_retention_window_bounds_what_is_loaded(store):
    store.record_sample("pool", _sample(minutes_ago=60 * 24 * 30))
    store.record_sample("pool", _sample(minutes_ago=10))
    assert len(store.load_samples("pool", days=14)) == 1


def test_purging_removes_only_what_is_outside_the_window(store):
    store.record_sample("pool", _sample(minutes_ago=60 * 24 * 30))
    store.record_sample("pool", _sample(minutes_ago=10))

    removed = store.purge_old_samples(days=14)
    assert removed == 1
    assert store.sample_count("pool") == 1


def test_an_unreadable_payload_is_skipped_not_fatal(store):
    """A row written by an older version is not worth losing the other 4000 over."""
    store.record_sample("pool", _sample())
    store._store.execute(  # pylint: disable=protected-access
        "INSERT INTO managed_load_samples (load_id, timestamp, payload) VALUES (?,?,?)",
        ("pool", NOW.isoformat(), "{not json"),
    )
    assert len(store.load_samples("pool")) == 1


def test_model_state_round_trips(store):
    state = {"loss_coefficient": 23.4, "cop_nominal": 4.8, "confidence": 0.62}
    assert store.save_model_state("pool", state) is True
    assert store.load_model_state("pool") == state


def test_saving_model_state_twice_replaces_it(store):
    store.save_model_state("pool", {"loss_coefficient": 10.0})
    store.save_model_state("pool", {"loss_coefficient": 20.0})
    assert store.load_model_state("pool")["loss_coefficient"] == 20.0


def test_missing_model_state_is_none(store):
    assert store.load_model_state("nobody") is None


@pytest.mark.parametrize("bad", [None, "text", 42, [1, 2]])
def test_unserialisable_model_state_is_refused(store, bad):
    assert store.save_model_state("pool", bad) is False


def test_forget_removes_both_samples_and_state(store):
    store.record_sample("pool", _sample())
    store.save_model_state("pool", {"loss_coefficient": 10.0})

    store.forget("pool")
    assert store.sample_count("pool") == 0
    assert store.load_model_state("pool") is None
