"""
Module: optimization_backend_evopt
This module provides the EVOptBackend class, which acts as a backend for EVopt optimization.
It accepts EOS-format optimization requests, transforms them into the EVopt format, sends them
to the EVopt server,
and transforms the responses back into EOS-format responses.
Classes:
    EVCCOptBackend: Handles the transformation, communication, and response processing for
    EVopt optimization.
Typical usage example:
    backend = EVCCOptBackend(base_url="http://evcc-opt-server",
    time_zone=pytz.timezone("Europe/Berlin"))
    eos_response, avg_runtime = backend.optimize(eos_request)
"""

import logging
import time
import json
import os
from math import floor
from datetime import datetime
import requests

logger = logging.getLogger("__main__")


class EVOptBackend:
    """
    Backend for EVopt optimization.
    Accepts EOS-format requests, transforms to EVopt format, and returns EOS-format responses.
    """

    def __init__(self, base_url, time_frame_base, time_zone):
        self.base_url = base_url
        self.time_frame_base = time_frame_base
        self.time_zone = time_zone
        self.last_optimization_runtimes = [0] * 5
        self.last_optimization_runtime_number = 0

    def optimize(self, eos_request, timeout=180):
        """
        Accepts EOS-format request, transforms to EVopt format, sends request,
        transforms response back to EOS-format, and returns (response_json, avg_runtime).
        """
        evopt_request, errors = self._transform_request_from_eos_to_evopt(eos_request)
        if errors:
            logger.error("[EVopt] Request transformation errors: %s", errors)
        # Optionally, write transformed payload to json file for debugging
        debug_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "json",
            "optimize_request_evopt.json",
        )
        debug_path = os.path.abspath(debug_path)
        try:
            with open(debug_path, "w", encoding="utf-8") as fh:
                json.dump(evopt_request, fh, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.warning("[EVopt] Could not write debug file: %s", e)

        request_url = self.base_url + "/optimize/charge-schedule"
        logger.info(
            "[EVopt] Request optimization with: %s - and with timeout: %s",
            request_url,
            timeout,
        )
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        response = None
        try:
            start_time = time.time()
            response = requests.post(
                request_url, headers=headers, json=evopt_request, timeout=timeout
            )
            end_time = time.time()
            elapsed_time = end_time - start_time
            minutes, seconds = divmod(elapsed_time, 60)
            logger.info(
                "[EVopt] Response retrieved successfully in %d min %.2f sec for current run",
                int(minutes),
                seconds,
            )
            response.raise_for_status()
            # Store runtime in circular list
            if all(runtime == 0 for runtime in self.last_optimization_runtimes):
                self.last_optimization_runtimes = [elapsed_time] * 5
            else:
                self.last_optimization_runtimes[
                    self.last_optimization_runtime_number
                ] = elapsed_time
            self.last_optimization_runtime_number = (
                self.last_optimization_runtime_number + 1
            ) % 5
            avg_runtime = sum(self.last_optimization_runtimes) / 5
            evopt_response = response.json()

            # Optionally, write transformed payload to json file for debugging
            debug_path = os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "json",
                "optimize_response_evopt.json",
            )
            debug_path = os.path.abspath(debug_path)
            try:
                with open(debug_path, "w", encoding="utf-8") as fh:
                    json.dump(evopt_response, fh, indent=2, ensure_ascii=False)
            except OSError as e:
                logger.warning("[EVopt] Could not write debug file: %s", e)

            eos_response = self._transform_response_from_evopt_to_eos(
                evopt_response, evopt_request
            )
            return eos_response, avg_runtime
        except requests.exceptions.Timeout:
            logger.error("[EVopt] Request timed out after %s seconds", timeout)
            return {"error": "Request timed out - trying again with next run"}, None
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EVopt] Connection error - server not reachable at %s "
                "will try again with next cycle - error: %s",
                request_url,
                str(e),
            )
            return {
                "error": f"EVopt server not reachable at {self.base_url} "
                "will try again with next cycle"
            }, None
        except requests.exceptions.RequestException as e:
            logger.error("[EVopt] Request failed: %s", e)
            if response is not None:
                logger.error("[EVopt] Response status: %s", response.status_code)
                logger.debug(
                    "[EVopt] ERROR - response of server is:\n%s",
                    response.text,
                )
            logger.debug(
                "[EVopt] ERROR - payload for the request was:\n%s",
                evopt_request,
            )
            return {"error": str(e)}, None

    def _transform_request_from_eos_to_evopt(self, eos_request):
        """
        Translate EOS request -> EVCC request.
        Returns (evopt: dict, external_errors: list[str])
        """
        eos_request = eos_request or {}
        errors = []

        ems = eos_request.get("ems", {}) or {}
        pv_series = ems.get("pv_prognose_wh", []) or []
        price_series = ems.get("strompreis_euro_pro_wh", []) or []
        feed_series = ems.get("einspeiseverguetung_euro_pro_wh", []) or []
        load_series = ems.get("gesamtlast", []) or []

        now = datetime.now(self.time_zone)
        if self.time_frame_base == 900:
            # 15-min intervals
            current_slot = now.hour * 4 + floor(now.minute / 15)

            def wrap(arr):
                arr = arr or []
                return (arr[current_slot:] + arr[:current_slot])[:192]

            pv_series = wrap(pv_series)
            price_series = wrap(price_series)
            feed_series = wrap(feed_series)
            load_series = wrap(load_series)
            n = 192
        else:
            # hourly intervals
            current_hour = now.hour
            pv_series = (
                pv_series[current_hour:] if len(pv_series) > current_hour else pv_series
            )
            price_series = (
                price_series[current_hour:]
                if len(price_series) > current_hour
                else price_series
            )
            feed_series = (
                feed_series[current_hour:]
                if len(feed_series) > current_hour
                else feed_series
            )
            load_series = (
                load_series[current_hour:]
                if len(load_series) > current_hour
                else load_series
            )
            lengths = [
                len(s)
                for s in (pv_series, price_series, feed_series, load_series)
                if len(s) > 0
            ]
            n = min(lengths) if lengths else 1

        def normalize(arr):
            return [float(x) for x in arr[:n]] if arr else [0.0] * n

        pv_ts = normalize(pv_series)
        price_ts = normalize(price_series)
        feed_ts = normalize(feed_series)
        load_ts = normalize(load_series)

        pv_akku = eos_request.get("pv_akku") or {}
        batt_capacity_wh = float(pv_akku.get("capacity_wh", 0))
        batt_initial_pct = float(pv_akku.get("initial_soc_percentage", 0))
        batt_min_pct = float(pv_akku.get("min_soc_percentage", 0))
        batt_max_pct = float(pv_akku.get("max_soc_percentage", 100))
        batt_c_max = float(pv_akku.get("max_charge_power_w", 0))
        batt_eta_c = float(pv_akku.get("charging_efficiency", 0.95))
        batt_eta_d = float(pv_akku.get("discharging_efficiency", 0.95))

        s_min = batt_capacity_wh * (batt_min_pct / 100.0)
        s_max = batt_capacity_wh * (batt_max_pct / 100.0)
        s_initial = batt_capacity_wh * (batt_initial_pct / 100.0)

        batteries = []
        if batt_capacity_wh > 0:
            batteries.append(
                {
                    "device_id": pv_akku.get("device_id", "akku1"),
                    "charge_from_grid": True,
                    "discharge_to_grid": True,
                    "s_min": s_min,
                    "s_max": s_max,
                    "s_initial": s_initial,
                    "p_demand": [0.0] * n,
                    # "s_goal": [s_initial] * n,
                    "s_goal": [0.0] * n,
                    "c_min": 0.0,
                    "c_max": batt_c_max,
                    "d_max": batt_c_max,
                    "p_a": 0.0,
                }
            )

        p_max_imp = 10000
        p_max_exp = 10000

        # Compute dt series based on time_frame_base
        # Each entry corresponds to the time frame in seconds
        # first entry may be shorter to align with time_frame_base
        now = datetime.now(self.time_zone)
        seconds_since_midnight = now.hour * 3600 + now.minute * 60 + now.second
        dt_first_entry = self.time_frame_base - (
            seconds_since_midnight % self.time_frame_base
        )
        dt_series = [dt_first_entry] + [self.time_frame_base] * (n - 1)

        evopt = {
            "strategy": {
                "charging_strategy": "charge_before_export",
                "discharging_strategy": "discharge_before_import",
            },
            "grid": {
                "p_max_imp": p_max_imp,
                "p_max_exp": p_max_exp,
                "prc_p_imp_exc": 0,
            },
            "batteries": batteries,
            "time_series": {
                "dt": dt_series,
                "gt": [float(x) for x in load_ts],
                "ft": [float(x) for x in pv_ts],
                "p_N": [float(x) for x in price_ts],
                "p_E": [float(x) for x in feed_ts],
            },
            "eta_c": batt_eta_c if batt_capacity_wh > 0 else 0.95,
            "eta_d": batt_eta_d if batt_capacity_wh > 0 else 0.95,
        }

        return evopt, errors

    def _transform_response_from_evopt_to_eos(self, evcc_resp, evopt=None):
        """
        Translate EVoptimizer response -> EOS-style optimize response.

        ARRAY LENGTH SPECIFICATION FOR EOS RESPONSE:
        ============================================

        CONTROL ARRAYS (Full 2-day timeline from midnight today):
        ----------------------------------------------------------
        These arrays MUST cover the complete period from midnight today (00:00)
        to midnight day after tomorrow (48:00):

        - ac_charge:           192 values (15-min) OR 48 values (hourly)
        - dc_charge:           192 values (15-min) OR 48 values (hourly)
        - discharge_allowed:   192 values (15-min) OR 48 values (hourly)
        - start_solution:      192 values (15-min) OR 48 values (hourly)
        - eauto.charge_array:  192 values (15-min) OR 48 values (hourly) [if present]
        - eauto.discharge_array: 192 values (15-min) OR 48 values (hourly) [if present]

        Implementation: Past slots (midnight -> now) are zero-padded,
                    Future slots (now -> midnight tomorrow) from EVopt response

        RESULT ARRAYS (Variable timeline from current time):
        -----------------------------------------------------
        These arrays cover ONLY the future period from NOW until midnight tomorrow:

        - result.Last_Wh_pro_Stunde:              Variable (depends on current time)
        - result.Netzbezug_Wh_pro_Stunde:         Variable (depends on current time)
        - result.Netzeinspeisung_Wh_pro_Stunde:   Variable (depends on current time)
        - result.Kosten_Euro_pro_Stunde:          Variable (depends on current time)
        - result.Einnahmen_Euro_pro_Stunde:       Variable (depends on current time)
        - result.Verluste_Pro_Stunde:             Variable (depends on current time)
        - result.akku_soc_pro_stunde:             Variable (depends on current time)
        - result.Electricity_price:               Variable (depends on current time)
        - result.Home_appliance_wh_per_hour:      Variable (depends on current time)
        - result.EAuto_SoC_pro_Stunde:            Variable (depends on current time)

        Implementation: No padding, arrays start from current time

        EXAMPLES:
        ---------
        For 15-minute intervals (time_frame_base=900):
        - Current time: 07:30 (slot 30 of 96 for day 1)
        - Control arrays: 30 zeros (00:00-07:15) + 162 values from EVopt = 192 total
        - Result arrays: 162 values (07:30 today -> 00:00 day after tomorrow)

        For hourly intervals (time_frame_base=3600):
        - Current time: 07:xx (hour 7)
        - Control arrays: 7 zeros (00:00-06:00) + 41 values from EVopt = 48 total
        - Result arrays: 41 values (07:00 today -> 00:00 day after tomorrow)

        EVOPT RESPONSE EXPECTATION:
        ---------------------------
        EVopt server returns 192 (15-min) or 48 (hourly) values representing
        48 hours starting from the current time slot. These are used to:
        1. Fill future slots in control arrays (after padding past with zeros)
        2. Provide all data for result arrays (no padding needed)

        Args:
            evcc_resp: EVopt server response dict
            evopt: Original EVopt request dict (for extracting pricing, load, etc.)

        Returns:
            dict: EOS-format response with properly sized arrays
        """
        # defensive guard
        if not isinstance(evcc_resp, dict):
            logger.debug(
                "[EOS] EVCC transform response - input not a dict, returning empty dict"
            )
            return {}

        # EVCC might wrap actual payload under "response"
        resp = evcc_resp.get("response", evcc_resp)

        # Calculate time-based parameters
        time_params = self._calculate_time_parameters()

        # Extract battery parameters from request
        battery_params = self._extract_battery_parameters(evopt)

        # Extract response data arrays
        response_arrays = self._extract_response_arrays(
            resp, time_params["n_control"], time_params["n_result"]
        )

        # Extract pricing data
        pricing_data = self._extract_pricing_data(evopt, time_params["n_result"])

        # Process control arrays (ac_charge, dc_charge, discharge_allowed)
        control_arrays = self._process_control_arrays(
            response_arrays["full"],
            battery_params,
            time_params["n_result"],  # CHANGED: was n_control
        )

        # Process result arrays (costs, revenues, losses, SOC)
        result_data = self._process_result_arrays(
            response_arrays["result"],
            pricing_data,
            battery_params,
            evopt,
            time_params["n_result"],
        )

        # Build EOS response
        return self._build_eos_response(
            control_arrays,
            result_data,
            time_params,
            resp,
            evcc_resp,
        )

    def _calculate_time_parameters(self):
        """
        Calculate time-based parameters for array sizing and padding.

        Returns:
            dict with keys:
            - n_control: Total slots for control arrays (192 for 15-min, 48 for hourly)
            - n_result: Slots for result arrays (from now to midnight tomorrow)
            - current_slot: Current time slot index
            - pad_past: Padding array for past slots
        """
        now = datetime.now(self.time_zone)
        current_hour = now.hour

        if self.time_frame_base == 900:
            # 15-minute intervals
            n_control = 192  # 48 hours * 4
            current_slot = now.hour * 4 + floor(now.minute / 15)
            pad_past = [0.0] * current_slot

            # From NOW to midnight tomorrow
            slots_today = (24 - now.hour) * 4 - floor(now.minute / 15)
            n_result = slots_today + 96  # +96 for tomorrow
        else:
            # Hourly intervals
            n_control = 48  # 48 hours
            current_slot = current_hour
            pad_past = [0.0] * current_hour

            # From NOW to midnight tomorrow
            hours_today = 24 - current_hour
            n_result = hours_today + 24  # +24 for tomorrow

        return {
            "n_control": n_control,
            "n_result": n_result,
            "current_slot": current_slot,
            "current_hour": current_hour,
            "pad_past": pad_past,
        }

    def _extract_battery_parameters(self, evopt):
        """
        Extract battery parameters from EVopt request.

        Returns:
            dict with keys: s_max, eta_c, eta_d, c_max, d_max
        """
        params = {
            "s_max": None,
            "eta_c": 0.95,
            "eta_d": 0.95,
            "c_max": None,
            "d_max": None,
        }

        if not isinstance(evopt, dict):
            return params

        breq = evopt.get("batteries")
        if not isinstance(breq, list) or len(breq) == 0:
            return params

        b0r = breq[0]

        # Extract s_max
        try:
            params["s_max"] = float(b0r.get("s_max", 0.0))
            if params["s_max"] == 0:
                params["s_max"] = None
        except (ValueError, TypeError):
            params["s_max"] = None

        # Extract eta_c and eta_d
        try:
            params["eta_c"] = float(evopt.get("eta_c", b0r.get("eta_c", 0.95) or 0.95))
        except (ValueError, TypeError):
            params["eta_c"] = 0.95

        try:
            params["eta_d"] = float(evopt.get("eta_d", b0r.get("eta_d", 0.95) or 0.95))
        except (ValueError, TypeError):
            params["eta_d"] = 0.95

        # Extract c_max and d_max
        try:
            params["c_max"] = float(b0r.get("c_max", 0.0)) or None
        except (ValueError, TypeError):
            params["c_max"] = None

        try:
            params["d_max"] = float(b0r.get("d_max", 0.0)) or None
        except (ValueError, TypeError):
            params["d_max"] = None

        return params

    def _extract_response_arrays(self, resp, n_control, n_result):
        """
        Extract data arrays from EVopt response.

        Returns:
            dict with keys:
            - full: Full arrays for control processing (n_control length)
            - result: Truncated arrays for result processing (n_result length)
        """
        batteries_resp = resp.get("batteries") or []
        first_batt = batteries_resp[0] if batteries_resp else {}

        # Extract full arrays (for control processing)
        charging_power_full = list(
            first_batt.get("charging_power") or [0.0] * n_control
        )[:n_control]
        discharging_power_full = list(
            first_batt.get("discharging_power") or [0.0] * n_control
        )[:n_control]
        grid_import_full = list(resp.get("grid_import") or [0.0] * n_control)[
            :n_control
        ]
        grid_export_full = list(resp.get("grid_export") or [0.0] * n_control)[
            :n_control
        ]
        soc_wh_full = list(first_batt.get("state_of_charge") or [])[:n_control]

        return {
            "full": {
                "charging_power": charging_power_full,
                "discharging_power": discharging_power_full,
                "grid_import": grid_import_full,
                "grid_export": grid_export_full,
                "soc_wh": soc_wh_full,
            },
            "result": {
                "charging_power": charging_power_full[:n_result],
                "discharging_power": discharging_power_full[:n_result],
                "grid_import": grid_import_full[:n_result],
                "grid_export": grid_export_full[:n_result],
                "soc_wh": soc_wh_full[:n_result],
            },
        }

    def _extract_pricing_data(self, evopt, n):
        """
        Extract and normalize pricing data from EVopt request.

        Returns:
            dict with keys: p_n, p_e, electricity_price (all arrays of length n)
        """
        p_n = None
        p_e = None
        electricity_price = [None] * n

        if not isinstance(evopt, dict):
            return {
                "p_n": [0.0] * n,
                "p_e": [0.0] * n,
                "electricity_price": [0.0] * n,
            }

        ts = evopt.get("time_series", {}) or {}
        p_n = ts.get("p_N")
        p_e = ts.get("p_E")

        # Normalize p_N
        if isinstance(p_n, list):
            if len(p_n) >= n:
                p_n = [float(x) for x in p_n[:n]]
            elif p_n:
                p_n = [float(x) for x in p_n] + [float(p_n[-1])] * (n - len(p_n))
            else:
                p_n = [0.0] * n
            electricity_price = p_n.copy()
        elif isinstance(p_n, (int, float)):
            p_n = [float(p_n)] * n
            electricity_price = p_n.copy()
        else:
            p_n = [0.0] * n
            electricity_price = [0.0] * n

        # Normalize p_E
        if isinstance(p_e, list):
            if len(p_e) >= n:
                p_e = [float(x) for x in p_e[:n]]
            elif p_e:
                p_e = [float(x) for x in p_e] + [float(p_e[-1])] * (n - len(p_e))
            else:
                p_e = [0.0] * n
        elif isinstance(p_e, (int, float)):
            p_e = [float(p_e)] * n
        else:
            p_e = [0.0] * n

        return {
            "p_n": p_n,
            "p_e": p_e,
            "electricity_price": electricity_price,
        }

    def _process_control_arrays(self, full_arrays, battery_params, n_result):
        """
        Process control arrays (ac_charge, dc_charge, discharge_allowed).

        NOTE: Processes n_result values which will be combined with pad_past
        to create the full n_control-length arrays.

        Returns:
            dict with keys: ac_charge, dc_charge, discharge_allowed
        """
        charging_power = full_arrays["charging_power"]
        discharging_power = full_arrays["discharging_power"]
        grid_import = full_arrays["grid_import"]

        # Determine c_max and d_max (fallback to observed max)
        c_max = battery_params["c_max"]
        d_max = battery_params["d_max"]

        if not c_max:
            try:
                observed_max = (
                    max([float(x) for x in charging_power]) if charging_power else 0.0
                )
                c_max = observed_max if observed_max > 0 else 1.0
            except (ValueError, TypeError):
                c_max = 1.0

        if not d_max:
            try:
                observed_max = (
                    max([float(x) for x in discharging_power])
                    if discharging_power
                    else 0.0
                )
                d_max = observed_max if observed_max > 0 else 1.0
            except (ValueError, TypeError):
                d_max = 1.0

        # Process arrays - only n_result values (will be padded later)
        ac_charge = []
        dc_charge = []
        for i in range(n_result):  # CHANGED: was n_control
            if i < len(charging_power):
                cp = float(charging_power[i])
                gi = float(grid_import[i]) if i < len(grid_import) else 0.0

                # ac_charge: fraction of charging from grid
                charge_from_grid = min(cp, gi)
                try:
                    frac = charge_from_grid / float(c_max) if float(c_max) > 0 else 0.0
                except (ValueError, TypeError):
                    frac = 0.0

                if frac != frac:  # NaN check
                    frac = 0.0
                if gi <= 0:  # No grid import = PV-only charging
                    frac = 0.0

                ac_charge.append(max(0.0, min(1.0, frac)))

                # dc_charge: 1 if charging, 0 otherwise
                dc_charge.append(1.0 if cp > 0.0 else 0.0)
            else:
                ac_charge.append(0.0)
                dc_charge.append(0.0)

        # discharge_allowed
        discharge_allowed = []
        for i in range(n_result):  # CHANGED: was n_control
            if i < len(discharging_power):
                discharge_allowed.append(1 if float(discharging_power[i]) > 1e-9 else 0)
            else:
                discharge_allowed.append(0)

        return {
            "ac_charge": ac_charge,
            "dc_charge": dc_charge,
            "discharge_allowed": discharge_allowed,
            "c_max": c_max,
            "d_max": d_max,
        }

    def _process_result_arrays(
        self, result_arrays, pricing_data, battery_params, evopt, n
    ):
        """
        Process result arrays (costs, revenues, losses, SOC, etc.).

        Returns:
            dict with all result data
        """
        charging_power = result_arrays["charging_power"]
        discharging_power = result_arrays["discharging_power"]
        grid_import = result_arrays["grid_import"]
        grid_export = result_arrays["grid_export"]
        soc_wh = result_arrays["soc_wh"]

        p_n = pricing_data["p_n"]
        p_e = pricing_data["p_e"]
        eta_c = battery_params["eta_c"]
        eta_d = battery_params["eta_d"]

        # Calculate costs and revenues
        kosten_per_hour = []
        einnahmen_per_hour = []
        for i in range(n):
            gi = float(grid_import[i]) if i < len(grid_import) else 0.0
            ge = float(grid_export[i]) if i < len(grid_export) else 0.0
            pr = float(p_n[i]) if i < len(p_n) else 0.0
            pe = float(p_e[i]) if i < len(p_e) else 0.0

            kosten_per_hour.append(gi * pr)
            einnahmen_per_hour.append(ge * pe)

        # Calculate battery losses
        verluste_per_hour = []
        for i in range(n):
            ch = float(charging_power[i]) if i < len(charging_power) else 0.0
            dch = float(discharging_power[i]) if i < len(discharging_power) else 0.0
            loss = ch * (1.0 - eta_c) + dch * (1.0 - eta_d)
            verluste_per_hour.append(loss)

        # Calculate SOC percentage
        akku_soc_pct = self._calculate_soc_percentage(soc_wh, battery_params["s_max"])

        # Get household load from request
        last_wh = self._extract_household_load(evopt, grid_import, n)

        return {
            "Last_Wh_pro_Stunde": last_wh,
            "Einnahmen_Euro_pro_Stunde": [float(x) for x in einnahmen_per_hour],
            "Kosten_Euro_pro_Stunde": [float(x) for x in kosten_per_hour],
            "Gesamt_Verluste": float(sum(verluste_per_hour)),
            "Gesamtbilanz_Euro": float(sum(einnahmen_per_hour) - sum(kosten_per_hour)),
            "Gesamteinnahmen_Euro": float(sum(einnahmen_per_hour)),
            "Gesamtkosten_Euro": float(sum(kosten_per_hour)),
            "Home_appliance_wh_per_hour": [0.0] * n,
            "Netzbezug_Wh_pro_Stunde": [float(x) for x in grid_import[:n]],
            "Netzeinspeisung_Wh_pro_Stunde": [float(x) for x in grid_export[:n]],
            "Verluste_Pro_Stunde": [float(x) for x in verluste_per_hour],
            "akku_soc_pro_stunde": akku_soc_pct if akku_soc_pct else [],
            "Electricity_price": pricing_data["electricity_price"],
            "EAuto_SoC_pro_Stunde": [],  # Placeholder
        }

    def _calculate_soc_percentage(self, soc_wh, s_max):
        """
        Convert SOC from Wh to percentage.

        Returns:
            list of percentages or empty list
        """
        if not soc_wh:
            return []

        # Determine reference capacity
        ref = s_max
        if not ref:
            try:
                ref = max([float(x) for x in soc_wh]) if soc_wh else None
            except (ValueError, TypeError):
                ref = None

        if not ref or ref <= 0:
            return []

        akku_soc_pct = []
        for v in soc_wh:
            try:
                pct = float(v) / float(ref) * 100.0
            except (ValueError, TypeError):
                pct = 0.0
            akku_soc_pct.append(pct)

        return akku_soc_pct

    def _extract_household_load(self, evopt, grid_import_fallback, n):
        """
        Extract household load from EVopt request, fallback to grid_import.

        Returns:
            list of load values (Wh)
        """
        if not isinstance(evopt, dict):
            return [float(x) for x in grid_import_fallback[:n]]

        ts = evopt.get("time_series", {}) or {}
        gt = ts.get("gt")

        if not isinstance(gt, list) or len(gt) == 0:
            return [float(x) for x in grid_import_fallback[:n]]

        # Normalize/trim/pad gt to length n
        if len(gt) >= n:
            return [float(x) for x in gt[:n]]
        else:
            last_val = float(gt[-1])
            return [float(x) for x in gt] + [last_val] * (n - len(gt))

    def _build_eos_response(
        self, control_arrays, result_data, time_params, resp, evcc_resp
    ):
        """
        Build final EOS response structure.

        Returns:
            dict with complete EOS response
        """
        eos_resp = {
            "ac_charge": time_params["pad_past"]
            + [float(x) for x in control_arrays["ac_charge"]],
            "dc_charge": time_params["pad_past"]
            + [float(x) for x in control_arrays["dc_charge"]],
            "discharge_allowed": time_params["pad_past"]
            + [int(x) for x in control_arrays["discharge_allowed"]],
            "eautocharge_hours_float": None,
            "result": result_data,
        }

        # Handle start_solution (if present in response)
        start_solution = self._extract_start_solution(
            resp, evcc_resp, time_params["n_result"]  # CHANGED: was n_control
        )
        if start_solution:
            eos_resp["start_solution"] = time_params["pad_past"] + start_solution

        # Handle washingstart (if present)
        washingstart = resp.get("washingstart")
        if washingstart is not None:
            eos_resp["washingstart"] = time_params["pad_past"] + washingstart

        # Attach eauto_obj if present
        if "eauto_obj" in resp:
            eos_resp["eauto_obj"] = resp.get("eauto_obj")

        # Add timestamp
        try:
            eos_resp["timestamp"] = datetime.now(self.time_zone).isoformat()
        except (ValueError, TypeError):
            eos_resp["timestamp"] = datetime.now().isoformat()

        return eos_resp

    def _extract_start_solution(
        self, resp, evcc_resp, n_result
    ):  # CHANGED: was n_control
        """
        Extract start_solution from response.

        Returns:
            list of n_result values (will be padded later)
        """
        if isinstance(resp.get("start_solution"), list):
            return [
                float(x) if isinstance(x, (int, float)) else 0
                for x in resp.get("start_solution")[:n_result]  # CHANGED
            ]

        eauto_obj = resp.get("eauto_obj") or evcc_resp.get("eauto_obj")
        if isinstance(eauto_obj, dict) and isinstance(
            eauto_obj.get("charge_array"), list
        ):
            return [
                int(1 if float(x) > 0 else 0)
                for x in eauto_obj.get("charge_array")[:n_result]  # CHANGED
            ]

        return [0] * n_result  # CHANGED
