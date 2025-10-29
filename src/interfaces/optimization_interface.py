"""
optimization_interface.py
This module provides the OptimizationInterface class, which serves as the main abstraction layer
for interacting with different optimization backends. It accepts and returns requests and responses
in the EOS format, handles backend selection, and delegates transformation logic to the selected
backend. The interface also manages control data, home appliance scheduling, and calculates the next
optimal run time for the optimization process.
Classes:
    OptimizationInterface: Abstraction for optimization backends supporting EOS-format requests and
    responses.
Usage:
    Instantiate OptimizationInterface with configuration and timezone, then use its methods to
    perform optimization, retrieve control data, and manage scheduling.
Example:
    interface = OptimizationInterface(config, timezone)
    response, avg_runtime = interface.optimize(eos_request)
"""

import logging
from datetime import datetime, timedelta
from .optimization_backends.optimization_backend_eos import EOSBackend
from .optimization_backends.optimization_backend_evcc_opt import EVCCOptBackend

logger = logging.getLogger("__main__")


class OptimizationInterface:
    """
    Main abstraction for optimization backends.
    Accepts and returns EOS-format requests/responses.
    Handles backend selection and delegates all transformation logic to the backend.
    """

    def __init__(self, config, timezone):
        self.eos_source = config.get("source", "eos_server")
        self.base_url = (
            f"http://{config.get('server', '192.168.1.1')}:{config.get('port', 8503)}"
        )
        self.time_zone = timezone

        if self.eos_source == "evcc_opt":
            self.backend = EVCCOptBackend(self.base_url, self.time_zone)
            self.backend_type = "evcc_opt"
            logger.info("[OPT] Using EVCC Opt backend")
        elif self.eos_source == "eos_server":
            self.backend = EOSBackend(self.base_url, self.time_zone)
            self.backend_type = "eos_server"
            logger.info("[OPT] Using EOS Server backend")
        else:
            raise ValueError(f"Unknown backend source: {self.eos_source}")

        self.last_start_solution = None
        self.home_appliance_released = False
        self.home_appliance_start_hour = None
        self.last_control_data = [
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "error": 0,
                "hour": -1,
            },
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "error": 0,
                "hour": -1,
            },
        ]

    def optimize(self, eos_request, timeout=180):
        """
        Main entry point for optimization.
        Accepts EOS-format request, returns EOS-format response.
        """
        eos_response, avg_runtime = self.backend.optimize(eos_request, timeout)
        return eos_response, avg_runtime

    def examine_response_to_control_data(self, optimized_response_in):
        """
        Examines the optimized response data for control parameters.
        Returns tuple: (ac_charge, dc_charge, discharge_allowed, response_error)
        """
        current_hour = datetime.now(self.time_zone).hour
        ac_charge_demand_relative = None
        dc_charge_demand_relative = None
        discharge_allowed = None
        response_error = False

        if "ac_charge" in optimized_response_in:
            ac_charge_demand_relative = optimized_response_in["ac_charge"]
            self.last_control_data[0]["ac_charge_demand"] = ac_charge_demand_relative[
                current_hour
            ]
            self.last_control_data[1]["ac_charge_demand"] = ac_charge_demand_relative[
                current_hour + 1 if current_hour < 23 else 0
            ]
            ac_charge_demand_relative = ac_charge_demand_relative[current_hour]
            logger.debug(
                "[OPT] AC charge demand for current hour %s:00 -> %s %%",
                current_hour,
                ac_charge_demand_relative * 100,
            )
        if "dc_charge" in optimized_response_in:
            dc_charge_demand_relative = optimized_response_in["dc_charge"]
            self.last_control_data[0]["dc_charge_demand"] = dc_charge_demand_relative[
                current_hour
            ]
            self.last_control_data[1]["dc_charge_demand"] = dc_charge_demand_relative[
                current_hour + 1 if current_hour < 23 else 0
            ]
            dc_charge_demand_relative = dc_charge_demand_relative[current_hour]
            logger.debug(
                "[OPT] DC charge demand for current hour %s:00 -> %s %%",
                current_hour,
                dc_charge_demand_relative * 100,
            )
        if "discharge_allowed" in optimized_response_in:
            discharge_allowed = optimized_response_in["discharge_allowed"]
            self.last_control_data[0]["discharge_allowed"] = discharge_allowed[
                current_hour
            ]
            self.last_control_data[1]["discharge_allowed"] = discharge_allowed[
                current_hour + 1 if current_hour < 23 else 0
            ]
            discharge_allowed = bool(discharge_allowed[current_hour])
            logger.debug(
                "[OPT] Discharge allowed for current hour %s:00 %s",
                current_hour,
                discharge_allowed,
            )

        if (
            "start_solution" in optimized_response_in
            and len(optimized_response_in["start_solution"]) > 1
        ):
            self.set_last_start_solution(optimized_response_in["start_solution"])
            logger.debug(
                "[OPT] Start solution for current hour %s:00 %s",
                current_hour,
                self.get_last_start_solution(),
            )
        else:
            logger.error("[OPT] No control data in optimized response")
            response_error = True

        self.last_control_data[0]["error"] = int(response_error)
        self.last_control_data[1]["error"] = int(response_error)
        self.last_control_data[0]["hour"] = current_hour
        self.last_control_data[1]["hour"] = current_hour + 1 if current_hour < 23 else 0

        if "washingstart" in optimized_response_in:
            self.home_appliance_start_hour = optimized_response_in["washingstart"]
            self.home_appliance_released = (
                self.home_appliance_start_hour == current_hour
            )
            logger.debug(
                "[OPT] Home appliance - current hour %s:00 - start hour %s - is Released: %s",
                current_hour,
                self.home_appliance_start_hour,
                self.home_appliance_released,
            )

        return (
            ac_charge_demand_relative,
            dc_charge_demand_relative,
            discharge_allowed,
            response_error,
        )

    def set_last_start_solution(self, last_start_solution):
        """
        Sets the last start solution for the optimization process.

        Args:
            last_start_solution: The solution to be stored as the last start solution.
        """
        self.last_start_solution = last_start_solution

    def get_last_start_solution(self):
        """
        Returns the last start solution used in the optimization process.

        Returns:
            Any: The last start solution stored in the instance.
        """
        return self.last_start_solution

    def get_last_control_data(self):
        """
        Retrieve the most recent control data.

        Returns:
            Any: The last control data stored in the instance.
        """
        return self.last_control_data

    def get_home_appliance_released(self):
        """
        Returns the value of the home_appliance_released attribute.

        Returns:
            Any: The current value of home_appliance_released.
        """
        return self.home_appliance_released

    def get_home_appliance_start_hour(self):
        """
        Returns the start hour for the home appliance.

        Returns:
            int: The hour at which the home appliance is scheduled to start.
        """
        return self.home_appliance_start_hour

    def calculate_next_run_time(self, current_time, avg_runtime, update_interval):
        """
        Calculate the next run time prioritizing quarter-hour alignment with improved gap filling.
        """
        # Calculate minimum time between runs
        min_gap_seconds = max((update_interval + avg_runtime) * 0.7, 30)

        # Find next quarter-hour from current time
        next_quarter = current_time.replace(second=0, microsecond=0)
        current_minute = next_quarter.minute

        minutes_past_quarter = current_minute % 15
        if minutes_past_quarter == 0 and current_time.second > 0:
            minutes_to_add = 15
        elif minutes_past_quarter == 0:
            minutes_to_add = 15
        else:
            minutes_to_add = 15 - minutes_past_quarter

        next_quarter += timedelta(minutes=minutes_to_add)

        quarter_aligned_start = next_quarter - timedelta(seconds=avg_runtime)

        # **BUG FIX**: Check if quarter_aligned_start is in the past
        if quarter_aligned_start <= current_time:
            # Move to the next quarter-hour
            next_quarter += timedelta(minutes=15)
            quarter_aligned_start = next_quarter - timedelta(seconds=avg_runtime)
            logger.debug(
                "[OPTIMIZATION] Quarter start was in past, moved to next: %s",
                next_quarter.strftime("%H:%M:%S"),
            )

        time_until_quarter_start = (
            quarter_aligned_start - current_time
        ).total_seconds()

        # Debug logging
        logger.debug(
            "[OPTIMIZATION] Debug: current=%s, next_quarter=%s, quarter_start=%s, time_until=%.1fs",
            current_time.strftime("%H:%M:%S"),
            next_quarter.strftime("%H:%M:%S"),
            quarter_aligned_start.strftime("%H:%M:%S"),
            time_until_quarter_start,
        )

        # More aggressive gap-filling: if we have at least 2x the update interval,
        # try a gap-fill run
        if (
            time_until_quarter_start >= (2 * update_interval)
            and time_until_quarter_start >= min_gap_seconds
        ):
            normal_next_start = current_time + timedelta(seconds=update_interval)
            logger.info(
                "[OPTIMIZATION] Gap-fill run: start %s (quarter-aligned run follows at %s)",
                normal_next_start.strftime("%H:%M:%S"),
                next_quarter.strftime("%H:%M:%S"),
            )
            return normal_next_start

        # Otherwise, use quarter-aligned timing
        absolute_min_seconds = max(avg_runtime * 0.5, 30)
        if time_until_quarter_start < absolute_min_seconds:
            next_quarter += timedelta(minutes=15)
            quarter_aligned_start = next_quarter - timedelta(seconds=avg_runtime)
            logger.debug(
                "[OPTIMIZATION] Quarter too close, moved to next: %s",
                next_quarter.strftime("%H:%M:%S"),
            )

        logger.info(
            "[OPTIMIZATION] Quarter-hour aligned run: start %s, finish at %s",
            quarter_aligned_start.strftime("%H:%M:%S"),
            next_quarter.strftime("%H:%M:%S"),
        )
        return quarter_aligned_start

    def get_eos_version(self):
        """
        Returns the EOS version from the backend if available.
        """
        if hasattr(self.backend, "get_eos_version"):
            return self.backend.get_eos_version()
        return None
