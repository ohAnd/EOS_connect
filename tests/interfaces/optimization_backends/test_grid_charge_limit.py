"""
The inverter's grid charge rate in the plan (#390).

Battery and inverter set two different limits. A DC-coupled or hybrid system
charges from PV straight into the battery, so only what comes from the grid has
to pass the inverter: ``battery.max_charge_power_w`` holds for PV charging,
``inverter.max_grid_charge_rate`` for grid charging. The control loop always
capped the real setpoint at the grid rate, but the plan charged from the grid at
the battery's limit - too fast, so the charge window came out too short.

``local_evopt`` now bounds every slot's charge by PV + grid rate. EOS takes the
same number as ``inverter.max_ac_charge_power_w``, where the server knows it.
"""

from unittest.mock import Mock, patch

import pytest
import pytz
import requests

from src.interfaces.optimization_backends.local_evopt.optimizer import (
    BatteryConfig,
    GridConfig,
    OptimizationStrategy,
    Optimizer,
    TimeSeriesData,
)
from src.interfaces.optimization_backends.optimization_backend_eos import EOSBackend
from src.interfaces.optimization_backends.optimization_backend_eos_genetic import (
    EOSGeneticPath,
    supports_ac_charge_limit,
)
from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
    LocalEVOptBackend,
)
from src.interfaces.optimization_interface import OptimizationInterface

GENETIC_MODULE = (
    "src.interfaces.optimization_backends.optimization_backend_eos_genetic"
)
BERLIN = pytz.timezone("Europe/Berlin")


# ---------------------------------------------------------------------------
# local_evopt: the per-slot bound
# ---------------------------------------------------------------------------


def _battery(**overrides):
    """An empty 40 kWh battery that may take 17 kW - the reporter's setup."""
    params = {
        "s_min": 0,
        "s_max": 40000,
        "s_initial": 0,
        "c_min": 0,
        "c_max": 17000,
        "d_max": 17000,
        "p_a": 0.001,
        "charge_from_grid": True,
        "discharge_to_grid": False,
        "s_capacity": 40000,
    }
    params.update(overrides)
    return BatteryConfig(**params)


def _optimizer(battery, grid, pv=None, slots=4):
    """Cheap grid in every slot, so the solver charges as fast as it may."""
    ts = TimeSeriesData(
        dt=[3600] * slots,
        gt=[0.0] * slots,
        ft=pv or [0.0] * slots,
        p_N=[0.0001] * slots,
        p_E=[0.0] * slots,
    )
    return Optimizer(
        strategy=OptimizationStrategy(
            charging_strategy="none", discharging_strategy="none"
        ),
        grid=grid,
        batteries=[battery],
        time_series=ts,
    )


def _grid_charge_constraints(optimizer):
    """Names of the grid charge constraints the model got, slot by slot."""
    optimizer.create_model()
    names = (f"grid_charge_{t}" for t in optimizer.time_steps)
    return [
        name
        for name in names
        if optimizer.problem.get_constraint_by_name(name) is not None
    ]


def test_grid_charging_is_capped_at_the_grid_rate():
    result = _optimizer(_battery(), GridConfig(p_max_grid_charge=12000)).solve()

    assert result["status"] == "Optimal"
    charged = result["batteries"][0]["charging_power"]
    assert max(charged) == pytest.approx(12000.0, abs=1.0)
    for value in charged:
        assert value <= 12000.0 + 1e-6


def test_pv_charges_on_top_of_the_grid_rate():
    """With 5 kW of PV the battery takes its full 17 kW: 12 from the grid, 5 from PV."""
    result = _optimizer(
        _battery(), GridConfig(p_max_grid_charge=12000), pv=[5000.0], slots=1
    ).solve()

    assert result["status"] == "Optimal"
    assert result["batteries"][0]["charging_power"][0] == pytest.approx(
        17000.0, abs=1.0
    )


def test_without_a_grid_rate_the_battery_limit_applies():
    result = _optimizer(_battery(), GridConfig()).solve()

    assert result["status"] == "Optimal"
    assert max(result["batteries"][0]["charging_power"]) == pytest.approx(
        17000.0, abs=1.0
    )


@pytest.mark.parametrize("rate", [None, 0, -1, 17000, 20000])
def test_a_rate_that_cannot_bind_leaves_the_model_unchanged(rate):
    """
    A grid rate at or above the battery's limit - every AC-coupled system, and
    every default config - adds no constraint, so their plans stay what they were.
    """
    optimizer = _optimizer(_battery(), GridConfig(p_max_grid_charge=rate))
    assert not _grid_charge_constraints(optimizer)


def test_a_slot_whose_pv_covers_the_gap_gets_no_constraint():
    pv = [5000.0, 0.0, 6000.0, 0.0]
    optimizer = _optimizer(_battery(), GridConfig(p_max_grid_charge=12000), pv=pv)
    assert sorted(_grid_charge_constraints(optimizer)) == [
        "grid_charge_1",
        "grid_charge_3",
    ]


def test_a_battery_that_never_charges_from_the_grid_is_left_alone():
    optimizer = _optimizer(
        _battery(charge_from_grid=False), GridConfig(p_max_grid_charge=12000)
    )
    assert not _grid_charge_constraints(optimizer)


def test_the_charging_curve_counts_towards_what_can_bind():
    """Where the curve already holds the battery below the grid rate, nothing is added."""
    battery = _battery(c_max_series=[17000.0, 8000.0, 17000.0, 8000.0])
    optimizer = _optimizer(battery, GridConfig(p_max_grid_charge=12000))
    assert sorted(_grid_charge_constraints(optimizer)) == [
        "grid_charge_0",
        "grid_charge_2",
    ]


def test_quarter_hourly_slots_scale_the_rate_to_the_slot():
    battery = _battery()
    ts = TimeSeriesData(
        dt=[900] * 4, gt=[0.0] * 4, ft=[0.0] * 4, p_N=[0.0001] * 4, p_E=[0.0] * 4
    )
    result = Optimizer(
        strategy=OptimizationStrategy(
            charging_strategy="none", discharging_strategy="none"
        ),
        grid=GridConfig(p_max_grid_charge=12000),
        batteries=[battery],
        time_series=ts,
    ).solve()

    assert result["status"] == "Optimal"
    for value in result["batteries"][0]["charging_power"]:
        # charging_power is Wh per slot: 12 kW for a quarter hour is 3 kWh
        assert value <= 3000.0 + 1e-6


# ---------------------------------------------------------------------------
# local_evopt: wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(12000, 12000), (0, None), (None, None)])
def test_the_backend_keeps_only_a_positive_rate(value, expected):
    backend = LocalEVOptBackend(
        time_frame_base=3600, time_zone=BERLIN, max_grid_charge_w=value
    )
    assert backend.max_grid_charge_w == expected


def test_the_interface_hands_the_grid_rate_to_the_built_in_optimizer():
    interface = OptimizationInterface(
        {"source": "local_evopt", "local_evopt_max_grid_import_w": 25000},
        3600,
        BERLIN,
        inverter_max_grid_charge_rate_w=12000,
        inverter_max_pv_charge_rate_w=17000,
    )
    assert interface.backend.max_grid_charge_w == 12000
    assert interface.backend.max_grid_import_w == 25000, "import limit unchanged"


def test_an_explicit_zero_does_not_become_the_default_rate():
    """The interface's own fallback is 5000 W; a 0 must not turn into that limit."""
    interface = OptimizationInterface(
        {"source": "local_evopt"},
        3600,
        BERLIN,
        inverter_max_grid_charge_rate_w=0,
    )
    assert interface.backend.max_grid_charge_w is None


# ---------------------------------------------------------------------------
# EOS server: the native inverter setting
# ---------------------------------------------------------------------------


def _openapi(schemas):
    response = Mock()
    response.json.return_value = {"paths": {}, "components": {"schemas": schemas}}
    response.raise_for_status = Mock()
    return response


def test_a_server_with_the_setting_is_recognised():
    schemas = {
        "InverterParameters": {
            "properties": {"max_power_wh": {}, "max_ac_charge_power_w": {}}
        }
    }
    with patch(GENETIC_MODULE + ".requests.get", return_value=_openapi(schemas)):
        assert supports_ac_charge_limit("http://eos:8503") is True


def test_a_server_without_it_is_not():
    """EOS 0.2.0: the inverter has max_power_wh only and forbids extra keys."""
    schemas = {"InverterParameters": {"properties": {"max_power_wh": {}}}}
    with patch(GENETIC_MODULE + ".requests.get", return_value=_openapi(schemas)):
        assert supports_ac_charge_limit("http://eos:8503") is False


def test_an_unreachable_server_gets_no_limit():
    with patch(
        GENETIC_MODULE + ".requests.get",
        side_effect=requests.exceptions.ConnectionError("refused"),
    ):
        assert supports_ac_charge_limit("http://eos:8503") is False


def _backend(supported):
    backend = EOSBackend.__new__(EOSBackend)
    backend._ac_charge_limit_supported = supported
    return backend


REQUEST = {
    "pv_akku": {"device_id": "battery1", "max_charge_power_w": 17000},
    "inverter": {
        "device_id": "inverter1",
        "max_power_wh": 10000,
        "max_ac_charge_power_w": 12000,
        "battery_id": "battery1",
    },
}


def test_a_server_that_knows_the_setting_receives_it():
    sent = _backend(True)._drop_unsupported_inverter_keys(REQUEST)
    assert sent["inverter"]["max_ac_charge_power_w"] == 12000


def test_a_server_that_does_not_never_sees_it():
    sent = _backend(False)._drop_unsupported_inverter_keys(REQUEST)
    assert "max_ac_charge_power_w" not in sent["inverter"]
    assert sent["inverter"]["max_power_wh"] == 10000
    assert sent["pv_akku"] == REQUEST["pv_akku"]
    assert "max_ac_charge_power_w" in REQUEST["inverter"], "caller's request untouched"


def test_the_genetic_path_writes_the_setting_as_a_profile_adjustment():
    writes = []
    path = EOSGeneticPath(
        "http://eos:8503",
        900,
        BERLIN,
        get_config=lambda key: None,
        set_config=lambda key, value: writes.append((key, value)) or True,
        device_profiles=True,
    )
    path._mirror_devices(REQUEST)
    inverter = dict(writes)["devices/inverters"]["inverter1"]
    assert inverter["profile_adjustment"] == {
        "nominal_power_w": 10000,
        "max_ac_charge_power_w": 12000,
    }
