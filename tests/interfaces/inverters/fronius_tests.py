"""
Shared test suites for the two Fronius Gen24 implementations.

``fronius_legacy.py`` and ``fronius_v2.py`` differ in how they authenticate and in
little else that matters here: the inverter-data surface and the whole Time-of-Use
schedule behaviour are meant to be identical, and were being asserted twice in
byte-identical copies.

Same arrangement as ``base_inverter_tests.py`` next door -- the suite owns the
assertions, the concrete test class supplies the fixture that builds its own
inverter. Both implementations are still exercised separately; nothing is
parametrised away, only the duplicated bodies are gone.

A suite class must not be named ``Test*``, or pytest collects it on its own and
errors on the fixture it has no way to provide.
"""


class FroniusInverterDataTestSuite:
    """
    The inverter-data surface both Gen24 implementations expose.

    Requires a ``fronius_instance`` fixture.
    """

    def test_inverter_data_structure_initialized(self, fronius_instance):
        """Test that Fronius-specific inverter data structure is initialized."""
        assert (
            "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"
            in fronius_instance.inverter_current_data
        )
        assert (
            "MODULE_TEMPERATURE_MEAN_01_F32" in fronius_instance.inverter_current_data
        )
        assert "FANCONTROL_PERCENT_01_F32" in fronius_instance.inverter_current_data

    def test_get_inverter_current_data_returns_dict(self, fronius_instance):
        """Test that get_inverter_current_data returns the internal data dict."""
        result = fronius_instance.get_inverter_current_data()
        assert isinstance(result, dict)
        assert "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32" in result

    def test_has_api_set_max_grid_charge_rate_method(self, fronius_instance):
        """Fronius provides hardware-specific grid charge rate control."""
        assert hasattr(fronius_instance, "api_set_max_grid_charge_rate")
        assert callable(fronius_instance.api_set_max_grid_charge_rate)


class FroniusTimeOfUseTestSuite:
    """
    Tests for the Time-of-Use (TOU) inverter mode methods.

    Verifies that set_mode_avoid_discharge() and set_mode_allow_discharge()
    send the correct TOU schedule entries to the Gen24 for every optimizer
    state combination (dc_charge / discharge_allowed).

    Requires a ``fronius`` fixture whose ``set_time_of_use`` is mocked out.
    """

    # ------------------------------------------------------------------
    # set_mode_allow_discharge
    # ------------------------------------------------------------------

    def test_allow_discharge_with_positive_pv_rate_sends_charge_max(self, fronius):
        """Normal discharge: CHARGE_MAX with configured rate (no empty list)."""
        fronius.max_pv_charge_rate = 5000
        fronius.set_mode_allow_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        assert len(tou_list) == 1
        entry = tou_list[0]
        assert entry["ScheduleType"] == "CHARGE_MAX"
        assert entry["Power"] == 5000
        assert entry["Active"] is True

    def test_allow_discharge_with_zero_pv_rate_sends_charge_max_zero(self, fronius):
        """Evening-discharge / no-PV-refill: CHARGE_MAX:0 must be sent explicitly.

        Previously the code sent an empty TOU list which wiped all Gen24 rules.
        """
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_allow_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        assert len(tou_list) == 1, "Must not send empty list - that wipes all TOU rules"
        entry = tou_list[0]
        assert entry["ScheduleType"] == "CHARGE_MAX"
        assert entry["Power"] == 0
        assert entry["Active"] is True

    def test_allow_discharge_never_sends_empty_list(self, fronius):
        """set_mode_allow_discharge must never send an empty TOU list."""
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_allow_discharge()

        tou_list = fronius.set_time_of_use.call_args[0][0]
        assert tou_list != [], "Empty TOU list would wipe all existing Gen24 rules"

    # ------------------------------------------------------------------
    # set_mode_avoid_discharge
    # ------------------------------------------------------------------

    def test_avoid_discharge_with_positive_pv_rate_sends_only_discharge_max(
        self, fronius
    ):
        """Daytime hold: block discharging, PV may still charge freely."""
        fronius.max_pv_charge_rate = 5000
        fronius.set_mode_avoid_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        schedule_types = [e["ScheduleType"] for e in tou_list]
        assert "DISCHARGE_MAX" in schedule_types
        assert (
            "CHARGE_MAX" not in schedule_types
        ), "Should not restrict PV charging when dc_charge > 0"
        discharge_entry = next(
            e for e in tou_list if e["ScheduleType"] == "DISCHARGE_MAX"
        )
        assert discharge_entry["Power"] == 0

    def test_avoid_discharge_with_zero_pv_rate_sends_discharge_and_charge_max(
        self, fronius
    ):
        """Isolation mode: block both discharge AND PV charging."""
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_avoid_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        schedule_types = [e["ScheduleType"] for e in tou_list]
        assert "DISCHARGE_MAX" in schedule_types
        assert (
            "CHARGE_MAX" in schedule_types
        ), "Isolation mode must also block PV charging"
        charge_entry = next(e for e in tou_list if e["ScheduleType"] == "CHARGE_MAX")
        assert charge_entry["Power"] == 0

    def test_avoid_discharge_all_weekdays_active(self, fronius):
        """TOU rules must apply to every day of the week."""
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_avoid_discharge()

        tou_list = fronius.set_time_of_use.call_args[0][0]
        for entry in tou_list:
            weekdays = entry["Weekdays"]
            assert all(
                weekdays[day]
                for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            )

    def test_allow_discharge_all_weekdays_active(self, fronius):
        """TOU rules must apply to every day of the week."""
        fronius.max_pv_charge_rate = 5000
        fronius.set_mode_allow_discharge()

        tou_list = fronius.set_time_of_use.call_args[0][0]
        for entry in tou_list:
            weekdays = entry["Weekdays"]
            assert all(
                weekdays[day]
                for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            )

    # ------------------------------------------------------------------
    # api_set_max_pv_charge_rate
    # ------------------------------------------------------------------

    def test_api_set_max_pv_charge_rate_stores_value(self, fronius):
        """api_set_max_pv_charge_rate must update the stored rate."""
        fronius.api_set_max_pv_charge_rate(3000)
        assert fronius.max_pv_charge_rate == 3000

    def test_api_set_max_pv_charge_rate_zero_is_accepted(self, fronius):
        """Rate of 0 W is a valid and important value (block PV charging)."""
        fronius.api_set_max_pv_charge_rate(0)
        assert fronius.max_pv_charge_rate == 0

    def test_api_set_max_pv_charge_rate_negative_rejected(self, fronius):
        """Negative values must be rejected and the rate must stay unchanged."""
        fronius.max_pv_charge_rate = 5000
        fronius.api_set_max_pv_charge_rate(-100)
        assert fronius.max_pv_charge_rate == 5000
