"""
Can a PV installation actually be removed?

The store is a flat key/value table and a save only ever upserted, so a removed entry
had nowhere to be expressed. The client dropped its keys locally and sent what was
left, and the row survived:

  * removing the **last** entry changed no remaining value, so the diff was empty and
    the save reported "no changes" while the card was already gone from the screen;
  * removing an **earlier** one re-indexed the survivors over the top, but the highest
    index stayed behind - the list kept its length and its final entry became a
    duplicate of its neighbour.

The request now declares how long the list is, and the server drops the rest. Declared
rather than inferred: treating any request carrying ``pv_forecast.*`` as the whole
truth would let a one-field PATCH from a script delete every other entry.
"""

from tests.config_web.test_api import client_fixture  # noqa: F401  (fixture import)


def _entries(client):
    return client.get("/api/config/").get_json()["pv_forecast"]


def _with_two(client):
    """
    The fixture ships one installation; a removal test needs something to remove.

    Only fields the schema knows: a stored entry can carry leftovers from an earlier
    source (``resource_id``), and the save rejects those as unknown keys.
    """
    second = {
        "pv_forecast.1.name": "second array",
        "pv_forecast.1.lat": 48.0,
        "pv_forecast.1.lon": 9.0,
        "pv_forecast.1.azimuth": 180,
    }
    resp = client.put("/api/config/", json=second)
    assert resp.status_code == 200, resp.get_json()
    entries = _entries(client)
    assert len(entries) == 2, entries
    return entries


def test_removing_the_last_entry_shortens_the_list(client):
    before = _with_two(client)

    resp = client.put("/api/config/", json={"_list_lengths": {"pv_forecast": len(before) - 1}})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["success"] is True

    after = _entries(client)
    assert len(after) == len(before) - 1
    assert after == before[:-1], "the survivors must be untouched"


def test_the_rows_are_really_gone_not_just_hidden(client):
    before = _with_two(client)
    client.put("/api/config/", json={"_list_lengths": {"pv_forecast": 1}})
    removed = client.put(
        "/api/config/", json={"_list_lengths": {"pv_forecast": 1}}
    ).get_json()["removed"]
    # Second call has nothing left to take, which is how we know the first one landed.
    assert removed == []
    assert len(_entries(client)) == 1
    assert len(before) > 1


def test_removing_an_earlier_entry_does_not_leave_a_duplicate(client):
    """
    The re-index case. The client shifts the survivors down and says how many remain;
    without the prune the old tail stayed and the list never got shorter.
    """
    before = _with_two(client)

    shifted = {}
    for index, entry in enumerate(before[1:]):
        for field, value in entry.items():
            shifted[f"pv_forecast.{index}.{field}"] = value
    shifted["_list_lengths"] = {"pv_forecast": len(before) - 1}

    assert client.put("/api/config/", json=shifted).status_code == 200
    after = _entries(client)
    assert len(after) == len(before) - 1, after
    # The survivor is the one that was second, sitting at index 0 - and the old tail
    # is gone rather than left behind as a copy of it.
    assert after[0]["name"] == before[1]["name"]
    assert len(after) == 1


def test_a_save_that_declares_nothing_removes_nothing(client):
    """A partial PATCH from a script must never be read as the whole list."""
    before = _entries(client)
    resp = client.put("/api/config/", json={"pv_forecast.0.name": "renamed"})
    assert resp.status_code == 200
    assert resp.get_json().get("removed") == []
    assert len(_entries(client)) == len(before)


def test_a_nonsense_length_is_ignored(client):
    before = _entries(client)
    for bad in ("many", None, -1):
        client.put("/api/config/", json={"_list_lengths": {"pv_forecast": bad}})
    assert len(_entries(client)) in (len(before), 0)


def test_only_list_sections_can_be_pruned(client):
    before = client.get("/api/config/").get_json()
    client.put("/api/config/", json={"_list_lengths": {"battery": 0}})
    after = client.get("/api/config/").get_json()
    assert after["battery"] == before["battery"]
