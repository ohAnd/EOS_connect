"""Tests for the shared timeseries normalizer.

Covers the contract that the price and PV interfaces both rely on: canonical field
validation with an actionable error, derived `end`, timezone handling, and the unit
tables. The payload shapes used here are the ones reported in discussion #214.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytz

from src.interfaces.timeseries_normalizer import (
    PRICE_UNIT_TO_EUR_PER_WH,
    PV_UNITS,
    TimeseriesFormatError,
    convert_price_values,
    convert_pv_values,
    detect_resolution_seconds,
    extract_json_path,
    normalize_entries,
    price_plausibility_message,
    pv_plausibility_message,
)

BERLIN = pytz.timezone("Europe/Berlin")


class TestFieldValidation:
    """The format is fixed; the error has to make it obvious how to comply."""

    def test_missing_start_names_the_available_keys(self):
        payload = [{"start_time": "2026-08-23T00:00:00+02:00", "price_per_kwh": 0.3811}]

        with pytest.raises(TimeseriesFormatError) as exc:
            normalize_entries(payload, BERLIN)

        message = str(exc.value)
        assert "'start'" in message
        assert "'start_time'" in message
        assert "'price_per_kwh'" in message

    def test_missing_value_names_the_available_keys(self):
        payload = [{"start": "2026-08-23T00:00:00+02:00", "price_per_kwh": 0.3811}]

        with pytest.raises(TimeseriesFormatError) as exc:
            normalize_entries(payload, BERLIN)

        assert "'value'" in str(exc.value)
        assert "'price_per_kwh'" in str(exc.value)

    def test_error_points_at_the_template_docs(self):
        with pytest.raises(TimeseriesFormatError) as exc:
            normalize_entries([{"foo": 1}], BERLIN)

        assert "timeseries-templates" in str(exc.value)

    def test_non_list_payload_rejected(self):
        with pytest.raises(TimeseriesFormatError):
            normalize_entries({"start": "x", "value": 1}, BERLIN)

    def test_empty_payload_rejected(self):
        with pytest.raises(TimeseriesFormatError):
            normalize_entries([], BERLIN)

    def test_one_unparseable_entry_rejects_the_payload(self):
        """
        Dropping the bad entry instead would corrupt the result silently.

        The hourly price path maps the normalized list to slots by position, so a
        single missing entry shifts every later price an hour early — and in the
        direction that makes grid power look cheaper than it is. Rejecting lets the
        caller fall back to its cache instead.
        """
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
            {"start": "not-a-timestamp", "value": 0.31},
            {"start": "2026-08-23T01:00:00+02:00", "value": 0.32},
        ]

        with pytest.raises(TimeseriesFormatError) as exc:
            normalize_entries(payload, BERLIN)

        assert "entry 1" in str(exc.value)

    def test_null_value_rejects_the_payload(self):
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
            {"start": "2026-08-23T01:00:00+02:00", "value": None},
        ]

        with pytest.raises(TimeseriesFormatError) as exc:
            normalize_entries(payload, BERLIN)

        assert "entry 1" in str(exc.value)
        assert "non-numeric" in str(exc.value)

    def test_entry_missing_a_field_midway_rejects_the_payload(self):
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
            {"start": "2026-08-23T01:00:00+02:00"},
        ]

        with pytest.raises(TimeseriesFormatError) as exc:
            normalize_entries(payload, BERLIN)

        assert "entry 1" in str(exc.value)

    def test_all_entries_unusable_is_fatal(self):
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "value": "abc"},
            {"start": "2026-08-23T01:00:00+02:00", "value": "def"},
        ]

        with pytest.raises(TimeseriesFormatError):
            normalize_entries(payload, BERLIN)


class TestEndDerivation:
    """`end` is part of the documented format but never read — absence must not reject."""

    def test_end_derived_from_next_start(self):
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "value": 0.3811},
            {"start": "2026-08-23T00:15:00+02:00", "value": 0.3727},
        ]

        entries = normalize_entries(payload, BERLIN)

        assert entries[0]["end"] == entries[1]["start"]

    def test_last_end_reuses_the_preceding_delta(self):
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "value": 0.3811},
            {"start": "2026-08-23T00:15:00+02:00", "value": 0.3727},
        ]

        entries = normalize_entries(payload, BERLIN)

        assert entries[-1]["end"] - entries[-1]["start"] == timedelta(minutes=15)

    def test_single_entry_falls_back_to_one_hour(self):
        entries = normalize_entries(
            [{"start": "2026-08-23T00:00:00+02:00", "value": 0.3811}], BERLIN
        )

        assert entries[0]["end"] - entries[0]["start"] == timedelta(hours=1)

    def test_supplied_end_is_ignored_in_favour_of_the_derived_one(self):
        # EPEX supplies end_time, EVCC supplies end; neither is read for any
        # calculation, so the derived value is what the rest of the code sees.
        payload = [
            {"start": "2026-08-23T00:00:00+02:00", "end": "bogus", "value": 0.1766},
            {"start": "2026-08-23T00:15:00+02:00", "end": "bogus", "value": 0.1716},
        ]

        entries = normalize_entries(payload, BERLIN)

        assert entries[0]["end"] == entries[1]["start"]

    def test_entries_are_sorted_by_start(self):
        payload = [
            {"start": "2026-08-23T01:00:00+02:00", "value": 2.0},
            {"start": "2026-08-23T00:00:00+02:00", "value": 1.0},
        ]

        entries = normalize_entries(payload, BERLIN)

        assert [entry["value"] for entry in entries] == [1.0, 2.0]


class TestTimestampHandling:
    def test_offset_timestamps_preserved_as_instants(self):
        entries = normalize_entries(
            [{"start": "2026-08-23T00:00:00+02:00", "value": 1.0}], BERLIN
        )

        assert entries[0]["start"].utcoffset() == timedelta(hours=2)

    def test_zulu_timestamps_accepted(self):
        entries = normalize_entries(
            [{"start": "2026-08-23T00:00:00Z", "value": 1.0}], BERLIN
        )

        assert entries[0]["start"] == datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)

    def test_unix_timestamps_accepted(self):
        entries = normalize_entries([{"start": 1_756_000_000, "value": 1.0}], BERLIN)

        assert entries[0]["start"].tzinfo is not None

    def test_naive_timestamp_localized_and_warned(self, caplog):
        # This is bennobiber's template from #214: `| timestamp_utc` renders UTC wall
        # clock with no offset, which would otherwise silently shift every slot.
        with caplog.at_level("WARNING"):
            entries = normalize_entries(
                [{"start": "2026-08-23 12:00:00", "value": 1.0}], BERLIN
            )

        assert entries[0]["start"].tzinfo is not None
        assert "no UTC offset" in caplog.text
        assert "isoformat" in caplog.text

    def test_plain_tzinfo_without_localize_supported(self):
        # Tests construct interfaces with datetime.timezone.utc, which has no
        # pytz-style localize().
        entries = normalize_entries(
            [{"start": "2026-08-23 12:00:00", "value": 1.0}], timezone.utc
        )

        assert entries[0]["start"].tzinfo is not None

    def test_boolean_start_rejected(self):
        with pytest.raises(TimeseriesFormatError):
            normalize_entries([{"start": True, "value": 1.0}] * 2, BERLIN)


class TestResolutionDetection:
    def test_detects_quarter_hourly(self):
        entries = normalize_entries(
            [
                {"start": "2026-08-23T00:00:00+02:00", "value": 1.0},
                {"start": "2026-08-23T00:15:00+02:00", "value": 1.0},
            ],
            BERLIN,
        )

        assert detect_resolution_seconds(entries) == 900

    def test_detects_hourly(self):
        entries = normalize_entries(
            [
                {"start": "2026-08-23T00:00:00+02:00", "value": 1.0},
                {"start": "2026-08-23T01:00:00+02:00", "value": 1.0},
            ],
            BERLIN,
        )

        assert detect_resolution_seconds(entries) == 3600

    def test_unsupported_spacing_returns_none(self):
        entries = normalize_entries(
            [
                {"start": "2026-08-23T00:00:00+02:00", "value": 1.0},
                {"start": "2026-08-23T00:05:00+02:00", "value": 1.0},
            ],
            BERLIN,
        )

        assert detect_resolution_seconds(entries) is None

    def test_single_entry_returns_none(self):
        entries = normalize_entries(
            [{"start": "2026-08-23T00:00:00+02:00", "value": 1.0}], BERLIN
        )

        assert detect_resolution_seconds(entries) is None


class TestPriceUnitConversion:
    """EUR/kWh is the canonical unit because that is what EVCC's rates carry."""

    @pytest.mark.parametrize(
        "unit,raw,expected_eur_per_wh",
        [
            ("EUR/kWh", 0.3811, 0.0003811),
            ("ct/kWh", 38.11, 0.0003811),
            ("EUR/Wh", 0.0003811, 0.0003811),
        ],
    )
    def test_units_converge_on_the_same_internal_value(
        self, unit, raw, expected_eur_per_wh
    ):
        entries = [{"start": None, "end": None, "value": raw}]

        convert_price_values(entries, unit)

        assert entries[0]["value"] == pytest.approx(expected_eur_per_wh)

    def test_every_documented_unit_is_supported(self):
        assert set(PRICE_UNIT_TO_EUR_PER_WH) == {"EUR/kWh", "ct/kWh", "EUR/Wh"}

    def test_unknown_unit_rejected(self):
        with pytest.raises(TimeseriesFormatError):
            convert_price_values([{"value": 1.0}], "USD/kWh")

    def test_negative_prices_keep_their_sign(self):
        entries = [{"value": -0.05}]

        convert_price_values(entries, "EUR/kWh")

        assert entries[0]["value"] < 0


class TestPvUnitConversion:
    """W is canonical because that is what EVCC's solar forecast carries."""

    def test_watts_integrated_over_a_quarter_hour(self):
        entries = [{"value": 4000.0}]

        convert_pv_values(entries, "W", 900)

        assert entries[0]["value"] == pytest.approx(1000.0)

    def test_watts_over_a_full_hour_are_watt_hours(self):
        entries = [{"value": 4000.0}]

        convert_pv_values(entries, "W", 3600)

        assert entries[0]["value"] == pytest.approx(4000.0)

    def test_kilowatts_scaled_and_integrated(self):
        entries = [{"value": 4.0}]

        convert_pv_values(entries, "kW", 900)

        assert entries[0]["value"] == pytest.approx(1000.0)

    def test_energy_units_pass_through_regardless_of_slot_length(self):
        entries = [{"value": 1000.0}]

        convert_pv_values(entries, "Wh", 900)

        assert entries[0]["value"] == pytest.approx(1000.0)

    def test_kilowatt_hours_scaled(self):
        entries = [{"value": 1.0}]

        convert_pv_values(entries, "kWh", 900)

        assert entries[0]["value"] == pytest.approx(1000.0)

    def test_every_documented_unit_is_supported(self):
        assert set(PV_UNITS) == {"W", "kW", "Wh", "kWh"}

    def test_unknown_unit_rejected(self):
        with pytest.raises(TimeseriesFormatError):
            convert_pv_values([{"value": 1.0}], "MW", 900)


class TestPricePlausibility:
    def test_realistic_prices_produce_no_warning(self):
        values = [0.0003811, 0.0003727, 0.0003668]

        assert price_plausibility_message(values, "EUR/kWh") is None

    def test_thousandfold_too_high_is_flagged(self):
        # Exactly Tobias' case: EVCC's EUR/kWh read as EUR/Wh.
        values = [0.3811, 0.3727, 0.3668]

        message = price_plausibility_message(values, "EUR/Wh")

        assert message is not None
        assert "too high" in message

    def test_thousandfold_too_low_is_flagged(self):
        values = [0.0000003811, 0.0000003727]

        message = price_plausibility_message(values, "EUR/Wh")

        assert message is not None
        assert "too low" in message

    def test_a_few_negative_hours_do_not_trip_the_check(self):
        values = [-0.0001, 0.0] + [0.0003] * 20

        assert price_plausibility_message(values, "EUR/kWh") is None

    def test_empty_input_is_not_a_warning(self):
        assert price_plausibility_message([], "EUR/kWh") is None


class TestExtractJsonPath:
    """Shared by both interfaces and the probe, so the probe cannot disagree."""

    def test_nested_attribute_path(self):
        payload = {"attributes": {"data": [1, 2]}}

        assert extract_json_path(payload, "attributes.data") == [1, 2]

    def test_top_level_path(self):
        assert extract_json_path({"rates": [1]}, "rates") == [1]

    def test_array_index_notation(self):
        payload = {"prices": [{"data": [7]}]}

        assert extract_json_path(payload, "prices[0].data") == [7]

    def test_missing_path_returns_none(self):
        assert extract_json_path({"a": 1}, "b.c") is None


class TestPvPlausibility:
    """Catches the power/energy mix-up, which is only a factor of four."""

    def test_matching_installation_is_silent(self):
        # 4000 W across 15 min = 1000 Wh per slot on a 4 kW array.
        assert (
            pv_plausibility_message([1000.0] * 4, "W", 900, 4000)
            is None
        )

    def test_peak_beyond_installed_capacity_is_flagged(self):
        # Power read as energy: 4000 "Wh" per 15-min slot implies 16 kW.
        message = pv_plausibility_message([4000.0] * 4, "Wh", 900, 4000)

        assert message is not None
        assert "implausible PV level" in message

    def test_message_names_the_configured_unit(self):
        message = pv_plausibility_message([4000.0], "Wh", 900, 4000)

        assert "'Wh'" in message

    def test_modest_overshoot_is_tolerated(self):
        # Optimistic forecasts and oversized arrays must not produce noise.
        assert pv_plausibility_message([1100.0], "W", 900, 4000) is None

    def test_unknown_installed_power_disables_the_check(self):
        assert pv_plausibility_message([99999.0], "Wh", 900, 0) is None

    def test_empty_values_are_not_a_warning(self):
        assert pv_plausibility_message([], "W", 900, 4000) is None
