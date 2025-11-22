/**
 * Statistics Manager for EOS Connect
 * Handles statistics display and calculations
 * Extracted from legacy index.html
 */

class StatisticsManager {
    constructor() {
        console.log('[StatisticsManager] Initialized');
    }

    /**
     * Initialize statistics manager
     */
    init() {
        console.log('[StatisticsManager] Manager initialized');
    }

    /**
     * Show statistics including solar yield, expenses, income and feed-in data
     */
    showStatistics(data_request, data_response, data_controls) {
        const time_frame_base = data_controls["used_time_frame_base"];
        let yield_today, yield_tomorrow;
        let expense_today, income_today, feed_in_today;
        let expense_data = data_response["result"]["Kosten_Euro_pro_Stunde"];
        let income_data = data_response["result"]["Einnahmen_Euro_pro_Stunde"];
        let feed_in_data = data_response["result"]["Netzeinspeisung_Wh_pro_Stunde"];
        let currentHour = new Date(data_response["timestamp"]).getHours();

        if (time_frame_base === 3600) {
            // Hourly: first value is current hour, then next hours up to 23:00
            yield_today = data_request["ems"]["pv_prognose_wh"].slice(0, 24).reduce((acc, value) => acc + value, 0) / 1000;
            yield_tomorrow = data_request["ems"]["pv_prognose_wh"].slice(24, 48).reduce((acc, value) => acc + value, 0) / 1000;

            // expense_data[0] = current hour, expense_data[1] = next hour, etc.
            expense_today = expense_data.slice(0, 24 - currentHour).reduce((acc, value) => acc + value, 0).toFixed(2);
            income_today = income_data.slice(0, 24 - currentHour).reduce((acc, value) => acc + value, 0).toFixed(2);
            feed_in_today = feed_in_data.slice(0, 24 - currentHour).reduce((acc, value) => acc + value, 0) / 1000;
        } else if (time_frame_base === 900) {
            // 15-min: first value is current quarter, then next quarters up to 23:45
            yield_today = data_request["ems"]["pv_prognose_wh"].slice(0, 96).reduce((acc, value) => acc + value, 0) / 1000;
            yield_tomorrow = data_request["ems"]["pv_prognose_wh"].slice(96, 192).reduce((acc, value) => acc + value, 0) / 1000;

            // Calculate current quarter index (0 = :00, 1 = :15, 2 = :30, 3 = :45)
            let now = new Date(data_response["timestamp"]);
            let currentHour = now.getHours();
            let currentMinute = now.getMinutes();
            let currentQuarter = Math.floor(currentMinute / 15);
            let currentSlot = currentHour * 4 + currentQuarter;

            // expense_data[0] = current quarter, expense_data[1] = next quarter, etc.
            expense_today = expense_data.slice(0, 96 - currentSlot).reduce((acc, value) => acc + value, 0).toFixed(2);
            income_today = income_data.slice(0, 96 - currentSlot).reduce((acc, value) => acc + value, 0).toFixed(2);
            feed_in_today = feed_in_data.slice(0, 96 - currentSlot).reduce((acc, value) => acc + value, 0) / 1000;

        } else {
            // Fallback: use all as today
            yield_today = data_request["ems"]["pv_prognose_wh"].reduce((acc, value) => acc + value, 0) / 1000;
            yield_tomorrow = 0;
            expense_today = expense_data.reduce((acc, value) => acc + value, 0).toFixed(2);
            income_today = income_data.reduce((acc, value) => acc + value, 0).toFixed(2);
            feed_in_today = feed_in_data.reduce((acc, value) => acc + value, 0) / 1000;
        }

        document.getElementById('statistics_header_left').innerHTML = '<i class="fa-solid fa-solar-panel"></i> ' + yield_today.toFixed(1) + ' <span style="font-size: 0.6em;">kWh</span>';
        document.getElementById('statistics_header_left').title = "Solar yield for today";
        document.getElementById('statistics_header_right').innerHTML = yield_tomorrow.toFixed(1) + ' <span style="font-size: 0.6em;">kWh</span>' + ' <i class="fa-solid fa-solar-panel"></i> ';
        document.getElementById('statistics_header_right').title = "Solar yield for tomorrow";

        document.getElementById('expense_summary').innerText = expense_today + " " + localization.currency_symbol;
        document.getElementById('expense_summary').title = "Expense for the rest of the day";

        document.getElementById('income_summary').innerText = income_today + " " + localization.currency_symbol;
        document.getElementById('income_summary').title = "Income for the rest of the day";

        document.getElementById('feed_in_summary').innerText = feed_in_today.toFixed(1) + " kWh";
        document.getElementById('feed_in_summary').title = "Feed in for the rest of the day";
    }
}

// Legacy compatibility function
function showStatistics(data_request, data_response, data_controls) {
    if (statisticsManager) {
        statisticsManager.showStatistics(data_request, data_response, data_controls);
    }
}
