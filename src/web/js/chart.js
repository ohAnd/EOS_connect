/**
 * Chart Manager for EOS Connect
 * Handles chart creation, updates and display functionality
 * Extracted from legacy index.html
 */

class ChartManager {
    constructor() {
        this.chartInstance = null;
        console.log('[ChartManager] Initialized');
    }

    /**
     * Initialize chart manager
     */
    init() {
        console.log('[ChartManager] Manager initialized');
    }

    /**
     * Update existing chart with new data
     */
    updateChart(data_request, data_response, data_controls, priceInfo = null) {
        if (!this.chartInstance) {
            console.warn('[ChartManager] No chart instance to update');
            return;
        }

        // Use server timestamp consistently for data processing
        const serverTime = new Date(data_response["timestamp"]);
        const currentMinutes = serverTime.getMinutes();
        const currentSlot = serverTime.getHours() * 4 + Math.floor(currentMinutes / 15);
        const currentHour = serverTime.getHours();

        const time_frame_base = data_controls["used_time_frame_base"];

        const evopt_in_charge = data_controls["used_optimization_source"] === "evopt";

        // Create labels in user's local timezone - showing only hours with :00
        this.chartInstance.data.labels = Array.from(
            { length: data_response["result"]["Last_Wh_pro_Stunde"].length },
            (_, i) => {
                let labelTime;
                if (time_frame_base === 900) {
                    // Round down to previous quarter-hour
                    const base = new Date(serverTime);
                    base.setMinutes(Math.floor(base.getMinutes() / 15) * 15, 0, 0);
                    labelTime = new Date(base.getTime() + i * 15 * 60 * 1000);
                } else {
                    // For hourly intervals, add i*1h to the full hour (HH:00) from serverTime
                    let baseHour = new Date(serverTime);
                    baseHour.setMinutes(0, 0, 0);
                    labelTime = new Date(baseHour.getTime() + i * 60 * 60 * 1000);
                }
                if (evopt_in_charge && i === 0) {
                    // Show current time as HH:MM for the first entry
                    const hour = serverTime.getHours();
                    const minute = serverTime.getMinutes();
                    return `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`;
                } else {
                    const hour = labelTime.getHours();
                    const minute = labelTime.getMinutes();
                    return `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`;
                }
            }
        );

        // Calculate consumption (excluding home appliances)
        this.chartInstance.data.datasets[0].data = data_response["result"]["Last_Wh_pro_Stunde"].map((value, index) => {
            const actHomeApplianceValue = data_response["result"]["Home_appliance_wh_per_hour"].map(value => value)
            return ((value - actHomeApplianceValue[index]) / 1000).toFixed(3);
        });

        // Home appliances
        this.chartInstance.data.datasets[1].data = data_response["result"]["Home_appliance_wh_per_hour"].map(value => (value / 1000).toFixed(3));

        // PV forecast
        let pvData;
        if (time_frame_base === 900) {
            // 15-minute intervals, 192 slots
            pvData = data_request["ems"]["pv_prognose_wh"]
                .slice(currentSlot)
                .concat(data_request["ems"]["pv_prognose_wh"].slice(0, currentSlot))
                .slice(0, 192)
                .map(value => (value / 1000).toFixed(3));
        } else {
            // Hourly intervals, 48 slots
            pvData = data_request["ems"]["pv_prognose_wh"]
                .slice(currentHour)
                .concat(data_request["ems"]["pv_prognose_wh"].slice(24, 48))
                .map(value => (value / 1000).toFixed(3));
        }
        this.chartInstance.data.datasets[2].data = pvData;

        // Prepare arrays for grid and AC charge with redistribution logic
        const gridData = [];
        const acChargeData = [];

        data_response["result"]["Netzbezug_Wh_pro_Stunde"].forEach((value, index) => {
            var originalAcChargeValue = data_response["ac_charge"].slice(currentHour).concat(data_response["ac_charge"].slice(24, 48))[index] * max_charge_power_w;
            if (time_frame_base === 900) {
                const current_quarterly_slot = serverTime.getHours() * 4 + Math.floor(serverTime.getMinutes() / 15);
                originalAcChargeValue = data_response["ac_charge"].slice(current_quarterly_slot).concat(data_response["ac_charge"].slice(24, 48))[index] * max_charge_power_w;
            }


            let gridValue = (value - originalAcChargeValue) / 1000;
            let adjustedAcChargeValue = originalAcChargeValue / 1000;

            // Validation for invalid numbers
            if (isNaN(gridValue) || !isFinite(gridValue)) {
                console.warn(`Invalid grid calculation at index ${index}: Netzbezug=${value}, AC_charge=${originalAcChargeValue}, using 0 for grid`);
                gridValue = 0;
                adjustedAcChargeValue = (value / 1000); // Treat all as AC charge
            }
            // If calculated grid value would be negative, show actual grid data and planned AC charge
            else if (gridValue < 0) {
                console.info(`Negative calculated grid at index ${index}: ${gridValue.toFixed(3)}kW, showing actual Netzbezug=${(value / 1000).toFixed(3)}kW and planned AC charge=${(originalAcChargeValue / 1000).toFixed(3)}kW`);
                // Show actual grid consumption/feed-in from Netzbezug_Wh_pro_Stunde
                gridValue = value / 1000;
                // Show planned AC charge
                adjustedAcChargeValue = originalAcChargeValue / 1000;
            }

            gridData.push(gridValue.toFixed(3));
            acChargeData.push(adjustedAcChargeValue.toFixed(3));
        });

        // Set the calculated data
        this.chartInstance.data.datasets[3].data = gridData; // Grid consumption
        this.chartInstance.data.datasets[4].data = acChargeData; // AC charging (adjusted)

        // Rest of the datasets remain unchanged
        this.chartInstance.data.datasets[5].data = data_response["result"]["akku_soc_pro_stunde"];
        this.chartInstance.data.datasets[6].data = data_response["result"]["Kosten_Euro_pro_Stunde"];
        this.chartInstance.data.datasets[7].data = data_response["result"]["Einnahmen_Euro_pro_Stunde"];
        
        if (time_frame_base === 900) {
            this.chartInstance.data.datasets[8].data = data_response["discharge_allowed"].slice(currentSlot).concat(data_response["discharge_allowed"].slice(96, 192));
        } else {
            this.chartInstance.data.datasets[8].data = data_response["discharge_allowed"].slice(currentHour).concat(data_response["discharge_allowed"].slice(24, 48));
        }

        // Electricity Price - with segment styling for forecast data
        const priceRawData = data_response["result"]["Electricity_price"];
        const priceData = priceRawData.map(value => value * 1000);
        
        // Apply segment styling if forecast data is available
        if (priceInfo && priceInfo.forecast_start_index !== null && priceInfo.forecast_type !== "all_real") {
            // Calculate current hour offset based on time frame
            const now = new Date();
            const currentHour = now.getHours();
            const timeFrameBase = data_controls && data_controls["used_time_frame_base"] ? data_controls["used_time_frame_base"] : 3600;
            
            // Convert absolute forecast_start_index (from midnight) to relative index in priceData
            // priceData starts from current hour, so subtract the hour offset
            let arrayOffset = 0;
            if (timeFrameBase === 900) {
                // 15-min intervals: multiply hour by 4
                arrayOffset = currentHour * 4;
            } else {
                // Hourly: use hour directly
                arrayOffset = currentHour;
            }
            
            const forecastIdx = Math.max(0, priceInfo.forecast_start_index - arrayOffset);
            
            // Split price data into real and forecast portions
            const realPriceData = priceData.slice(0, forecastIdx);
            const forecastPriceData = priceData.slice(forecastIdx);
            
            // Fill real portion with data, forecast portion with nulls (hidden)
            const dataset9Data = [];
            for (let i = 0; i < priceData.length; i++) {
                dataset9Data.push(i < forecastIdx ? priceData[i] : null);
            }
            
            // Fill new dataset 10 with forecast data, real portion with nulls (hidden)
            const dataset10Data = [];
            for (let i = 0; i < priceData.length; i++) {
                dataset10Data.push(i >= forecastIdx ? priceData[i] : null);
            }
            
            // Set dataset 8 (real prices) - solid orange
            this.chartInstance.data.datasets[9].data = dataset9Data;
            this.chartInstance.data.datasets[9].label = `Electricity Price (${localization.currency_symbol}/kWh)`;
            this.chartInstance.data.datasets[9].borderColor = 'rgba(255, 69, 0, 0.8)';
            this.chartInstance.data.datasets[9].borderDash = [];
            
            // Set dataset 10 (forecast prices) - gray
            if (!this.chartInstance.data.datasets[10]) {
                console.warn('[ChartManager] Dataset 10 does not exist for forecast visualization');
            } else {
                this.chartInstance.data.datasets[10].data = dataset10Data;
                this.chartInstance.data.datasets[10].label = `Electricity Price Forecast - ${priceInfo.forecast_type.replace(/_/g, ' ')} (${localization.currency_symbol}/kWh)`;
                this.chartInstance.data.datasets[10].borderColor = 'rgba(167, 167, 167, 0.7)';
                // this.chartInstance.data.datasets[10].borderDash = [5, 5];  // Dotted pattern
                this.chartInstance.data.datasets[10].borderWidth = 2;  // Thicker to see dashing
                this.chartInstance.data.datasets[10].type = 'line';
                this.chartInstance.data.datasets[10].yAxisID = 'y1';
                this.chartInstance.data.datasets[10].stepped = true;
                this.chartInstance.data.datasets[10].pointRadius = 0;
                this.chartInstance.data.datasets[10].pointHoverRadius = 4;
                this.chartInstance.data.datasets[10].fill = false;
                this.chartInstance.data.datasets[10].hidden = false;
            }
            
            this.chartInstance.options.scales.y1.title.text = `Price (${localization.currency_symbol}/kWh)`;
        } else {
            // No forecasting - all real prices
            this.chartInstance.data.datasets[9].data = priceData;
            this.chartInstance.data.datasets[9].label = `Electricity Price (${localization.currency_symbol}/kWh)`;
            this.chartInstance.data.datasets[9].borderColor = 'rgba(255, 69, 0, 0.8)';
            this.chartInstance.data.datasets[9].borderDash = [];
            
            // Hide dataset 10 if it exists
            if (this.chartInstance.data.datasets[10]) {
                this.chartInstance.data.datasets[10].data = [];
                this.chartInstance.data.datasets[10].hidden = true;
            }
            
            this.chartInstance.options.scales.y1.title.text = `Price (${localization.currency_symbol}/kWh)`;
        }

        this.chartInstance.update('none'); // Update without animation
    }

    /**
     * Create new chart instance
     */
    createChart(data_request, data_response, data_controls, priceInfo = null) {
        const ctx = document.getElementById('energyChart').getContext('2d');
        this.chartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [
                    { label: 'Load', data: [], backgroundColor: 'rgba(75, 192, 192, 0.2)', borderColor: 'rgba(75, 192, 192, 1)', borderWidth: 1, stack: 'load' },
                    { label: 'Home Appliance', data: [], backgroundColor: 'rgba(172, 41, 0, 0.4)', borderColor: 'rgba(172, 41, 0, 1)', borderWidth: 1, stack: 'load' },
                    { label: 'PV forecast', data: [], backgroundColor: '#FFA500', borderColor: '#FF991C', borderWidth: 1, stack: 'combined' },
                    { label: 'Grid', data: [], backgroundColor: 'rgba(128, 128, 128, 0.6)', borderColor: 'rgba(211, 211, 211, 0.7)', borderWidth: 1, stack: 'combined' },
                    { label: 'AC Charge', data: [], backgroundColor: 'darkred', borderColor: 'rgba(255, 0, 0, 0.2)', borderWidth: 1, stack: 'combined' },
                    { label: 'Battery SOC', data: [], type: 'line', backgroundColor: 'blue', borderColor: 'lightblue', borderWidth: 1, yAxisID: 'y2', pointRadius: 1, pointHoverRadius: 4, fill: false, hidden: false },
                    { label: 'Expense', data: [], type: 'line', borderColor: 'lightgreen', backgroundColor: 'green', borderWidth: 1, yAxisID: 'y1', stepped: true, hidden: true, pointRadius: 1, pointHoverRadius: 4 },
                    { label: 'Income', data: [], type: 'line', borderColor: 'lightyellow', backgroundColor: 'yellow', borderWidth: 1, yAxisID: 'y1', stepped: true, hidden: true, pointRadius: 1, pointHoverRadius: 4 },
                    { label: 'Discharge Allowed', data: [], type: 'line', borderColor: 'rgba(144, 238, 144, 0.3)', backgroundColor: 'rgba(144, 238, 144, 0.05)', borderWidth: 1, fill: true, yAxisID: 'y3', pointRadius: 1, pointHoverRadius: 4, stepped: true },
                    { label: `Electricity Price (${localization.currency_symbol}/kWh)`, data: [], type: 'line', borderColor: 'rgba(255, 69, 0, 0.8)', backgroundColor: 'rgba(255, 165, 0, 0.2)', borderWidth: 1, yAxisID: 'y1', stepped: true, pointRadius: 1, pointHoverRadius: 4 },
                    { label: 'Electricity Price - Forecast', data: [], type: 'line', borderColor: 'rgba(167, 167, 167, 0.7)', backgroundColor: 'rgba(220, 20, 60, 0.05)', borderWidth: 2, yAxisID: 'y1', stepped: true, pointRadius: 1, pointHoverRadius: 4, fill: false, hidden: true }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: { beginAtZero: true, title: { display: true, text: 'Energy (kWh)', color: 'lightgray' }, grid: { color: 'rgb(54, 54, 54)' }, ticks: { color: 'lightgray' } },
                    y1: { beginAtZero: true, position: 'right', title: { display: true, text: `Price (${localization.currency_symbol}/kWh)`, color: 'lightgray' }, grid: { drawOnChartArea: false }, ticks: { color: 'lightgray', callback: value => value.toFixed(2) } },
                    y2: { beginAtZero: true, position: 'right', title: { display: true, text: 'Battery SOC (%)', color: 'darkgray' }, grid: { drawOnChartArea: false }, ticks: { color: 'darkgray', callback: value => value.toFixed(0) } },
                    y3: { beginAtZero: true, position: 'right', display: false, title: { display: true, text: 'AC Charge', color: 'darkgray' }, grid: { drawOnChartArea: false }, ticks: { color: 'darkgray', callback: value => value.toFixed(2) } },
                    x: { grid: { color: 'rgb(54, 54, 54)' }, ticks: { color: 'lightgray', font: { size: 10 } } }
                },
                plugins: {
                    legend: { display: !isMobile(), labels: { color: 'lightgray' } },
                    tooltip: {
                        mode: 'index',  // Show all datasets for the hovered x-axis value
                        intersect: false,  // Don't require intersection with data point
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        titleColor: '#fff',
                        bodyColor: '#fff',
                        borderColor: 'rgba(255, 255, 255, 0.3)',
                        borderWidth: 1,
                        padding: 12,
                        displayColors: true,
                        callbacks: {
                            label: function (context) {
                                // Default label
                                let label = context.dataset.label || '';
                                // Value with unit
                                let value = context.parsed.y;
                                // Only add unit for "Load" (or for all, if you want)
                                if (label === 'Load')
                                    return `${label}: ${value} kWh`;
                                else if (label === 'Home Appliance')
                                    return `${label}: ${value} kWh`;
                                else if (label === 'PV forecast')
                                    return `${label}: ${value} kWh`;
                                else if (label === 'Grid')
                                    return `${label}: ${value} kWh`;
                                else if (label === 'AC Charge')
                                    return `${label}: ${value} kWh`;
                                else if (label === 'Battery SOC')
                                    return `${label}: ${value.toFixed(2)} %`;
                                else if (label === 'Expense')
                                    return `${label}: ${value} ${localization.currency_symbol}`;
                                else if (label === 'Income')
                                    return `${label}: ${value} ${localization.currency_symbol}`;
                                else if (label.startsWith('Electricity Price'))
                                    return `${label}: ${value.toFixed(3)} ${localization.currency_symbol}/kWh`;
                                else if (label === 'Discharge Allowed')
                                    return `${label}: ${value}`;
                                return `${label}: ${value}`;
                            }
                        }
                    }
                },
            }
        });

        // Set global reference for legacy compatibility
        chartInstance = this.chartInstance;

        this.updateChart(data_request, data_response, data_controls, priceInfo); // Feed the content immediately after creation
    }

    /**
     * Update legend visibility based on screen size
     */
    updateLegendVisibility() {
        if (this.chartInstance) {
            this.chartInstance.options.plugins.legend.display = !isMobile();
            if (!this.chartInstance.options.scales.y.ticks.font)
                this.chartInstance.options.scales.y.ticks.font = {};
            this.chartInstance.options.scales.y.ticks.font.size = isMobile() ? 8 : 12;

            if (!this.chartInstance.options.scales.y1.ticks.font)
                this.chartInstance.options.scales.y1.ticks.font = {};
            this.chartInstance.options.scales.y1.ticks.font.size = isMobile() ? 8 : 12;

            if (!this.chartInstance.options.scales.y2.ticks.font)
                this.chartInstance.options.scales.y2.ticks.font = {};
            this.chartInstance.options.scales.y2.ticks.font.size = isMobile() ? 8 : 12;

            if (!this.chartInstance.options.scales.x.ticks.font)
                this.chartInstance.options.scales.x.ticks.font = {};
            this.chartInstance.options.scales.x.ticks.font.size = isMobile() ? 8 : 12;

            this.chartInstance.options.scales.y.title.display = !isMobile();
            this.chartInstance.options.scales.y1.title.display = !isMobile();
            this.chartInstance.options.scales.y2.title.display = !isMobile();

            this.chartInstance.update();
        }
    }
}

// Legacy compatibility functions
function createChart(data_request, data_response, data_controls, priceInfo = null) {
    if (chartManager) {
        chartManager.createChart(data_request, data_response, data_controls, priceInfo);
    }
}

function updateChart(data_request, data_response, data_controls, priceInfo = null) {
    if (chartManager) {
        chartManager.updateChart(data_request, data_response, data_controls, priceInfo);
    }
}

function updateLegendVisibility() {
    if (chartManager) {
        chartManager.updateLegendVisibility();
    }
}
