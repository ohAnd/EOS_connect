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

        Produces a fuller EOS-shaped response using the sample `src/json/optimize_response.json`
        as guidance. The mapping is conservative and uses available EVCC fields:

        - ac_charge, dc_charge, discharge_allowed, start_solution
        - result.* arrays: Last_Wh_pro_Stunde, EAuto_SoC_pro_Stunde,
            Einnahmen_Euro_pro_Stunde, Kosten_Euro_pro_Stunde, Netzbezug_Wh_pro_Stunde,
            Netzeinspeisung_Wh_pro_Stunde, Verluste_Pro_Stunde, akku_soc_pro_stunde,
            Electricity_price
        - numeric summaries: Gesamt_Verluste, Gesamtbilanz_Euro, Gesamteinnahmen_Euro,
            Gesamtkosten_Euro
        - eauto_obj, washingstart, timestamp

        evcc_resp: dict (raw EVCC JSON) or {"response": {...}}.
        evopt: optional EVCC request dict (used to read p_N, p_E, eta_c, eta_d,
            battery s_max/c_max).
        """
        # defensive guard
        if not isinstance(evcc_resp, dict):
            logger.debug(
                "[EOS] EVCC transform response - input not a dict, returning empty dict"
            )
            return {}

        # EVCC might wrap actual payload under "response"
        resp = evcc_resp.get("response", evcc_resp)

        # Set total hours and slice for future
        current_hour = datetime.now(self.time_zone).hour
        n_total = 48  # Total hours from midnight today to midnight tomorrow
        n_future = n_total - current_hour  # Hours from now to end of tomorrow
        n = n_future  # Override n to focus on future horizon
        if self.time_frame_base == 900:
            n = n_future * 4  # 15-min intervals

        # primary battery arrays (first battery)
        batteries_resp = resp.get("batteries") or []
        first_batt = batteries_resp[0] if batteries_resp else {}
        charging_power = list(first_batt.get("charging_power") or [0.0] * n)[:n]
        discharging_power = list(first_batt.get("discharging_power") or [0.0] * n)[:n]
        soc_wh = list(first_batt.get("state_of_charge") or [])[:n]

        # grid arrays
        grid_import = list(resp.get("grid_import") or [0.0] * n)[:n]
        grid_export = list(resp.get("grid_export") or [0.0] * n)[:n]

        # harvest pricing from evopt when available (per-Wh units)
        p_n = None
        p_e = None
        electricity_price = [None] * n
        if isinstance(evopt, dict):
            ts = evopt.get("time_series", {}) or {}
            p_n = ts.get("p_N")
            p_e = ts.get("p_E")
            # if p_N/p_E are lists, normalize length
            if isinstance(p_n, list):
                p_n = (
                    [float(x) for x in p_n[:n]]
                    + [float(p_n[-1])] * max(0, n - len(p_n))
                    if p_n
                    else None
                )
            if isinstance(p_e, list):
                p_e = (
                    [float(x) for x in p_e[:n]]
                    + [float(p_e[-1])] * max(0, n - len(p_e))
                    if p_e
                    else None
                )
            if isinstance(p_n, list):
                electricity_price = [float(x) for x in p_n[:n]]
            elif isinstance(p_n, (int, float)):
                electricity_price = [float(p_n)] * n

        # fallback price arrays if missing
        if not any(isinstance(x, (int, float)) for x in electricity_price):
            electricity_price = [0.0] * n
        if p_n is None:
            p_n = electricity_price
        if p_e is None:
            p_e = [0.0] * n

        # battery parameters from request if present (s_max in Wh, eta_c, eta_d)
        s_max_req = None
        eta_c = None
        eta_d = None
        if isinstance(evopt, dict):
            breq = evopt.get("batteries")
            if isinstance(breq, list) and len(breq) > 0:
                b0r = breq[0]
                try:
                    s_max_req = float(b0r.get("s_max", 0.0))
                except (ValueError, TypeError):
                    s_max_req = None
                try:
                    eta_c = float(evopt.get("eta_c", b0r.get("eta_c", 0.95) or 0.95))
                except (ValueError, TypeError):
                    eta_c = 0.95
                try:
                    eta_d = float(evopt.get("eta_d", b0r.get("eta_d", 0.95) or 0.95))
                except (ValueError, TypeError):
                    eta_d = 0.95
        # Set defaults
        eta_c = eta_c if eta_c is not None else 0.95
        eta_d = eta_d if eta_d is not None else 0.95
        s_max_val = s_max_req if s_max_req not in (None, 0) else None

        # compute ac_charge fraction: charging_power normalized to c_max from request
        # or observed max
        c_max = None
        d_max = None
        if isinstance(evopt, dict):
            breq = evopt.get("batteries")
            if isinstance(breq, list) and len(breq) > 0:
                try:
                    c_max = float(breq[0].get("c_max", 0.0))
                except (ValueError, TypeError):
                    c_max = None
                try:
                    d_max = float(breq[0].get("d_max", 0.0))
                except (ValueError, TypeError):
                    d_max = None
        # fallback observed maxima
        try:
            if not c_max:
                observed_max_ch = (
                    max([float(x) for x in charging_power]) if charging_power else 0.0
                )
                c_max = observed_max_ch if observed_max_ch > 0 else 1.0
            if not d_max:
                observed_max_dch = (
                    max([float(x) for x in discharging_power])
                    if discharging_power
                    else 0.0
                )
                d_max = observed_max_dch if observed_max_dch > 0 else 1.0
        except (ValueError, TypeError):
            c_max = c_max or 1.0
            d_max = d_max or 1.0

        ac_charge = []
        for i, v in enumerate(charging_power):
            # Use grid import if charging_power exceeds grid_import
            charge_from_grid = min(float(v), float(grid_import[i]))
            try:
                frac = charge_from_grid / float(c_max) if float(c_max) > 0 else 0.0
            except (ValueError, TypeError):
                frac = 0.0
            if frac != frac:
                frac = 0.0
            ac_charge.append(max(0.0, min(1.0, frac)))

        # Adjust ac_charge: set to 0 if no grid import (PV-only charging)
        for i in range(n):
            if grid_import[i] <= 0:
                ac_charge[i] = 0.0

        # dc_charge: mark 1.0 if charging_power > 0 (conservative)
        dc_charge = [1.0 if float(v) > 0.0 else 0.0 for v in charging_power]

        # discharge_allowed: 1 if discharging_power > tiny epsilon
        discharge_allowed = [1 if float(v) > 1e-9 else 0 for v in discharging_power]

        # start_solution: prefer resp['start_solution'] if present, else try
        # eauto_obj.charge_array -> ints, otherwise zeros
        start_solution = None
        if isinstance(resp.get("start_solution"), list):
            # coerce to numbers
            start_solution = [
                float(x) if isinstance(x, (int, float)) else 0
                for x in resp.get("start_solution")[:n]
            ]
        else:
            eauto_obj = resp.get("eauto_obj") or evcc_resp.get("eauto_obj")
            if isinstance(eauto_obj, dict) and isinstance(
                eauto_obj.get("charge_array"), list
            ):
                # map boolean/float charge_array to integers (placeholder)
                start_solution = [
                    int(1 if float(x) > 0 else 0)
                    for x in eauto_obj.get("charge_array")[:n]
                ]
        if start_solution is None:
            start_solution = [0] * n

        # washingstart if present
        washingstart = resp.get("washingstart")

        # compute per-hour costs and revenues in Euro (using €/Wh units from p_N/p_E)
        kosten_per_hour = []
        einnahmen_per_hour = []
        for i in range(n):
            gi = float(grid_import[i]) if i < len(grid_import) else 0.0
            ge = float(grid_export[i]) if i < len(grid_export) else 0.0
            pr = (
                float(p_n[i])
                if isinstance(p_n, list) and i < len(p_n)
                else float(p_n[i]) if isinstance(p_n, list) and len(p_n) > 0 else 0.0
            )
            pe = (
                float(p_e[i])
                if isinstance(p_e, list) and i < len(p_e)
                else (float(p_e[i]) if isinstance(p_e, list) and len(p_e) > 0 else 0.0)
            )
            # if p_N/p_E are scalars (should be lists), handle above; fallback zero if missing
            if isinstance(p_n, (int, float)):
                pr = float(p_n)
            if isinstance(p_e, (int, float)):
                pe = float(p_e)

            kosten = gi * pr
            einnahmen = ge * pe
            kosten_per_hour.append(kosten)
            einnahmen_per_hour.append(einnahmen)

        # estimate per-hour battery losses: charging_loss + discharging_loss
        verluste_per_hour = []
        for i in range(n):
            ch = float(charging_power[i]) if i < len(charging_power) else 0.0
            dch = float(discharging_power[i]) if i < len(discharging_power) else 0.0
            loss = ch * (1.0 - eta_c) + dch * (1.0 - eta_d)
            verluste_per_hour.append(loss)

        # Akku SoC percent per hour (if soc_wh available): convert to percent using s_max_req
        # or inferred max
        akku_soc_pct = []
        if soc_wh:
            # determine s_max reference: use s_max_req if provided, otherwise attempt to
            # infer from soc_wh max
            ref = s_max_val
            if not ref:
                try:
                    ref = max([float(x) for x in soc_wh]) if soc_wh else None
                except (ValueError, TypeError):
                    ref = None
            for v in soc_wh:
                try:
                    if ref and ref > 0:
                        pct = float(v) / float(ref) * 100.0
                    else:
                        pct = float(v)
                except (ValueError, TypeError):
                    pct = 0.0
                akku_soc_pct.append(pct)
        else:
            akku_soc_pct = []

        # totals
        gesamt_kosten = sum(kosten_per_hour) if kosten_per_hour else 0.0
        gesamt_einnahmen = sum(einnahmen_per_hour) if einnahmen_per_hour else 0.0
        gesamt_verluste = sum(verluste_per_hour) if verluste_per_hour else 0.0
        gesamt_bilanz = gesamt_einnahmen - gesamt_kosten

        # build result dict like optimize_response.json
        result = {}
        # Prefer household load ('gt') from the EVCC request if available,
        # otherwise fall back to EVCC response grid_import (parity with previous behavior).
        last_wh = None
        if isinstance(evopt, dict):
            ts = evopt.get("time_series", {}) or {}
            gt = ts.get("gt")
            if isinstance(gt, list) and len(gt) > 0:
                # normalize/trim/pad gt to length n (similar to other normalizations)
                if len(gt) >= n:
                    last_wh = [float(x) for x in gt[:n]]
                else:
                    last_val = float(gt[-1])
                    last_wh = [float(x) for x in gt] + [last_val] * (n - len(gt))
        # fallback to grid_import if gt not present or invalid
        if last_wh is None:
            last_wh = [float(x) for x in grid_import[:n]]

        result["Last_Wh_pro_Stunde"] = last_wh

        if akku_soc_pct:
            # EAuto_SoC_pro_Stunde - fallback to eauto object SOC or same as akku
            # percent if appropriate
            result["EAuto_SoC_pro_Stunde"] = (
                [
                    float(x)
                    for x in (
                        evcc_resp.get("eauto_obj", {}).get("soc_wh")
                        or (
                            []
                            if not isinstance(evcc_resp.get("eauto_obj", {}), dict)
                            else []
                        )
                    )[:n]
                ]
                if evcc_resp.get("eauto_obj")
                else []
            )
        # Einnahmen & Kosten per hour
        result["Einnahmen_Euro_pro_Stunde"] = [float(x) for x in einnahmen_per_hour]
        result["Kosten_Euro_pro_Stunde"] = [float(x) for x in kosten_per_hour]
        result["Gesamt_Verluste"] = float(gesamt_verluste)
        result["Gesamtbilanz_Euro"] = float(gesamt_bilanz)
        result["Gesamteinnahmen_Euro"] = float(gesamt_einnahmen)
        result["Gesamtkosten_Euro"] = float(gesamt_kosten)
        # Home appliance placeholder (zeros)
        result["Home_appliance_wh_per_hour"] = [0.0] * n
        result["Netzbezug_Wh_pro_Stunde"] = [float(x) for x in grid_import[:n]]
        result["Netzeinspeisung_Wh_pro_Stunde"] = [float(x) for x in grid_export[:n]]
        result["Verluste_Pro_Stunde"] = [float(x) for x in verluste_per_hour]
        if akku_soc_pct:
            result["akku_soc_pro_stunde"] = [float(x) for x in akku_soc_pct[:n]]
        # Electricity price array
        result["Electricity_price"] = [float(x) for x in electricity_price[:n]]

        # Pad past hours with zeros for control arrays
        pad_past = [0.0] * current_hour
        if self.time_frame_base == 900:
            pad_past = [0.0] * (current_hour * 4)

        eos_resp = {
            "ac_charge": pad_past + [float(x) for x in ac_charge],
            "dc_charge": pad_past + [float(x) for x in dc_charge],
            "discharge_allowed": pad_past + [int(x) for x in discharge_allowed],
            "eautocharge_hours_float": None,
            "result": result,  # result arrays remain unpadded (start from current time)
        }

        # attach eauto_obj if present in resp
        if "eauto_obj" in resp:
            eos_resp["eauto_obj"] = resp.get("eauto_obj")

        # map start_solution and washingstart if present
        eos_resp["start_solution"] = pad_past + start_solution
        if washingstart is not None:
            eos_resp["washingstart"] = pad_past + washingstart

        # timestamp
        try:
            eos_resp["timestamp"] = datetime.now(self.time_zone).isoformat()
        except (ValueError, TypeError):
            eos_resp["timestamp"] = datetime.now().isoformat()

        return eos_resp

    def _validate_evopt_request(self, evopt):
        """
        Validate EVopt-format optimization request.
        Returns: (bool, list[str]) - valid, errors
        """
        errors = []
        if not isinstance(evopt, dict):
            errors.append("evopt request must be a dictionary.")
        # Example: check required keys
        required_keys = ["strategy", "grid", "batteries", "time_series"]
        for key in required_keys:
            if key not in evopt:
                errors.append(f"Missing required key: {key}")
        return len(errors) == 0, errors
