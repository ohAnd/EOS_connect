/**
 * Schedule Manager for EOS Connect
 * Handles schedule display and management functionality
 * Extracted from legacy index.html
 */

class ScheduleManager {
    constructor() {
        console.log('[ScheduleManager] Initialized');
    }

    /**
     * Initialize schedule manager
     */
    init() {
        console.log('[ScheduleManager] Manager initialized');
    }

    /**
     * Show schedule for next 24 hours
     */
    showSchedule(data_request, data_response) {
        //console.log("------- showSchedule -------");
        var serverTime = new Date(data_response["timestamp"]);
        var currentHour = serverTime.getHours();
        var discharge_allowed = data_response["discharge_allowed"];
        var ac_charge = data_response["ac_charge"];

        // Add timezone indicator to schedule header
        document.getElementById('load_schedule_header').innerHTML =
            ` Schedule next 24 hours <small>(Local Time)</small>`;

        ac_charge = ac_charge.map((value, index) => value * max_charge_power_w);
        var priceData = data_response["result"]["Electricity_price"];
        var expenseData = data_response["result"]["Kosten_Euro_pro_Stunde"];
        var incomeData = data_response["result"]["Einnahmen_Euro_pro_Stunde"];

        // clear all entries in div discharge_scheduler
        var tableBody = document.querySelector("#discharge_scheduler .table-body");
        tableBody.innerHTML = '';

        priceData.forEach((value, index) => {
            if (index > 23) return;

            if ((index + 1) % 4 === 0 && (index + 1) !== 0) {
                var row = document.createElement('div');
                row.className = 'table-row';
                row.style.borderBottom = "1px solid #707070";
                row.style.height = "5px";
                tableBody.appendChild(row); // Append the row to the table body
                var row = document.createElement('div');
                row.className = 'table-row';
                row.style.height = "5px";
                tableBody.appendChild(row); // Append the row to the table body
            }

            var row = document.createElement('div');
            row.className = 'table-row';

            var cell1 = document.createElement('div');
            cell1.className = 'table-cell';
            // cell1.innerHTML = ((index + currentHour) % 24) + ":00";
            const labelTime = new Date(serverTime.getTime() + (index * 60 * 60 * 1000));
            cell1.innerHTML = labelTime.getHours().toString().padStart(2, '0') + ":00";
            
            cell1.style.textAlign = "right";
            row.appendChild(cell1);

            var cell2 = document.createElement('div');
            cell2.className = 'table-cell';
            const buttonDiv = document.createElement('div');
            buttonDiv.style.border = "1px solid #ccc";
            buttonDiv.style.borderRadius = "5px";
            buttonDiv.style.borderColor = "darkgray";
            buttonDiv.style.width = "50px";
            buttonDiv.style.display = "inline-block";
            buttonDiv.style.textAlign = "center";

            if (index === 0 && inverter_mode_num > 2) {
                // override first hour - if eos connect overriding eos
                if (inverter_mode_num === 3) { // MODE_AVOID_DISCHARGE_EVCC_FAST
                    //buttonDiv.style.backgroundColor = "#3399FF";
                    buttonDiv.style.color = COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST;
                    buttonDiv.innerHTML = " <i class='fa-solid fa-charging-station'></i> <i class='fa-solid fa-lock'></i> ";
                } else if (inverter_mode_num === 4) { // MODE_DISCHARGE_ALLOWED_EVCC_PV
                    //buttonDiv.style.backgroundColor = "#3399FF";
                    buttonDiv.style.color = COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV;
                    buttonDiv.innerHTML = " <i class='fa-solid fa-charging-station'></i> <i class='fa-solid fa-battery-half'></i> ";
                } else if (inverter_mode_num === 5) { //MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV
                    //buttonDiv.style.backgroundColor = "rgb(255, 144, 144)";
                    buttonDiv.style.color = COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV;
                    buttonDiv.innerHTML = " <i class='fa-solid fa-charging-station'></i> <i class='fa-solid fa-battery-half'></i> ";
                }
            } else if (discharge_allowed[(index + currentHour)] === 1) {
                //buttonDiv.style.backgroundColor = "grey";
                buttonDiv.style.color = COLOR_MODE_DISCHARGE_ALLOWED;
                buttonDiv.innerHTML = "<i class='fa-solid fa-battery-half'></i>";
            } else if (ac_charge[(index + currentHour)]) {
                //buttonDiv.style.backgroundColor = color_bat_grid_charging;
                buttonDiv.style.color = COLOR_MODE_CHARGE_FROM_GRID;
                let acChargeValue = ac_charge[(index + currentHour)] === 0 ? "" : (ac_charge[(index + currentHour)] / 1000).toFixed(1) + '<span style="font-size: xx-small;"> kWh<span>';
                buttonDiv.innerHTML = "<i class='fa-solid fa-plug-circle-bolt'></i> " + acChargeValue;
                buttonDiv.style.padding = "0 10px";
                buttonDiv.style.width = "";
            } else {
                //buttonDiv.style.backgroundColor = "";
                buttonDiv.style.color = COLOR_MODE_AVOID_DISCHARGE;
                buttonDiv.innerHTML = "<i class='fa-solid fa-lock'></i>";
            }

            cell2.appendChild(buttonDiv);
            cell2.style.textAlign = "center";
            row.appendChild(cell2);

            var cell3 = document.createElement('div');
            cell3.className = 'table-cell';
            cell3.innerHTML = (priceData[index] * 100000).toFixed(1);
            cell3.style.textAlign = "center";
            row.appendChild(cell3);

            var cell4 = document.createElement('div');
            cell4.className = 'table-cell';
            cell4.innerHTML = (expenseData[index]).toFixed(2);
            cell4.style.textAlign = "center";
            row.appendChild(cell4);

            var cell5 = document.createElement('div');
            cell5.className = 'table-cell';
            cell5.innerHTML = (incomeData[index]).toFixed(2);
            cell5.style.textAlign = "center";
            row.appendChild(cell5);

            tableBody.appendChild(row);
        });
    }
}

// Legacy compatibility function
function showSchedule(data_request, data_response) {
    if (scheduleManager) {
        scheduleManager.showSchedule(data_request, data_response);
    }
}
