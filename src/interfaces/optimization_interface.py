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
from .optimization_backends.optimization_backend_evopt import EVOptBackend

logger = logging.getLogger("__main__")


class OptimizationInterface:
    """
    Main abstraction for optimization backends.
    Accepts and returns EOS-format requests/responses.
    Handles backend selection and delegates all transformation logic to the backend.
    """

    def __init__(self, config, time_frame_base, timezone):
        self.eos_source = config.get("source", "eos_server")
        self.base_url = (
            f"http://{config.get('server', '192.168.1.1')}:{config.get('port', 8503)}"
        )
        self.time_frame_base = time_frame_base
        self.time_zone = timezone
        self.config = config  # Store config for accessing optimization settings

        if self.eos_source == "evopt":
            self.backend = EVOptBackend(
                self.base_url, self.time_frame_base, self.time_zone
            )
            self.backend_type = "evopt"
            logger.info("[OPTIMIZATION] Using EVopt backend")
        elif self.eos_source == "eos_server":
            self.backend = EOSBackend(
                self.base_url, self.time_frame_base, self.time_zone
            )
            self.backend_type = "eos_server"
            logger.info("[OPTIMIZATION] Using EOS Server backend")
        else:
            raise ValueError(f"Unknown backend source: {self.eos_source}")

        self.last_start_solution = None
        self.home_appliance_released = False
        self.home_appliance_start_hour = None
        self.last_eos_request = None  # Store last request for dynamic override logic
        self.last_control_data = [
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "dyn_override_active": False,
                "error": 0,
                "hour": -1,
            },
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "dyn_override_active": False,
                "error": 0,
                "hour": -1,
            },
        ]

    def optimize(self, eos_request, timeout=180):
        """
        Main entry point for optimization.
        Accepts EOS-format request, returns EOS-format response.
        """
        self.last_eos_request = eos_request  # Store for dynamic override logic
        eos_response, avg_runtime = self.backend.optimize(eos_request, timeout)
        return eos_response, avg_runtime

    def examine_response_to_control_data(self, optimized_response_in):
        """
        Examines the optimized response data for control parameters.

        Returns tuple: (ac_charge, dc_charge, discharge_allowed, response_error, dyn_override_array)
            - ac_charge (float): AC charge demand as relative value
            - dc_charge (float): DC charge demand as relative value
            - discharge_allowed (bool): Whether discharge is allowed (may include dynamic override)
            - response_error (bool): Whether an error occurred during processing
            - dyn_override_array (list[bool]): Dynamic override state for each time slot (PV > Load condition)
        """
        # current_hour = datetime.now(self.time_zone).hour
        # ac_charge_demand_relative = None
        # dc_charge_demand_relative = None
        # discharge_allowed = None
        # response_error = False

        # if "ac_charge" in optimized_response_in:
        #     ac_charge_demand_relative = optimized_response_in["ac_charge"]
        #     self.last_control_data[0]["ac_charge_demand"] = ac_charge_demand_relative[
        #         current_hour
        #     ]
        #     self.last_control_data[1]["ac_charge_demand"] = ac_charge_demand_relative[
        #         current_hour + 1 if current_hour < 23 else 0
        #     ]
        #     ac_charge_demand_relative = ac_charge_demand_relative[current_hour]
        #     logger.debug(
        #         "[OPTIMIZATION] AC charge demand for current hour %s:00 -> %s %%",
        #         current_hour,
        #         ac_charge_demand_relative * 100,
        #     )
        # if "dc_charge" in optimized_response_in:
        #     dc_charge_demand_relative = optimized_response_in["dc_charge"]
        #     self.last_control_data[0]["dc_charge_demand"] = dc_charge_demand_relative[
        #         current_hour
        #     ]
        #     self.last_control_data[1]["dc_charge_demand"] = dc_charge_demand_relative[
        #         current_hour + 1 if current_hour < 23 else 0
        #     ]
        #     dc_charge_demand_relative = dc_charge_demand_relative[current_hour]
        #     logger.debug(
        #         "[OPTIMIZATION] DC charge demand for current hour %s:00 -> %s %%",
        #         current_hour,
        #         dc_charge_demand_relative * 100,
        #     )
        # if "discharge_allowed" in optimized_response_in:
        #     discharge_allowed = optimized_response_in["discharge_allowed"]
        #     self.last_control_data[0]["discharge_allowed"] = discharge_allowed[
        #         current_hour
        #     ]
        #     self.last_control_data[1]["discharge_allowed"] = discharge_allowed[
        #         current_hour + 1 if current_hour < 23 else 0
        #     ]
        #     discharge_allowed = bool(discharge_allowed[current_hour])
        #     logger.debug(
        #         "[OPTIMIZATION] Discharge allowed for current hour %s:00 %s",
        #         current_hour,
        #         discharge_allowed,
        #     )

        now = datetime.now(self.time_zone)
        # Calculate the current step index based on time_frame_base (in seconds)
        steps_per_hour = 3600 // self.time_frame_base
        current_step = now.hour * steps_per_hour + now.minute // (
            self.time_frame_base // 60
        )
        # Calculate the datetime for the current step: today midnight
        #  + step number * time_frame_base (in seconds)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        current_step_time = today_midnight + timedelta(
            seconds=current_step * self.time_frame_base
        )

        next_step = (
            current_step + 1
            if current_step
            < len(optimized_response_in.get("discharge_allowed", [])) - 1
            else 0
        )

        ac_charge_demand_relative = None
        dc_charge_demand_relative = None
        discharge_allowed = None
        dyn_override_allowed_array = []  # Array to store override states for all slots
        response_error = False

        if "ac_charge" in optimized_response_in:
            ac_charge = optimized_response_in["ac_charge"]
            self.last_control_data[0]["ac_charge_demand"] = ac_charge[current_step]
            self.last_control_data[1]["ac_charge_demand"] = ac_charge[next_step]
            ac_charge_demand_relative = ac_charge[current_step]
            logger.info(
                "[CHARGE_DEMAND] Optimizer response extraction: ac_charge[step=%d] = %.3f (%.1f%%) "
                "(time=%s, next_step=%d with value=%.3f)",
                current_step,
                ac_charge_demand_relative,
                ac_charge_demand_relative * 100,
                current_step_time.strftime("%Y-%m-%d %H:%M"),
                next_step,
                ac_charge[next_step],
            )
            logger.debug(
                "[OPTIMIZATION] AC charge demand for current step %s (%s) -> %s %%",
                current_step,
                current_step_time.strftime("%Y-%m-%d %H:%M"),
                ac_charge_demand_relative * 100,
            )
        if "dc_charge" in optimized_response_in:
            dc_charge = optimized_response_in["dc_charge"]
            self.last_control_data[0]["dc_charge_demand"] = dc_charge[current_step]
            self.last_control_data[1]["dc_charge_demand"] = dc_charge[next_step]
            dc_charge_demand_relative = dc_charge[current_step]
            logger.debug(
                "[OPTIMIZATION] DC charge demand for current step %s (%s) -> %s %%",
                current_step,
                current_step_time.strftime("%Y-%m-%d %H:%M"),
                dc_charge_demand_relative * 100,
            )
        if "discharge_allowed" in optimized_response_in:
            discharge_allowed_arr = optimized_response_in["discharge_allowed"]
            self.last_control_data[0]["discharge_allowed"] = discharge_allowed_arr[
                current_step
            ]
            self.last_control_data[1]["discharge_allowed"] = discharge_allowed_arr[
                next_step
            ]
            discharge_allowed = bool(discharge_allowed_arr[current_step])
            logger.debug(
                "[OPTIMIZATION] Discharge allowed for current step %s (%s): %s",
                current_step,
                current_step_time.strftime("%Y-%m-%d %H:%M"),
                discharge_allowed,
            )

            # Dynamic override: Calculate for ALL slots if configured and request data available
            # NOTE: Dynamic override priority is enforced at the state determination stage.
            # Manual override (if active) takes precedence over dynamic override.
            # BaseControl.__set_current_overall_state() checks manual override FIRST and returns
            # early if active, ensuring manual override always wins regardless of dynamic override state.
            dyn_override_enabled = self.config.get(
                "dyn_override_discharge_allowed_pv_greater_load", False
            )
            dyn_override_active = False

            # Initialize array with all False - will be updated for overridden slots
            dyn_override_allowed_array = [False] * len(discharge_allowed_arr)

            if dyn_override_enabled and self.last_eos_request:
                try:
                    ems_data = self.last_eos_request.get("ems", {})
                    pv_prognose = ems_data.get("pv_prognose_wh", [])
                    gesamtlast = ems_data.get("gesamtlast", [])

                    if pv_prognose and gesamtlast:
                        # Check all slots for potential overrides
                        for slot_idx in range(
                            min(
                                len(pv_prognose),
                                len(gesamtlast),
                                len(discharge_allowed_arr),
                            )
                        ):
                            pv_forecast = pv_prognose[slot_idx]
                            load_forecast = gesamtlast[slot_idx]

                            # Override applies if: PV > Load AND optimizer said no discharge
                            if (
                                pv_forecast > load_forecast
                                and not discharge_allowed_arr[slot_idx]
                            ):
                                dyn_override_allowed_array[slot_idx] = True

                                # Log for current slot
                                if slot_idx == current_step:
                                    dyn_override_active = True
                                    logger.info(
                                        "[OPTIMIZATION] Dynamic PV>Load override ACTIVATED for "
                                        "step %s (%s): PV=%.0f Wh > Load=%.0f Wh "
                                        "- discharge allowed overridden to TRUE",
                                        slot_idx,
                                        (
                                            today_midnight
                                            + timedelta(
                                                seconds=slot_idx * self.time_frame_base
                                            )
                                        ).strftime("%Y-%m-%d %H:%M"),
                                        pv_forecast,
                                        load_forecast,
                                    )
                except (KeyError, IndexError, TypeError) as e:
                    logger.warning(
                        "[OPTIMIZATION] Error calculating dynamic override array: %s",
                        str(e),
                    )

            # Update the stored discharge_allowed value with the override result for current slot
            if dyn_override_allowed_array and current_step < len(
                dyn_override_allowed_array
            ):
                if dyn_override_allowed_array[current_step]:
                    discharge_allowed = True
                    dyn_override_active = True

            self.last_control_data[0]["discharge_allowed"] = discharge_allowed
            # Store the dynamic override state for later use (e.g., in web UI)
            self.last_control_data[0]["dyn_override_active"] = dyn_override_active
            # Store the array of override states for all slots
            self.last_control_data[0][
                "dyn_override_allowed_array"
            ] = dyn_override_allowed_array

        current_hour = datetime.now(self.time_zone).hour
        if (
            "start_solution" in optimized_response_in
            and len(optimized_response_in["start_solution"]) > 1
        ):
            self.set_last_start_solution(optimized_response_in["start_solution"])
            # logger.debug(
            #     "[OPTIMIZATION] Start solution for current hour %s:00 %s",
            #     current_hour,
            #     self.get_last_start_solution(),
            # )
        else:
            logger.error("[OPTIMIZATION] No control data in optimized response")
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
                "[OPTIMIZATION] Home appliance - current hour %s:00 - start hour %s - is Released: %s",
                current_hour,
                self.home_appliance_start_hour,
                self.home_appliance_released,
            )

        return (
            ac_charge_demand_relative,
            dc_charge_demand_relative,
            discharge_allowed,
            response_error,
            dyn_override_allowed_array,
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
        # Fallback if avg_runtime is None
        if avg_runtime is None:
            logger.warning(
                "[OPTIMIZATION] avg_runtime is None, using default value 60s"
            )
            avg_runtime = 60  # or another reasonable default

        # Calculate minimum time between runs
        try:
            min_gap_seconds = max((update_interval + avg_runtime) * 0.7, 30)
        except Exception as e:
            logger.error("[OPTIMIZATION] Failed to calculate min_gap_seconds: %s", e)
            min_gap_seconds = 30

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

    def is_eos_version_at_least(self, version_str):
        """
        Checks if the EOS version from the backend is at least the specified version.

        Args:
            version_str (str): The version string to compare against.
        Returns:
            bool: True if the EOS version is at least the specified version, False otherwise.
        """
        if hasattr(self.backend, "is_eos_version_at_least"):
            result = self.backend.is_eos_version_at_least(version_str)
            return result
        return False
