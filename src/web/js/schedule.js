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
    showSchedule(data_request, data_response, data_controls) {
        //console.log("------- showSchedule -------");
        var serverTime = new Date(data_response["timestamp"]);
        var currentHour = serverTime.getHours();
        var discharge_allowed = data_response["discharge_allowed"];
        var ac_charge = data_response["ac_charge"];
        var inverter_mode_num = data_controls["current_states"]["inverter_mode_num"];
        var manual_override_active = data_controls["current_states"]["override_active"];
        var manual_override_active_until = data_controls["current_states"]["override_end_time"];
        var max_charge_power_w = data_request["pv_akku"] && data_request["pv_akku"].hasOwnProperty("max_ladeleistung_w") ? data_request["pv_akku"]["max_ladeleistung_w"] : data_request["pv_akku"] ? data_request["pv_akku"]["max_charge_power_w"] : 0;

        ac_charge = ac_charge.map((value, index) => value * max_charge_power_w);
        var priceData = data_response["result"]["Electricity_price"];
        var socData = data_response["result"]["akku_soc_pro_stunde"];
        var expenseData = data_response["result"]["Kosten_Euro_pro_Stunde"];
        var incomeData = data_response["result"]["Einnahmen_Euro_pro_Stunde"];
        document.getElementById('schedule_currency_symbol').innerText = localization.currency_symbol;
        document.getElementById('price_minor_unit_label').innerText = `${localization.currency_minor_unit}/kWh`;
        document.querySelector('#discharge_scheduler .table-header .table-cell[title]').setAttribute(
            'title',
            `Shows your cost (Pay) and income (Earn) for this time slot. Format: Pay / Earn in ${localization.currency_symbol}.`
        );

        // clear all entries in div discharge_scheduler
        var tableBody = document.querySelector("#discharge_scheduler .table-body");
        tableBody.innerHTML = '';

        var soc_before = null;

        priceData.forEach((value, index) => {
            if (index > 23) return;

            var currentModeAtHour = (ac_charge[(index + currentHour)]) ? 0 : (discharge_allowed[(index + currentHour)] === 1) ? 2 : 1;

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

            // car charging override active
            if (index === 0 && inverter_mode_num > 2) {
                // override first hour - if eos connect overriding eos by evcc
                buttonDiv.style.color = EOS_CONNECT_ICONS[inverter_mode_num].color;
                if (inverter_mode_num === 3) { // MODE_AVOID_DISCHARGE_EVCC_FAST
                    buttonDiv.innerHTML = " <i class='fa-solid " + EOS_CONNECT_ICONS[inverter_mode_num].icon + "'></i> <i class='fa-solid " + EOS_CONNECT_ICONS[1].icon + "'></i> ";
                } else if (inverter_mode_num === 4) { // MODE_DISCHARGE_ALLOWED_EVCC_PV
                    buttonDiv.innerHTML = " <i class='fa-solid " + EOS_CONNECT_ICONS[inverter_mode_num].icon + "'></i> <i class='fa-solid " + EOS_CONNECT_ICONS[2].icon + "'></i> ";
                } else if (inverter_mode_num === 5) { //MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV
                    buttonDiv.innerHTML = " <i class='fa-solid " + EOS_CONNECT_ICONS[inverter_mode_num].icon + "'></i> <i class='fa-solid " + EOS_CONNECT_ICONS[2].icon + "'></i> ";
                }
            }
            // 30 minutes in seconds = 30 * 60 = 1800
            else if (manual_override_active && (manual_override_active_until - (labelTime.getTime() / 1000)) > -(45 * 60)) {
                buttonDiv.style.color = EOS_CONNECT_ICONS[inverter_mode_num].color;
                buttonDiv.innerHTML = "<i style='color:orange;' class='fa-solid fa-triangle-exclamation'></i> <i class='fa-solid " + EOS_CONNECT_ICONS[inverter_mode_num].icon + "'></i>";
            } else {
                buttonDiv.style.color = EOS_CONNECT_ICONS[currentModeAtHour].color;
                buttonDiv.innerHTML += "<i class='fa-solid " + EOS_CONNECT_ICONS[currentModeAtHour].icon + "'></i>";
                if (ac_charge[(index + currentHour)]) {
                    buttonDiv.innerHTML += " <span style='font-size: xx-small;'>" + (ac_charge[(index + currentHour)] / 1000).toFixed(1) + " kWh</span>";
                    buttonDiv.style.padding = "0 10px";
                    buttonDiv.style.width = "";
                }
            }

            // prep SOC cell
            const socVal = Number(socData[index]);
            const socStr = socVal.toFixed(1);
            const socColor = (soc_before !== null && socVal > soc_before) ? 'darkgrey' : (soc_before !== null && socVal < soc_before) ? 'white' : 'lightgray';

            // store for next iteration
            soc_before = socVal; // store for next iteration

            // prep expensse / income cell
            const expenseVal = Number(expenseData[index]);
            const incomeVal = Number(incomeData[index]);
            const expenseStr = expenseVal.toFixed(2);
            const incomeStr = incomeVal.toFixed(2);
            const expenseColor = expenseVal >= 0.005 ? 'lightgray' : 'rgba(131, 131, 131, 1)';
            const incomeColor = incomeVal >= 0.005 ? 'lightgray' : 'rgba(131, 131, 131, 1)';
            const in_out_text = `<span style="color: ${expenseColor}">${expenseStr}</span> / <span style="color: ${incomeColor}">${incomeStr}</span>`;

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
            cell4.innerHTML = in_out_text;
            cell4.style.textAlign = "center";
            row.appendChild(cell4);

            var cell5 = document.createElement('div');
            cell5.className = 'table-cell';
            cell5.innerHTML = '<span style="color: ' + socColor + '">' + socStr + '</span>';
            cell5.style.textAlign = "center";
            row.appendChild(cell5);

            tableBody.appendChild(row);
        });
    }
}

// Legacy compatibility function
function showSchedule(data_request, data_response, data_controls) {
    if (scheduleManager) {
        scheduleManager.showSchedule(data_request, data_response, data_controls);
    }
}
