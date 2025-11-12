/**
 * Battery Manager for EOS Connect
 * Handles battery status, charging data and display
 * Extracted from legacy index.html
 */

class BatteryManager {
    constructor() {
        console.log('[BatteryManager] Initialized');
    }

    /**
     * Initialize battery manager
     */
    init() {
        console.log('[BatteryManager] Manager initialized');
    }

    /**
     * Converts 15-min interval data to hourly averages based on the base timestamp.
     * @param {Array<number>} dataArray - Array of 15-min interval values.
     * @param {string|Date} baseTimestamp - Timestamp of the first value (ISO string or Date).
     * @returns {Array<number>} Array of hourly averages.
     */
    convertQuarterlyToHourly(dataArray, baseTimestamp) {
        var baseTime = new Date(baseTimestamp);
        var baseMinute = baseTime.getMinutes();
        // How many quarters left in the current hour (including the current one)
        var quartersLeft = Math.ceil((60 - baseMinute) / 15);

        var hourlyData = [];
        var i = 0;
        // First hour: average over the remaining quarters in the current hour
        if (quartersLeft > 0 && dataArray.length >= quartersLeft) {
            var avg = dataArray.slice(0, quartersLeft).reduce((a, b) => a + b, 0) / quartersLeft;
            hourlyData.push(avg);
            i = quartersLeft;
        }
        // Process remaining full hours
        for (; i < dataArray.length; i += 4) {
            var chunk = dataArray.slice(i, i + 4);
            if (chunk.length > 0) {
                var avg = chunk.reduce((a, b) => a + b, 0) / chunk.length;
                hourlyData.push(avg);
            }
        }
        return hourlyData;
    }

    /**
     * Set battery charging data and update UI elements
     */
    setBatteryChargingData(data_response, data_controls) {
        // planned charging
        var currentHour = new Date(data_response["timestamp"]).getHours(); // ✅ Use server time
        const timestamp = new Date(data_response["timestamp"]);

        let price_data = data_response["result"]["Electricity_price"];
        let ac_charge = data_response["ac_charge"];

        const time_frame_base = data_controls["used_time_frame_base"];

        // var next_charge_time = ac_charge.slice(currentHour).findIndex((value) => value > 0);
        // if (next_charge_time !== -1) {
        //     next_charge_time += currentHour;
        //     var next_charge_time_hour = next_charge_time % 24;
        //     document.getElementById('next_charge_time').innerText = next_charge_time_hour + ":00";
        // } else {
        //     document.getElementById('next_charge_time').innerText = "--:--";
        // }

        // Determine next charge time slot based on time frame
        let nextChargeIndex = -1;
        let nextChargeHour = "--";
        let nextChargeMin = "--";

        if (time_frame_base === 900) {
            const currentSlot = timestamp.getHours() * 4 + Math.floor(timestamp.getMinutes() / 15);
            nextChargeIndex = ac_charge.slice(currentSlot).findIndex(value => value > 0);
            if (nextChargeIndex !== -1) {
                const slot = currentSlot + nextChargeIndex;
                nextChargeHour = Math.floor(slot / 4) % 24;
                nextChargeMin = (slot % 4) * 15;
            }
        } else {
            nextChargeIndex = ac_charge.slice(currentHour).findIndex(value => value > 0);
            if (nextChargeIndex !== -1) {
                nextChargeHour = (currentHour + nextChargeIndex) % 24;
                nextChargeMin = "00";
            }
        }

        // Update UI with next charge time
        document.getElementById('next_charge_time').innerText =
            nextChargeIndex !== -1
                ? nextChargeHour.toString().padStart(2, '0') + ":" + nextChargeMin.toString().padStart(2, '0')
                : "--:--";

        // calculate the average price for the next charging hours based on ac_charge
        // and calculate the next charge amount
        var next_charge_amount = 0;
        let total_price = 0;
        let total_price_count = 0;
        var foundFirst = false;
        if (time_frame_base === 900) {
            const current_quarterly_slot = timestamp.getHours() * 4 + Math.floor(timestamp.getMinutes() / 15);
            for (let index = 0; index < ac_charge.slice(current_quarterly_slot).length; index++) {
                const value = ac_charge[current_quarterly_slot + index];
                if (value > 0) {
                    if (!foundFirst) {
                        foundFirst = true;
                    }
                    let current_slot_amount = value * max_charge_power_w;
                    let current_hour_price = price_data[index] * current_slot_amount; // Convert to minor unit per kWh
                    total_price += current_hour_price;
                    total_price_count += 1;
                    next_charge_amount += value * max_charge_power_w;
                } else if (foundFirst) {
                    break;
                }
            }
        } else {
            for (let index = 0; index < ac_charge.slice(currentHour).length; index++) {
                const value = ac_charge[currentHour + index];

                if (value > 0) {
                    if (!foundFirst) {
                        foundFirst = true;
                    }
                    let current_hour_amount = value * max_charge_power_w;
                    let current_hour_price = price_data[index] * current_hour_amount; // Convert to minor unit per kWh
                    total_price += current_hour_price;
                    total_price_count += 1;
                    next_charge_amount += value * max_charge_power_w;
                } else if (foundFirst) {
                    break; // Stop the loop once a 0 is encountered after the first non-zero value
                }
            }
        }

        let next_charge_avg_price = total_price / next_charge_amount * 100000;

        if (next_charge_amount === 0) {
            document.getElementById('next_charge_time').innerText = "not planned";
            const nextChargeSummary = document.getElementById('next_charge_summary');
            const nextChargeSummary2 = document.getElementById('next_charge_summary_2');
            if (nextChargeSummary) nextChargeSummary.style.display = "none";
            if (nextChargeSummary2) nextChargeSummary2.style.display = "none";
        } else {
            document.getElementById('next_charge_amount').innerText = (next_charge_amount / 1000).toFixed(1) + " kWh";

            // Set total price
            const sumPriceElement = document.getElementById('next_charge_sum_price');
            if (sumPriceElement) {
                sumPriceElement.innerText = total_price.toFixed(2) + " " + localization.currency_symbol;
            }

            // Set average price if element exists
            const avgPriceElement = document.getElementById('next_charge_avg_price');
            if (avgPriceElement && !isNaN(next_charge_avg_price) && isFinite(next_charge_avg_price)) {
                avgPriceElement.innerText = next_charge_avg_price.toFixed(1) + " " + localization.currency_minor_unit + "/kWh";
            }

            // Display charge summary elements
            const nextChargeHeader = document.getElementById('next_charge_header');
            const nextChargeSummary = document.getElementById('next_charge_summary');
            const nextChargeSummary2 = document.getElementById('next_charge_summary_2');
            if (nextChargeHeader) nextChargeHeader.style.display = "";
            if (nextChargeSummary) nextChargeSummary.style.display = "";
            if (nextChargeSummary2) nextChargeSummary2.style.display = "";
        }
    }
}

// Legacy compatibility function
function setBatteryChargingData(data_response, data_controls) {
    if (batteryManager) {
        batteryManager.setBatteryChargingData(data_response, data_controls);
    }
}
