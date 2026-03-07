"""
This module defines the BaseControl class, which manages the state and demands
of a control system. It includes methods for setting and retrieving charge demands,
discharge permissions, and overall system state.
"""

import logging
import time
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("__main__")
logger.info("[BASE-CTRL] loading module ")

MODE_CHARGE_FROM_GRID = 0
MODE_AVOID_DISCHARGE = 1
MODE_DISCHARGE_ALLOWED = 2
MODE_AVOID_DISCHARGE_EVCC_FAST = 3
MODE_DISCHARGE_ALLOWED_EVCC_PV = 4
MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV = 5
MODE_CHARGE_FROM_GRID_EVCC_FAST = 6

state_mapping = {
    -2: "BACK TO AUTO",
    -1: "MODE Startup",
    0: "MODE CHARGE FROM GRID",
    1: "MODE AVOID DISCHARGE",
    2: "MODE DISCHARGE ALLOWED",
    3: "MODE AVOID DISCHARGE EVCC FAST",
    4: "MODE DISCHARGE ALLOWED EVCC PV",
    5: "MODE DISCHARGE ALLOWED EVCC MIN+PV",
    6: "MODE CHARGE FROM GRID EVCC FAST",
}


class BaseControl:
    """
    BaseControl is a class that manages the state and demands of a control system.
    It keeps track of the current AC and DC charge demands, discharge allowed status,
    and the overall state of the system. The overall state can be one of three modes:
    MODE_CHARGE_FROM_GRID, MODE_AVOID_DISCHARGE, or MODE_DISCHARGE_ALLOWED.
    """

    def __init__(self, config, timezone, time_frame_base):
        self.current_ac_charge_demand = 0
        self.last_ac_charge_demand = 0
        self.last_ac_charge_power = 0
        self.last_logged_ac_charge_power = (
            -999
        )  # Track last logged value to avoid duplicate logs
        self.last_logged_ac_charge_power_time = 0  # Track time for heartbeat logging
        self.current_ac_charge_demand_no_override = 0
        self.current_dc_charge_demand = 0
        self.last_dc_charge_demand = 0
        self.last_logged_dc_charge_demand_override = (
            -999
        )  # Track last logged DC override value to avoid duplicate logs
        self.last_logged_dc_charge_demand_override_time = (
            0  # Track time for heartbeat logging
        )
        self.current_dc_charge_demand_no_override = 0
        self.current_bat_charge_max = 0
        self.last_bat_charge_max = 0
        self.current_discharge_allowed = -1
        self.current_evcc_charging_state = False
        self.current_evcc_charging_mode = False
        # Dynamic override state for PV > Load discharge override
        self.dyn_override_discharge_allowed_active = False
        # 1 hour = 3600 seconds / 900 for 15 minutes
        self.time_frame_base = time_frame_base
        # startup with None to force a writing to the inverter
        self.current_overall_state = -1
        self.override_active = False
        self.override_active_since = 0
        self.override_end_time = 0
        self.override_charge_rate = 0
        self.override_duration = 0
        self.current_battery_soc = 0
        self.time_zone = timezone
        self.config = config
        # Track the max_charge_power_w value used in the last optimization request
        # to ensure consistent conversion of relative charge values
        self.optimization_max_charge_power_w = config["battery"]["max_charge_power_w"]

        # Slot commitment tracking for stable power delivery
        # (Energy Commitment Strategy to reduce update frequency)
        self.current_slot_commitment_wh = 0  # Energy for current slot
        self.current_slot_target_power_w = 0  # Calculated power for this slot
        self.current_slot_end_time = None  # When this slot ends
        self.slot_commitment_time = None  # When commitment was made
        self.last_ac_charge_demand_tracked = 0  # Track energy changes
        self.last_battery_max_tracked = 0  # Track battery capability changes

        self._state_change_timestamps = []
        self.update_interval = 15  # seconds
        self._update_thread = None
        self._stop_event = threading.Event()
        self.__start_update_service()

    def get_state_mapping(self, num_mode):
        """
        Returns the state mapping dictionary.
        """
        return state_mapping.get(num_mode, "unknown state")

    def was_overall_state_changed_recently(self, time_window_seconds=1, consume=False):
        """
        Checks if the overall state was changed within the last `time_window_seconds`.
        If consume is True, the change timestamps are cleared after being detected.
        """
        current_time = time.time()
        # Remove timestamps older than the time window
        self._state_change_timestamps = [
            ts
            for ts in self._state_change_timestamps
            if current_time - ts <= time_window_seconds
        ]

        has_changed = len(self._state_change_timestamps) > 0

        if has_changed and consume:
            self._state_change_timestamps = []

        return has_changed

    def get_current_ac_charge_demand(self):
        """
        Returns the current AC charge demand calculated based on maximum battery charge power.
        """
        return self.current_ac_charge_demand

    def get_current_dc_charge_demand(self):
        """
        Returns the current DC charge demand.
        """
        return self.current_dc_charge_demand

    def get_current_bat_charge_max(self):
        """
        Returns the current maximum battery charge power.
        """
        logger.debug(
            "[BASE-CTRL] get current battery charge max %s", self.current_bat_charge_max
        )
        return self.current_bat_charge_max

    def get_current_discharge_allowed(self):
        """
        Returns the current discharge demand.
        """
        return self.current_discharge_allowed

    def get_effective_discharge_allowed(self):
        """
        Returns the effective discharge allowed state based on the final overall state.
        This reflects the FINAL state after all overrides (EVCC, manual) are applied.

        Returns:
            bool: True if discharge is allowed in the current effective state, False otherwise.
        """
        # Modes where discharge is explicitly allowed
        discharge_allowed_modes = [
            MODE_DISCHARGE_ALLOWED,  # 2: Normal discharge allowed
            MODE_DISCHARGE_ALLOWED_EVCC_PV,  # 4: EVCC PV mode (discharge to support EV)
            MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV,  # 5: EVCC Min+PV mode (discharge to support EV)
        ]

        return self.current_overall_state in discharge_allowed_modes

    def get_current_overall_state(self):
        """
        Returns the current overall state.
        """
        # Return the string representation of the state
        return state_mapping.get(self.current_overall_state, "unknown state")

    def get_current_overall_state_number(self):
        """
        Returns the current overall state as a number.
        """
        return self.current_overall_state

    def get_current_battery_soc(self):
        """
        Returns the current battery state of charge (SOC).
        """
        return self.current_battery_soc

    def get_current_evcc_charging_state(self):
        """
        Returns the current EVCC charging state.
        """
        return self.current_evcc_charging_state

    def get_current_evcc_charging_mode(self):
        """
        Returns the current EVCC charging mode.
        """
        return self.current_evcc_charging_mode

    def get_override_active_and_endtime(self):
        """
        Returns whether the override is active.
        """
        return self.override_active, int(self.override_end_time)

    def get_override_charge_rate(self):
        """
        Returns the override charge rate.
        """
        return self.override_charge_rate

    def get_override_duration(self):
        """
        Returns the override duration.
        """
        return self.override_duration

    def set_dyn_override_discharge_allowed_active(self, value):
        """
        Sets whether the dynamic PV > Load discharge override is currently active.

        Args:
            value (bool): True if dynamic override is active, False otherwise
        """
        self.dyn_override_discharge_allowed_active = value

    def get_dyn_override_discharge_allowed_active(self):
        """
        Returns whether the dynamic PV > Load discharge override is currently active.

        Returns:
            bool: True if dynamic override is active, False otherwise
        """
        return self.dyn_override_discharge_allowed_active

    def set_current_ac_charge_demand(self, value_relative):
        """
        Sets the current AC charge demand.
        Uses the optimization_max_charge_power_w to convert relative values
        to ensure consistency with the value sent to the optimizer.

        Note: The value is always stored as Wh for the current slot (hour or 15min),
        not as W. Conversion to W is only done in get_needed_ac_charge_power().
        """
        current_time = datetime.now(self.time_zone)
        current_hour = current_time.hour
        if self.time_frame_base == 3600:
            minute_str = "00"
        else:
            minute = current_time.minute
            minute_str = f"{(minute // 15) * 15:02d}"
        current_charge_demand = value_relative * self.optimization_max_charge_power_w
        if current_charge_demand == self.current_ac_charge_demand:
            # No change, so do not log
            return
        # store the current charge demand without override
        self.current_ac_charge_demand_no_override = current_charge_demand
        if not self.override_active:
            self.current_ac_charge_demand = current_charge_demand
            logger.info(
                "[CHARGE_DEMAND] Relative→Absolute conversion: relative=%.3f × max_power=%s W "
                "→ %.2f Wh (slot=%s:%s, time_frame=%ds)",
                value_relative,
                self.optimization_max_charge_power_w,
                self.current_ac_charge_demand,
                current_hour,
                minute_str,
                self.time_frame_base,
            )
            logger.debug(
                "[BASE-CTRL] set AC charge demand for current slot %s:%s -> %.2f Wh"
                + " (slot=%ds, max=%s W)",
                current_hour,
                minute_str,
                self.current_ac_charge_demand,
                self.time_frame_base,
                self.optimization_max_charge_power_w,
            )
        elif self.override_active_since > time.time() - 2:
            logger.debug(
                "[BASE-CTRL] OVERRIDE AC charge demand for current slot %s:%s -> %.2f Wh"
                + " (slot=%ds, max=%s W)",
                current_hour,
                minute_str,
                self.current_ac_charge_demand,
                self.time_frame_base,
                self.config["battery"]["max_charge_power_w"],
            )
        self.__set_current_overall_state()

    def set_current_dc_charge_demand(self, value_relative):
        """
        Sets the current DC charge demand.
        Uses the optimization_max_charge_power_w to convert relative values
        to ensure consistency with the value sent to the optimizer.
        """
        current_hour = datetime.now(self.time_zone).hour
        current_charge_demand = value_relative * self.optimization_max_charge_power_w
        if current_charge_demand == self.current_dc_charge_demand:
            # logger.debug(
            #     "[BASE-CTRL] NO CHANGE DC charge demand for current hour %s:00 "+
            #     "unchanged -> %s Wh -"
            #     + " based on max charge power %s W",
            #     current_hour,
            #     self.current_dc_charge_demand,
            #     self.config["battery"]["max_charge_power_w"],
            # )
            return
        # store the current charge demand without override
        self.current_dc_charge_demand_no_override = current_charge_demand
        if not self.override_active:
            self.current_dc_charge_demand = current_charge_demand
            logger.debug(
                "[BASE-CTRL] set DC charge demand for current hour %s:00 -> %s Wh -"
                + " based on optimization max charge power %s W",
                current_hour,
                self.current_dc_charge_demand,
                self.optimization_max_charge_power_w,
            )
        else:
            # Only log override on change or every 60 seconds (heartbeat)
            current_time_unix = time.time()
            if (
                self.current_dc_charge_demand
                != self.last_logged_dc_charge_demand_override
                or current_time_unix - self.last_logged_dc_charge_demand_override_time
                > 60
            ):
                # Log details on change
                if (
                    self.current_dc_charge_demand
                    != self.last_logged_dc_charge_demand_override
                ):
                    logger.debug(
                        "[BASE-CTRL] OVERRIDE DC charge demand for current hour %s:00 -> %s Wh -"
                        + " based on max charge power %s W",
                        current_hour,
                        self.current_dc_charge_demand,
                        self.config["battery"]["max_charge_power_w"],
                    )
                # Log as heartbeat every 60 seconds
                elif (
                    current_time_unix - self.last_logged_dc_charge_demand_override_time
                    > 60
                ):
                    logger.debug(
                        "[BASE-CTRL] OVERRIDE DC charge demand (active): current hour %s:00 -> %s Wh (heartbeat)",
                        current_hour,
                        self.current_dc_charge_demand,
                    )
                self.last_logged_dc_charge_demand_override = (
                    self.current_dc_charge_demand
                )
                self.last_logged_dc_charge_demand_override_time = current_time_unix
        self.__set_current_overall_state()

    def set_current_bat_charge_max(self, value_max):
        """
        Sets the current maximum battery charge power.
        """
        if value_max == self.current_bat_charge_max:
            # logger.debug(
            #     "[BASE-CTRL] NO CHANGE Battery charge max unchanged -> %s W",
            #     self.current_bat_charge_max,
            # )
            return
        # store the current charge demand without override
        self.current_bat_charge_max = value_max
        logger.debug(
            "[BASE-CTRL] set current battery charge max to %s",
            self.current_bat_charge_max,
        )
        self.__set_current_overall_state()

    def set_current_discharge_allowed(self, value):
        """
        Sets the current discharge demand.
        """
        current_hour = datetime.now(self.time_zone).hour
        if value == self.current_discharge_allowed:
            # logger.debug(
            #     "[BASE-CTRL] NO CHANGE Discharge allowed for current hour %s:00 unchanged -> %s",
            #     current_hour,
            #     self.current_discharge_allowed,
            # )
            return
        self.current_discharge_allowed = value
        logger.debug(
            "[BASE-CTRL] set Discharge allowed for current hour %s:00 %s",
            current_hour,
            self.current_discharge_allowed,
        )
        self.__set_current_overall_state()

    def set_current_evcc_charging_state(self, value):
        """
        Sets the current EVCC charging state.
        """
        self.current_evcc_charging_state = value
        # logger.debug("[BASE-CTRL] set current EVCC charging state to %s", value)
        self.__set_current_overall_state()

    def set_current_evcc_charging_mode(self, value):
        """
        Sets the current EVCC charging mode.
        """
        self.current_evcc_charging_mode = value
        # logger.debug("[BASE-CTRL] set current EVCC charging mode to %s", value)
        self.__set_current_overall_state()

    def should_recalculate_slot_power(self):
        """
        Determine if we need to recalculate power for current slot (Energy Commitment Strategy).

        Returns:
            tuple: (should_recalculate: bool, reasons: list of strings)

        Recalculate power when:
        1. New slot started (time boundary crossed)
        2. Energy demand changed (new optimizer response)
        3. Battery capability decreased significantly (SOC/temp derate)
        4. Periodic safety refresh (every 5 minutes) to catch gradual changes
        """
        reasons = []
        current_time = datetime.now(self.time_zone)

        # Calculate current slot boundary
        seconds_elapsed = (
            current_time.hour * 3600 + current_time.minute * 60 + current_time.second
        ) % self.time_frame_base

        # Reason 1: New slot started
        if (
            self.current_slot_end_time is None
            or current_time >= self.current_slot_end_time
        ):
            reasons.append("new_slot")

        # Reason 2: Energy demand changed (new optimizer response)
        if self.current_ac_charge_demand != self.last_ac_charge_demand_tracked:
            reasons.append("new_optimizer_data")

        # Reason 3: Battery capability decreased significantly (5% threshold)
        # Only trigger on DECREASE, not increase (conservative approach)
        current_battery_max = round(self.current_bat_charge_max)
        battery_derate_threshold = self.current_slot_target_power_w * 0.95
        if (
            current_battery_max > 0
            and current_battery_max < battery_derate_threshold
            and self.current_slot_target_power_w > 0
        ):
            reasons.append("battery_derate")

        # Reason 4: Periodic safety refresh (every 5 minutes)
        # Catches gradual SOC increase and temperature changes from charging
        if self.slot_commitment_time is not None:
            elapsed_seconds = (current_time - self.slot_commitment_time).total_seconds()
            if elapsed_seconds >= 300:  # 5 minutes = 300 seconds
                reasons.append("periodic_safety_refresh")

        return len(reasons) > 0, reasons

    def update_slot_power_if_needed(self):
        """
        Update slot power commitment only when needed (Energy Commitment Strategy).

        Returns:
            tuple: (updated: bool, target_power: float)
        """
        should_update, reasons = self.should_recalculate_slot_power()

        if should_update:
            current_time = datetime.now(self.time_zone)

            # Calculate slot end time for full slot duration
            seconds_elapsed = (
                current_time.hour * 3600
                + current_time.minute * 60
                + current_time.second
            ) % self.time_frame_base
            seconds_to_end = self.time_frame_base - seconds_elapsed

            # If no time left, slot ends at next boundary
            if seconds_to_end <= 0:
                seconds_to_end = self.time_frame_base
                # Calculate next slot end time
                slot_end_seconds = (
                    current_time.hour * 3600
                    + current_time.minute * 60
                    + current_time.second
                    + self.time_frame_base
                ) % (24 * 3600)
            else:
                slot_end_seconds = (
                    current_time.hour * 3600
                    + current_time.minute * 60
                    + current_time.second
                    + seconds_to_end
                ) % (24 * 3600)

            # Calculate target power to deliver committed energy in full slot time
            current_ac_demand_wh = self.current_ac_charge_demand

            if seconds_to_end > 0 and current_ac_demand_wh > 0:
                calculated_power = round(
                    current_ac_demand_wh / (seconds_to_end / 3600), 0
                )
            else:
                calculated_power = 0

            # Cap by current battery capability
            battery_max = round(self.current_bat_charge_max)
            target_power = (
                min(calculated_power, battery_max)
                if battery_max > 0
                else calculated_power
            )

            # Store new commitment
            self.current_slot_target_power_w = target_power
            self.current_slot_commitment_wh = current_ac_demand_wh
            self.slot_commitment_time = current_time
            # Store slot end time (critical for next comparison!)
            self.current_slot_end_time = current_time + timedelta(
                seconds=seconds_to_end
            )
            # Update tracking for next comparison
            self.last_ac_charge_demand_tracked = current_ac_demand_wh
            self.last_battery_max_tracked = battery_max

            # Log reason for update
            logger.info(
                "[CHARGE_DEMAND] Updating slot power (reasons: %s): "
                "demand=%.0f Wh, slot=%ds, calculated=%.0f W, capped=%.0f W",
                ",".join(reasons),
                current_ac_demand_wh,
                seconds_to_end,
                calculated_power,
                target_power,
            )

            return True, target_power

        # No update needed, return existing commitment
        return False, self.current_slot_target_power_w

    def get_needed_ac_charge_power(self):
        """
        Returns AC charge power commitment for current slot (Energy Commitment Strategy).

        During normal EOS operation: Returns stable power calculated once per optimizer cycle
        and kept stable throughout the slot. Power is only recalculated when:
        - New optimizer response arrives
        - Battery capability decreases (SOC/temperature derating)
        - Slot boundary crossed (new time frame)

        This strategy reduces update frequency from ~3,600/hour to ~4-10/hour while maintaining
        accurate energy delivery and stable inverter control.

        During override: Returns current_ac_charge_demand directly as it's already in W.
        """
        # During override, current_ac_charge_demand is already in W, return it directly
        if self.override_active:
            return self.current_ac_charge_demand

        # Normal EOS operation: return committed power for current slot
        updated, power = self.update_slot_power_if_needed()

        if updated:
            logger.info(
                "[CHARGE_DEMAND] Slot power updated to %.0f W (new commitment)",
                power,
            )

        # Return current slot commitment (stable throughout slot)
        return self.current_slot_target_power_w

    def __set_current_overall_state(self):
        """
        Sets the current overall state and logs the timestamp if it changes.
        """
        # Check for changes in demands or battery limits FIRST
        changes = [
            (
                "AC charge demand",
                self.current_ac_charge_demand,
                self.last_ac_charge_demand,
            ),
            (
                "DC charge demand",
                self.current_dc_charge_demand,
                self.last_dc_charge_demand,
            ),
            (
                "Battery charge max",
                self.current_bat_charge_max,
                self.last_bat_charge_max,
            ),
        ]
        value_changed = any(curr != last for _, curr, last in changes)

        if self.override_active:
            # check if the override end time is reached
            if time.time() > self.override_end_time:
                logger.info("[BASE-CTRL] OVERRIDE end time reached, clearing override")
                self.clear_mode_override()
                return

            # IMPORTANT: Even during override, we must record value changes
            # so that was_overall_state_changed_recently() returns True
            if value_changed:
                self._state_change_timestamps.append(time.time())
                for name, curr, last in changes:
                    if curr != last:
                        logger.info(
                            "[BASE-CTRL] %s changed to %s W (Override active)",
                            name,
                            curr,
                        )

                # Update last values to prevent repeated triggers
                self.last_ac_charge_demand = self.current_ac_charge_demand
                self.last_dc_charge_demand = self.current_dc_charge_demand
                self.last_bat_charge_max = self.current_bat_charge_max
            return

        # Determine base state
        if self.current_ac_charge_demand > 0:
            new_state = MODE_CHARGE_FROM_GRID
        elif self.current_discharge_allowed > 0:
            new_state = MODE_DISCHARGE_ALLOWED
        elif self.current_discharge_allowed == 0:
            new_state = MODE_AVOID_DISCHARGE
        else:
            new_state = -1

        # EVCC override mapping
        evcc_override = {
            "now": MODE_AVOID_DISCHARGE_EVCC_FAST,
            "pv+now": MODE_AVOID_DISCHARGE_EVCC_FAST,
            "minpv+now": MODE_AVOID_DISCHARGE_EVCC_FAST,
            "pv+plan": MODE_AVOID_DISCHARGE_EVCC_FAST,
            "minpv+plan": MODE_AVOID_DISCHARGE_EVCC_FAST,
            "pv": MODE_DISCHARGE_ALLOWED_EVCC_PV,
            "minpv": MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV,
        }

        if self.current_evcc_charging_state:
            mode = self.current_evcc_charging_mode
            if mode in evcc_override:
                # Fast charge overrides grid charge
                if new_state == MODE_CHARGE_FROM_GRID and mode in (
                    "now",
                    "pv+now",
                    "minpv+now",
                    "pv+plan",
                    "minpv+plan",
                ):
                    new_state = MODE_CHARGE_FROM_GRID_EVCC_FAST
                    if self.current_overall_state != new_state:
                        logger.info(
                            "[BASE-CTRL] EVCC charging state is active, setting overall"
                            + " state to MODE_CHARGE_FROM_GRID_EVCC_FAST"
                        )
                else:
                    new_state = evcc_override[mode]
                    if self.current_overall_state != new_state:
                        logger.info(
                            "[BASE-CTRL] EVCC charging state is active, setting overall"
                            + " state to %s",
                            state_mapping.get(new_state, "unknown state"),
                        )

        # Check for changes

        changes = [
            (
                "AC charge demand (Wh/slot)",
                self.current_ac_charge_demand,
                self.last_ac_charge_demand,
            ),
            (
                "DC charge demand (Wh/slot)",
                self.current_dc_charge_demand,
                self.last_dc_charge_demand,
            ),
            (
                "Battery charge max (W)",
                self.current_bat_charge_max,
                self.last_bat_charge_max,
            ),
        ]
        value_changed = any(curr != last for _, curr, last in changes)

        if new_state != self.current_overall_state or value_changed:
            self._state_change_timestamps.append(time.time())
            if len(self._state_change_timestamps) > 1000:
                self._state_change_timestamps.pop(0)
            for name, curr, last in changes:
                if curr != last:
                    logger.info("[BASE-CTRL] %s changed to %.2f", name, curr)
            if not value_changed:
                logger.debug(
                    "[BASE-CTRL] overall state changed to %s",
                    state_mapping.get(new_state, "unknown state"),
                )

        # Update last values and state
        self.last_ac_charge_demand = self.current_ac_charge_demand
        self.last_dc_charge_demand = self.current_dc_charge_demand
        self.last_bat_charge_max = self.current_bat_charge_max
        self.current_overall_state = new_state

    def set_current_battery_soc(self, value):
        """
        Sets the current battery state of charge (SOC).
        """
        self.current_battery_soc = value
        # logger.debug("[BASE-CTRL] set current battery SOC to %s", value)

    def set_override_charge_rate(self, charge_rate):
        """
        Sets the override charge rate.
        """
        self.override_charge_rate = charge_rate
        logger.debug("[BASE-CTRL] set override charge rate to %s", charge_rate)

    def set_override_duration(self, duration):
        """
        Sets the override duration.
        """
        self.override_duration = duration
        logger.debug("[BASE-CTRL] set override duration to %s", duration)

    def set_mode_override(self, mode):
        """
        Sets the current overall state to a specific mode.
        """
        duration = self.override_duration
        # switch back to EOS given demands
        if mode == -2:
            self.clear_mode_override()
            return
        # convert to seconds
        duration_seconds = 0
        if 0 <= duration <= 12 * 60:
            duration_seconds = duration * 60
            # duration_seconds = duration * 60 / 10
        else:
            logger.error("[BASE-CTRL] OVERRIDE invalid duration %s", duration)
            return

        if mode >= 0 or mode <= 2:
            self.current_overall_state = mode
            self.override_active = True
            self.override_end_time = (time.time() + duration_seconds) // 60 * 60
            self._state_change_timestamps.append(time.time())
            logger.info(
                "[BASE-CTRL] OVERRIDE set overall state to %s with endtime %s",
                state_mapping[mode],
                datetime.fromtimestamp(
                    self.override_end_time, self.time_zone
                ).isoformat(),
            )
            if self.override_charge_rate > 0 and mode == MODE_CHARGE_FROM_GRID:
                self.current_ac_charge_demand = self.override_charge_rate * 1000
                logger.info(
                    "[BASE-CTRL] OVERRIDE set AC charge demand to %s",
                    self.current_ac_charge_demand,
                )
            if self.override_charge_rate > 0 and mode == MODE_DISCHARGE_ALLOWED:
                self.current_dc_charge_demand = self.override_charge_rate * 1000
                logger.info(
                    "[BASE-CTRL] OVERRIDE set DC charge demand to %s",
                    self.current_dc_charge_demand,
                )
            self.override_active_since = time.time()
        else:
            logger.error("[BASE-CTRL] OVERRIDE invalid mode %s", mode)

    def clear_mode_override(self):
        """
        Clears the current mode overrideand trigger a state change.
        """
        self.override_active = False
        self.override_end_time = 0
        self.current_ac_charge_demand = self.current_ac_charge_demand_no_override
        self.current_dc_charge_demand = self.current_dc_charge_demand_no_override
        self.__set_current_overall_state()
        # reset the override end time to 0
        logger.info("[BASE-CTRL] cleared mode override")

    def __start_update_service(self):
        """
        Starts the background thread to periodically update the charging state.
        """
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self.__update_base_control_loop, daemon=True
            )
            self._update_thread.start()
            logger.info("[BASE-CTRL] Update service started.")

    def shutdown(self):
        """
        Stops the background thread and shuts down the update service.
        """
        if self._update_thread and self._update_thread.is_alive():
            self._stop_event.set()
            self._update_thread.join()
            logger.info("[BASE-CTRL] Update service stopped.")

    def __update_base_control_loop(self):
        """
        The loop that runs in the background thread to update the charging state.
        """
        while not self._stop_event.is_set():
            self.__set_current_overall_state()

            sleep_interval = self.update_interval
            while sleep_interval > 0:
                if self._stop_event.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, sleep_interval))  # Sleep in 1-second chunks
                sleep_interval -= 1

        self.__start_update_service()
