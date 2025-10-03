/**
 * EVCC Manager for EOS Connect
 * Handles Electric Vehicle Charging Control functionality
 * Extracted from legacy index.html
 */

class EVCCManager {
    constructor() {
        console.log('[EVCCManager] Initialized');
    }

    /**
     * Initialize EVCC manager
     */
    init() {
        console.log('[EVCCManager] Manager initialized');
    }

    /**
     * Get charging color and text based on EVCC mode and state
     */
    getChargingColorAndText(evcc_mode, evcc_state) {
        let color = "white";
        let text = "N/A";

        if (evcc_mode === "off") {
            text = "Off";
        } else if (evcc_mode === "pv") {
            text = "PV";
            if (evcc_state) {
                color = COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV;
            }
        } else if (evcc_mode === "minpv") {
            text = "Min+PV";
            if (evcc_state) {
                color = COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV;
            }
        } else if (evcc_mode === "now") {
            text = "Fast charge";
            if (evcc_state) {
                color = COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST;
            }
        } else if (evcc_mode === "pv+now" || evcc_mode === "minpv+now") {
            text = "Smart Cost Fast";
            if (evcc_state) {
                color = COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST;
            }
        }

        return { color, text };
    }

    /**
     * Show single loadpoint UI
     */
    showSingleLoadpoint(session) {
        const singleTable = document.getElementById('ecar_charging_table_single');
        const multipleTable = document.getElementById('ecar_charging_table_multiple');
        const offTable = document.getElementById('ecar_charging_table_off');
        
        if (singleTable) singleTable.style.display = "";
        if (multipleTable) multipleTable.style.display = "none";
        if (offTable) offTable.style.display = "none";

        const displayName = session["vehicleName"] || "Unknown Vehicle";
        
        // Update UI elements if they exist - based on legacy implementation
        const vehicleNameElement = document.getElementById('vehicle_name');
        if (vehicleNameElement) {
            vehicleNameElement.innerText = displayName;
        }
        
        // Update all single vehicle elements - based on legacy implementation
        if (session["vehicleName"]) {
            let displayName = session["vehicleName"] || "Unknown Vehicle";
            if (displayName.length > 25)
                displayName = displayName.substring(0, 25) + "...";
            writeIfValueChanged('ecar_charging_name', displayName);
        }
        
        if (session["vehicleSoc"] !== undefined) {
            writeIfValueChanged('ecar_charging_soc', (session["vehicleSoc"]).toFixed(1) + " %");
        }
        
        if (session["vehicleOdometer"] !== undefined) {
            writeIfValueChanged('ecar_charging_odometer', session["vehicleOdometer"] + " km");
        }
        
        if (session["vehicleRange"] !== undefined) {
            writeIfValueChanged('ecar_charging_range', session["vehicleRange"] + " km");
        }
        
        if (session["chargedEnergy"] !== undefined) {
            writeIfValueChanged('ecar_charging_charged', (session["chargedEnergy"] / 1000).toFixed(1) + " kWh");
        }
        
        if (session["chargeRemainingEnergy"] !== undefined) {
            writeIfValueChanged('ecar_charging_charged_remain', (session["chargeRemainingEnergy"] / 1000).toFixed(1) + " kWh");
        }
        
        if (session["chargeDuration"] !== undefined) {
            let chargeDuration = session["chargeDuration"];
            let hours = Math.floor(chargeDuration / 3600);
            let minutes = Math.floor((chargeDuration % 3600) / 60);
            writeIfValueChanged('ecar_charging_duration', hours.toString().padStart(2, '0') + ":" + minutes.toString().padStart(2, '0'));
        }
        
        if (session["chargeRemainingDuration"] !== undefined) {
            let chargeRemainingDuration = session["chargeRemainingDuration"];
            let remainingHours = Math.floor(chargeRemainingDuration / 3600);
            let remainingMinutes = Math.floor((chargeRemainingDuration % 3600) / 60);
            writeIfValueChanged('ecar_charging_duration_remain', remainingHours.toString().padStart(2, '0') + ":" + remainingMinutes.toString().padStart(2, '0'));
        }
    }

    /**
     * Show car charging data - handles EVCC display logic
     */
    showCarChargingData(data_controls) {
        // car charging - current states of EVCC
        let evcc_mode = data_controls["evcc"]["charging_mode"];
        let evcc_state = data_controls["evcc"]["charging_state"];

        this.updateEVCCStatus(evcc_mode, evcc_state);

        let numOfConnectedVehicles = data_controls["evcc"]["current_sessions"].filter(session => session["connected"]).length;
        
        if (numOfConnectedVehicles > 1) {
            this.showMultipleLoadpoints(data_controls["evcc"]["current_sessions"]);
        } else if (numOfConnectedVehicles == 1) {
            let entryOfConnectedVehicle = data_controls["evcc"]["current_sessions"].find(session => session["connected"]);
            this.showSingleLoadpoint(entryOfConnectedVehicle);
        } else {
            this.showNoConnectedVehicles();
        }
    }

    /**
     * Show multiple loadpoints (multiple connected vehicles)
     */
    showMultipleLoadpoints(sessions) {
        document.getElementById('ecar_charging_table_single').style.display = "none";
        document.getElementById('ecar_charging_table_multiple').style.display = "";
        document.getElementById('ecar_charging_table_off').style.display = "none";

        sessions.forEach((session) => {
            if (session["connected"]) {
                let vehicle_name = session["vehicleName"] || "Unknown Vehicle";
                vehicle_name = vehicle_name.replace(/[^a-zA-Z0-9_]/g, '_'); // sanitize vehicle name for id

                let displayName = session["vehicleName"] || "Unknown Vehicle";
                // reduce vehicle name to 10 characters
                if (displayName.length > 10)
                    displayName = displayName.substring(0, 10) + "...";

                let row = document.createElement('tr');
                // set id for the row
                row.id = 'ecar_charging_row_' + vehicle_name;
                row.style.color = this.getChargingColorAndText(session["mode"], session["charging"]).color; // set color based on charging state
                row.innerHTML = `
                    <td style="text-align: left; cursor: help;" colspan="2" title="name of vehicle in EVCC">
                        <i class="fa-solid fa-car"></i> <span id="ecar_charging_name_${vehicle_name}">${displayName}</span>
                    </td>
                    <td class="valueChange" style="text-align: left; cursor: help;" colspan="2" title="eCar SOC">
                        <span id="ecar_charging_soc_${vehicle_name}">${(session["vehicleSoc"]).toFixed(1)} %</span> 
                        <i class="fa-solid fa-car-battery"></i>
                    </td>
                    <td style="text-align: right; cursor: help;" class="valueChange" id="ecar_charging_charged_${vehicle_name}"
                        title="already charged energy">${(session["chargedEnergy"] / 1000).toFixed(1) + " kWh"}</td>
                    <td style="text-align: right;"><i class="fa-solid fa-right-long"> <i
                                class="fa-solid fa-bolt"> </i></td>
                    <td style="text-align: right; border-left-width: 1px; border-left-style: solid;"> <i
                            class="fa-solid fa-road"></i></td>
                    <td style="text-align: right; cursor: help;" class="valueChange" style="text-align: right;"
                        id="ecar_charging_range_${vehicle_name}" title="current vehicle range">${session["vehicleRange"] + " km"}</td>
                `;
                // check if the row already exists
                let existingRow = document.getElementById('ecar_charging_row_' + vehicle_name);
                if (existingRow) {
                    // update the existing row
                    existingRow.style.color = row.style.color; // update color based on charging state
                    existingRow.innerHTML = row.innerHTML;
                } else {
                    // append the new row to the table
                    document.getElementById('ecar_charging_table_multiple').appendChild(row);
                }
            }
        });
        
        // cleanup, remove rows for vehicles that are not connected anymore
        let connectedVehicles = sessions
            .filter(session => session["connected"])
            .map(session => (session["vehicleName"] || "Unknown Vehicle").replace(/[^a-zA-Z0-9_]/g, '_'));
        
        let rows = document.querySelectorAll('#ecar_charging_table_multiple tr');
        rows.forEach(row => {
            // check if the row id starts with 'ecar_charging_row_' and if the vehicle is not connected anymore
            // and remove it if not connected
            if (row.id.startsWith('ecar_charging_row_') && !connectedVehicles.includes(row.id.replace('ecar_charging_row_', ''))) {
                console.log("Removing row: ", row.id);
                row.remove();
            }
        });
        
        if (rows.length === 0) {
            this.showNoConnectedVehicles();
        }
    }

    /**
     * Show no connected vehicles state
     */
    showNoConnectedVehicles() {
        document.getElementById('ecar_charging_table_single').style.display = "none";
        document.getElementById('ecar_charging_table_multiple').style.display = "none";
        document.getElementById('ecar_charging_table_off').style.display = "";
    }

    /**
     * Update EVCC status display
     */
    updateEVCCStatus(evcc_mode, evcc_state) {
        const { color, text } = this.getChargingColorAndText(evcc_mode, evcc_state);
        
        const modeElement = document.getElementById('evcc_mode');
        const stateElement = document.getElementById('evcc_state');
        
        if (modeElement) {
            modeElement.innerText = text;
            modeElement.style.color = color;
        }
        
        if (stateElement) {
            stateElement.innerText = evcc_state ? "Charging" : "Idle";
            stateElement.style.color = color;
        }
    }
}

// Legacy compatibility functions
function getChargingColorAndText(evcc_mode, evcc_state) {
    if (evccManager) {
        return evccManager.getChargingColorAndText(evcc_mode, evcc_state);
    }
    return { color: "white", text: "N/A" };
}

function showSingleLoadpoint(session) {
    if (evccManager) {
        evccManager.showSingleLoadpoint(session);
    }
}

function showCarChargingData(data_controls) {
    if (evccManager) {
        evccManager.showCarChargingData(data_controls);
    }
}