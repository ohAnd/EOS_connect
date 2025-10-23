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
    showStatistics(data_request, data_response) {
        // set the values for solar yield today and tomorrow
        let yield_today = data_request["ems"]["pv_prognose_wh"].slice(0, 24).reduce((acc, value) => acc + value, 0) / 1000;
        let yield_tomorrow = data_request["ems"]["pv_prognose_wh"].slice(24, 48).reduce((acc, value) => acc + value, 0) / 1000;
        document.getElementById('statistics_header_left').innerHTML = '<i class="fa-solid fa-solar-panel"></i> ' + yield_today.toFixed(1) + ' <span style="font-size: 0.6em;">kWh</span>';
        document.getElementById('statistics_header_left').title = "Solar yield for today";
        document.getElementById('statistics_header_right').innerHTML = + yield_tomorrow.toFixed(1) + ' <span style="font-size: 0.6em;">kWh</span>' + ' <i class="fa-solid fa-solar-panel"></i> ';
        document.getElementById('statistics_header_right').title = "Solar yield for tomorrow";

        // set expense and income for today and tomorrow
        let expense_data = data_response["result"]["Kosten_Euro_pro_Stunde"];
        let income_data = data_response["result"]["Einnahmen_Euro_pro_Stunde"];
        let feed_in_data = data_response["result"]["Netzeinspeisung_Wh_pro_Stunde"];

        let currentHour = new Date(data_response["timestamp"]).getHours(); // ✅ Use server time
        let expense_today = expense_data.slice(0, 24 - currentHour).reduce((acc, value) => acc + value, 0).toFixed(2);
        document.getElementById('expense_summary').innerText = expense_today + " " + PRICE_INFO.symbol;
        document.getElementById('expense_summary').title = "Expense for the rest of the day";

        // set income for rest of the day
        let income_today = income_data.slice(0, 24 - currentHour).reduce((acc, value) => acc + value, 0).toFixed(2);
        document.getElementById('income_summary').innerText = income_today + " " + PRICE_INFO.symbol;
        document.getElementById('income_summary').title = "Income for the rest of the day";

        // set feed in for rest of the day
        let feed_in_today = feed_in_data.slice(0, 24 - currentHour).reduce((acc, value) => acc + value, 0) / 1000;
        document.getElementById('feed_in_summary').innerText = feed_in_today.toFixed(1) + " kWh";
        document.getElementById('feed_in_summary').title = "Feed in for the rest of the day";
    }
}

// Legacy compatibility function
function showStatistics(data_request, data_response) {
    if (statisticsManager) {
        statisticsManager.showStatistics(data_request, data_response);
    }
}
