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
     * Set battery charging data and update UI elements
     */
    setBatteryChargingData(data_response) {
        // planned charging
        var currentHour = new Date(data_response["timestamp"]).getHours(); // ✅ Use server time
        let price_data = data_response["result"]["Electricity_price"];
        let ac_charge = data_response["ac_charge"];

        var next_charge_time = ac_charge.slice(currentHour).findIndex((value) => value > 0);
        if (next_charge_time !== -1) {
            next_charge_time += currentHour;
            var next_charge_time_hour = next_charge_time % 24;
            document.getElementById('next_charge_time').innerText = next_charge_time_hour + ":00";
        } else {
            document.getElementById('next_charge_time').innerText = "--:--";
        }

        // calculate the average price for the next charging hours based on ac_charge
        // and calculate the next charge amount
        var next_charge_amount = 0;
        let total_price = 0;
        let total_price_count = 0;
        var foundFirst = false;
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
                sumPriceElement.innerText = total_price.toFixed(2) + " " + PRICE_INFO.symbol;
            }
            
            // Set average price if element exists
            const avgPriceElement = document.getElementById('next_charge_avg_price');
            if (avgPriceElement && !isNaN(next_charge_avg_price) && isFinite(next_charge_avg_price)) {
                avgPriceElement.innerText = next_charge_avg_price.toFixed(1) + " " + PRICE_INFO.minorUnit;
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
function setBatteryChargingData(data_response) {
    if (batteryManager) {
        batteryManager.setBatteryChargingData(data_response);
    }
}
