"""
Tests for the offline timeseries doctor (scripts/timeseries_doctor.py).

The doctor is the "Test connection" button as a CLI: it must render the
probe's verdict verbatim and show where every entry lands on the grid.
Payloads are built in the tests — the tool exists for payloads we do not
have as fixtures.
"""

import importlib.util
import json
import os
from datetime import datetime, timedelta

import pytz
import requests

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_SCRIPT = os.path.join(_PROJECT_ROOT, "scripts", "timeseries_doctor.py")

_spec = importlib.util.spec_from_file_location("timeseries_doctor", _SCRIPT)
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)

BERLIN = pytz.timezone("Europe/Berlin")
TODAY = "2026-08-23"
MIDNIGHT = BERLIN.localize(datetime(2026, 8, 23))


def _hourly_payload(count=48, start=None, value_key="price_per_kwh"):
    """Hour-long price entries, start+end, anchored at MIDNIGHT by default."""
    start = start or MIDNIGHT
    entries = []
    for i in range(count):
        slot_start = (start + timedelta(hours=i)).astimezone(BERLIN)
        slot_end = (start + timedelta(hours=i + 1)).astimezone(BERLIN)
        entries.append(
            {
                "start_time": slot_start.isoformat(),
                "end_time": slot_end.isoformat(),
                value_key: 0.20 + (i % 24) * 0.01,
            }
        )
    return entries


def _write_payload(tmp_path, entries, wrap=True):
    """Write entries (optionally wrapped HA-style) to a payload file."""
    payload = {"attributes": {"data": entries}} if wrap else entries
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _run(argv, capsys):
    """Run the doctor's main() and return (exit code, stdout, stderr)."""
    code = doctor.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestFileMode:
    """Diagnosing a payload pasted into a file."""

    def test_full_coverage_is_ok_exit_zero(self, tmp_path, capsys):
        """A 48-slot series anchored at --today is OK with exit code 0."""
        path = _write_payload(tmp_path, _hourly_payload(48))
        code, out, _ = _run(
            ["--tz", "Europe/Berlin", "--today", TODAY, "--file", path], capsys
        )
        assert code == 0
        assert "OK" in out
        assert "start='start_time'" in out
        assert "value='price_per_kwh'" in out
        assert "covered slots:      48 of 48" in out
        assert f"anchor (slot 0):    {MIDNIGHT.isoformat()}" in out

    def test_partial_series_warns_but_passes(self, tmp_path, capsys):
        """A series covering part of the window passes but shows the gaps."""
        path = _write_payload(tmp_path, _hourly_payload(36))
        code, out, _ = _run(
            ["--tz", "Europe/Berlin", "--today", TODAY, "--file", path], capsys
        )
        assert code == 0
        assert "covered slots:      36 of 48 (first 0, last 35)" in out
        assert "warning: The series covers slots 0-35 of 48" in out
        # Uncovered slots are visible, not hidden.
        assert "gap" in out

    def test_stale_series_fails_exit_one(self, tmp_path, capsys):
        """A series entirely before the window fails like the probe does."""
        stale = _hourly_payload(24, start=MIDNIGHT - timedelta(days=7))
        path = _write_payload(tmp_path, stale)
        code, out, _ = _run(
            ["--tz", "Europe/Berlin", "--today", TODAY, "--file", path], capsys
        )
        assert code == 1
        assert "FAIL" in out
        # The window-miss verdict names the url field — same text as the probe.
        assert "price.data_url" in out

    def test_bare_value_array_fails_without_landing_table(self, tmp_path, capsys):
        """Bare number arrays are rejected; no landing table is attempted."""
        path = _write_payload(tmp_path, [0.25, 0.30, 0.28], wrap=False)
        code, out, _ = _run(
            ["--tz", "Europe/Berlin", "--today", TODAY, "--file", path], capsys
        )
        assert code == 1
        assert "FAIL" in out
        assert "== Landing ==" not in out

    def test_bad_json_path_fails(self, tmp_path, capsys):
        """A data path that resolves to nothing is reported by name."""
        path = _write_payload(tmp_path, _hourly_payload(4))
        code, _, err = _run(
            [
                "--tz", "Europe/Berlin", "--today", TODAY,
                "--file", path, "--path", "attributes.nope",
            ],
            capsys,
        )
        assert code == 1
        assert "attributes.nope" in err

    def test_unknown_timezone_fails(self, tmp_path, capsys):
        """An unknown zone name is an error, not a silent UTC fallback."""
        path = _write_payload(tmp_path, _hourly_payload(4))
        code, _, err = _run(
            ["--tz", "Mars/Olympus", "--today", TODAY, "--file", path], capsys
        )
        assert code == 1
        assert "Mars/Olympus" in err


def test_fall_back_day_shows_both_two_oclocks(tmp_path, capsys):
    """2026-10-25 Europe/Berlin: the two 02:00 wall hours get their offsets."""
    # The day has 25 wall-clock hours; build the entries in UTC so the
    # payload is unambiguous.
    midnight = BERLIN.localize(datetime(2026, 10, 25))
    start_utc = midnight.astimezone(pytz.utc)
    entries = []
    for i in range(48):
        slot_start = start_utc + timedelta(hours=i)
        entries.append(
            {
                "start": slot_start.isoformat(),
                "end": (slot_start + timedelta(hours=1)).isoformat(),
                "value": 0.25,
            }
        )
    path = _write_payload(tmp_path, entries)
    code, out, _ = _run(
        ["--tz", "Europe/Berlin", "--today", "2026-10-25", "--file", path],
        capsys,
    )
    assert code == 0
    # The anchor carries midnight's own offset, not now's.
    assert "anchor (slot 0):    2026-10-25T00:00:00+02:00" in out
    # Slots 2 and 3 are both 02:00 on the wall — the doctor shows the
    # offset that tells them apart.
    assert "2026-10-25T02:00:00+02:00" in out
    assert "2026-10-25T02:00:00+01:00" in out


class TestDomains:
    """Per-domain unit handling and placement kinds."""

    def test_feed_in_domain_renders_price_units(self, tmp_path, capsys):
        """Feed-in payloads are priced and displayed in ct/kWh."""
        path = _write_payload(tmp_path, _hourly_payload(48))
        code, out, _ = _run(
            [
                "--domain", "feed_in", "--tz", "Europe/Berlin",
                "--today", TODAY, "--file", path,
            ],
            capsys,
        )
        assert code == 0
        assert "ct/kWh" in out

    def test_pv_domain_places_energy(self, tmp_path, capsys):
        """PV payloads resolve pv_estimate and show energy per slot."""
        entries = _hourly_payload(48, value_key="pv_estimate")
        path = _write_payload(tmp_path, entries)
        code, out, _ = _run(
            [
                "--domain", "pv", "--unit", "kWh", "--tz", "Europe/Berlin",
                "--today", TODAY, "--file", path,
            ],
            capsys,
        )
        assert code == 0
        assert "value='pv_estimate'" in out
        assert "Wh" in out


class TestUrlMode:
    """Fetching the payload instead of reading a file."""

    def test_fetches_with_bearer_token(self, monkeypatch, capsys):
        """--url fetches with the shared bearer kwargs and diagnoses the body."""
        seen = {}

        class _Response:
            """Canned HTTP response."""

            def raise_for_status(self):
                """Succeed."""

            def json(self):
                """A healthy HA-style payload."""
                return {"attributes": {"data": _hourly_payload(48)}}

        def fake_get(url, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return _Response()

        monkeypatch.setattr(doctor.requests, "get", fake_get)
        code, out, _ = _run(
            [
                "--tz", "Europe/Berlin", "--today", TODAY,
                "--url", "http://ha.local/api/states/sensor.prices",
                "--token", "secret-token",
            ],
            capsys,
        )
        assert code == 0
        assert "OK" in out
        assert seen["url"] == "http://ha.local/api/states/sensor.prices"
        assert seen["kwargs"]["headers"]["Authorization"] == "Bearer secret-token"

    def test_fetch_error_is_reported(self, monkeypatch, capsys):
        """A refused connection is a curated message and exit code 1."""

        def fake_get(url, **kwargs):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(doctor.requests, "get", fake_get)
        code, _, err = _run(
            ["--url", "http://down.local/x", "--today", TODAY], capsys
        )
        assert code == 1
        assert "could not load the payload" in err


def test_verdict_matches_analyse(tmp_path, capsys):
    """The doctor must never disagree with the "Test connection" button."""
    entries = _hourly_payload(36)
    path = _write_payload(tmp_path, entries)
    _, out, _ = _run(
        ["--tz", "Europe/Berlin", "--today", TODAY, "--file", path], capsys
    )
    verdict = doctor.analyse(
        entries,
        "price",
        "EUR/kWh",
        BERLIN,
        time_frame_base=3600,
        installed_power_w=0,
        anchor=MIDNIGHT,
    )
    assert verdict["ok"]
    for warning in verdict["warnings"]:
        assert f"warning: {warning}" in out
