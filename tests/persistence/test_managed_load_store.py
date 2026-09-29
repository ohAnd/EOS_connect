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


# --- the decision journal ------------------------------------------------------------

class TestDecisionJournal:
    """
    What was decided, and against which plan.

    Written because a toggling appliance could not be explained after the fact. The
    release signal says only that it changed; by the time anyone looks, the plan that
    said so has been replaced several times over.
    """

    def _record(self, store, moment, **overrides):
        record = {
            "origin": "optimizer", "released": True, "reason": "planned cheap slot",
            "current_slot": 76, "planned_now": True, "plan_hash": "abc12345",
            "planned_slots": [76, 77], "planned_wh": 800.0, "demand_wh": 35733.5,
        }
        record.update(overrides)
        return store.record_decision("pool", moment, record)

    def test_a_decision_round_trips(self, store):
        now = datetime.now(timezone.utc)
        assert self._record(store, now) is True

        rows = store.load_decisions("pool")
        assert len(rows) == 1
        row = rows[0]
        assert row["released"] is True
        assert row["reason"] == "planned cheap slot"
        assert row["current_slot"] == 76
        assert row["planned_now"] is True
        assert row["plan_hash"] == "abc12345"
        assert row["planned_slots"] == [76, 77]
        assert row["demand_wh"] == 35733.5

    def test_the_newest_comes_first(self, store):
        now = datetime.now(timezone.utc)
        for minutes, plan in ((30, "aaa"), (20, "bbb"), (10, "ccc")):
            self._record(store, now - timedelta(minutes=minutes), plan_hash=plan)

        assert [r["plan_hash"] for r in store.load_decisions("pool")] == \
            ["ccc", "bbb", "aaa"]

    def test_a_plan_that_keeps_moving_is_visible_as_distinct_hashes(self, store):
        """The whole point: four plans in seven minutes has to be countable."""
        now = datetime.now(timezone.utc)
        for index, plan in enumerate(["aaa", "bbb", "aaa", "ccc", "ddd", "ccc"]):
            self._record(store, now - timedelta(minutes=index), plan_hash=plan)

        hashes = {r["plan_hash"] for r in store.load_decisions("pool")}
        assert hashes == {"aaa", "bbb", "ccc", "ddd"}

    def test_only_the_window_asked_for_comes_back(self, store):
        now = datetime.now(timezone.utc)
        self._record(store, now - timedelta(hours=30), plan_hash="old")
        self._record(store, now - timedelta(hours=2), plan_hash="new")

        assert [r["plan_hash"] for r in store.load_decisions("pool", hours=24)] == ["new"]

    def test_the_limit_keeps_the_recent_end(self, store):
        now = datetime.now(timezone.utc)
        for index in range(10):
            self._record(store, now - timedelta(minutes=index),
                         plan_hash=f"h{index:02d}")

        rows = store.load_decisions("pool", limit=3)
        assert [r["plan_hash"] for r in rows] == ["h00", "h01", "h02"]

    def test_decisions_are_kept_per_load(self, store):
        now = datetime.now(timezone.utc)
        self._record(store, now)
        store.record_decision("sauna", now, {"origin": "self", "released": False,
                                             "reason": "no demand"})

        assert len(store.load_decisions("pool")) == 1
        assert len(store.load_decisions("sauna")) == 1

    def test_a_record_with_no_timestamp_is_refused(self, store):
        assert store.record_decision("pool", "not a datetime", {}) is False

    def test_old_decisions_are_purged(self, store):
        now = datetime.now(timezone.utc)
        self._record(store, now - timedelta(days=9))
        self._record(store, now)

        assert store.purge_old_decisions(days=7) == 1
        assert len(store.load_decisions("pool", hours=168)) == 1

    def test_forgetting_a_load_takes_its_journal_too(self, store):
        """The id is the user's - the next appliance under that name is not this one."""
        self._record(store, datetime.now(timezone.utc))
        store.forget("pool")

        assert store.load_decisions("pool") == []

    def test_the_journal_is_separate_from_the_calibration_samples(self, store):
        """A calibrator sifting control records out of its own history forever."""
        now = datetime.now(timezone.utc)
        self._record(store, now)
        store.record_sample("pool", {"timestamp": now, "medium_c": 25.1,
                                     "ambient_c": 17.2, "power_w": 1600.0})

        assert len(store.load_samples("pool")) == 1
        assert len(store.load_decisions("pool")) == 1
