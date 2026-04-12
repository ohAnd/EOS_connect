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

        // Use gesamtlast from request as pure household load.
        // EOS server includes AC charging energy in Last_Wh_pro_Stunde; using gesamtlast
        // gives consistent display across both EOS and EVopt backends.
        let gesamtlastSliced;
        if (time_frame_base === 900) {
            gesamtlastSliced = data_request["ems"]["gesamtlast"]
                .slice(currentSlot)
                .concat(data_request["ems"]["gesamtlast"].slice(0, currentSlot))
                .slice(0, data_response["result"]["Last_Wh_pro_Stunde"].length);
        } else {
            gesamtlastSliced = data_request["ems"]["gesamtlast"]
                .slice(currentHour)
                .concat(data_request["ems"]["gesamtlast"].slice(24, 48))
                .slice(0, data_response["result"]["Last_Wh_pro_Stunde"].length);
        }
        // Calculate consumption (excluding home appliances)
        this.chartInstance.data.datasets[0].data = gesamtlastSliced.map((value, index) => {
            const actHomeApplianceValue = data_response["result"]["Home_appliance_wh_per_hour"][index] || 0;
            return ((value - actHomeApplianceValue) / 1000).toFixed(3);
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
        // Color PV forecast bars: gold when dc_charge=1 (PV charges battery), standard orange otherwise
        // Only active when pv_battery_charge_control_enabled is set in config
        const pvChargeCtrlEnabled = data_controls["current_states"] &&
            data_controls["current_states"]["pv_battery_charge_control_enabled"];
        if (data_response["dc_charge"] && pvChargeCtrlEnabled) {
            let dcChargeSlots;
            if (time_frame_base === 900) {
                dcChargeSlots = data_response["dc_charge"].slice(currentSlot).concat(data_response["dc_charge"].slice(96, 192));
            } else {
                dcChargeSlots = data_response["dc_charge"].slice(currentHour).concat(data_response["dc_charge"].slice(24, 48));
            }
            this.chartInstance.data.datasets[2].backgroundColor = pvData.map((_, i) =>
                dcChargeSlots[i] ? '#df6c00' : '#FFA500'
            );
            this.chartInstance.data.datasets[2].borderColor = pvData.map((_, i) =>
                dcChargeSlots[i] ? '#ff7b00' : '#FF991C'
            );
            // Store slots for dataset[12] tooltip carrier
            this.chartInstance.data.datasets[12].data = dcChargeSlots;
        } else {
            // Reset to uniform orange when no dc_charge data
            this.chartInstance.data.datasets[2].backgroundColor = '#FFA500';
            this.chartInstance.data.datasets[2].borderColor = '#FF991C';
            this.chartInstance.data.datasets[12].data = [];
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

            // EVopt: subtract planned AC charge from grid.
            // EOS: Last_Wh_pro_Stunde already contains optimizer-added load, which can differ
            // from planned AC charge, so subtract the embedded optimizer load component instead.
            const responseLoadWh = data_response["result"]["Last_Wh_pro_Stunde"][index] || 0;
            const householdLoadWh = gesamtlastSliced[index] || 0;
            const optimizerAddedLoadWh = evopt_in_charge
                ? originalAcChargeValue
                : Math.max(0, responseLoadWh - householdLoadWh);

            let gridValue = (value - optimizerAddedLoadWh) / 1000;
            let adjustedAcChargeValue = originalAcChargeValue / 1000;

            // Validation for invalid numbers
            if (isNaN(gridValue) || !isFinite(gridValue)) {
                console.warn(`Invalid grid calculation at index ${index}: Netzbezug=${value}, optimizer_added_load=${optimizerAddedLoadWh}, using 0 for grid`);
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

        // dataset[12] is populated alongside PV bar recolouring above — nothing more needed here

        // Dynamic Override (PV > Load) dataset - show when feature is active
        let dynOverrideData = null;
        if (data_controls["current_states"]["dyn_override_discharge_allowed_enabled"]) {
            // Get the override array from API response
            const overrideArray = data_controls["current_states"]["dyn_override_discharge_allowed_array"] || [];
            const arrayLength = time_frame_base === 900 ? (48 * 4) : 48;
            
            if (overrideArray && overrideArray.length > 0) {
                // Convert boolean array to numeric (1 for true, 0 for false)
                dynOverrideData = overrideArray.map(val => val ? 1 : 0);
                // Ensure the array has the correct length
                while (dynOverrideData.length < arrayLength) {
                    dynOverrideData.push(0);
                }
                dynOverrideData = dynOverrideData.slice(0, arrayLength);
            } else {
                // If no override array available, initialize with zeros
                dynOverrideData = new Array(arrayLength).fill(0);
            }
        } else {
            // If feature is disabled, show empty array
            dynOverrideData = [];
        }

        if (time_frame_base === 900) {
            if (dynOverrideData.length > 0) {
                this.chartInstance.data.datasets[9].data = dynOverrideData.slice(currentSlot).concat(dynOverrideData.slice(96, 192));
            }
        } else {
            if (dynOverrideData.length > 0) {
                this.chartInstance.data.datasets[9].data = dynOverrideData.slice(currentHour).concat(dynOverrideData.slice(24, 48));
            }
        }

        // Electricity Price - with segment styling for forecast data
        const priceRawData = data_response["result"]["Electricity_price"];
        const priceData = priceRawData.map(value => value * 1000);
        
        // Apply segment styling if forecast data is available
        if (priceInfo && priceInfo.forecast_start_index !== null && priceInfo.forecast_type !== "all_real") {
            // Calculate current slot offset based on time frame
            // forecast_start_index is absolute from midnight, so we need to subtract current position
            const timeFrameBase = data_controls && data_controls["used_time_frame_base"] ? data_controls["used_time_frame_base"] : 3600;
            
            // Convert absolute forecast_start_index (from midnight) to relative index in priceData
            // priceData starts from midnight, so subtract the current slot offset
            let arrayOffset = 0;
            if (timeFrameBase === 900) {
                // 15-min intervals: use currentSlot which includes both hours AND minutes
                // currentSlot already calculated at top: currentHour * 4 + Math.floor(currentMinutes / 15)
                arrayOffset = currentSlot;
            } else {
                // Hourly: use hour directly
                arrayOffset = currentHour;
            }
            
            const forecastIdx = Math.max(0, priceInfo.forecast_start_index - arrayOffset);
            
            // Split price data into real and forecast portions
            const realPriceData = priceData.slice(0, forecastIdx);
            const forecastPriceData = priceData.slice(forecastIdx);
            
            // Fill real portion with data, forecast portion with nulls (hidden)
            const dataset10Data = [];
            for (let i = 0; i < priceData.length; i++) {
                dataset10Data.push(i < forecastIdx ? priceData[i] : null);
            }
            
            // Fill new dataset 11 with forecast data, real portion with nulls (hidden)
            const dataset11Data = [];
            for (let i = 0; i < priceData.length; i++) {
                dataset11Data.push(i >= forecastIdx ? priceData[i] : null);
            }
            
            // Set dataset 10 (real prices) - solid orange
            this.chartInstance.data.datasets[10].data = dataset10Data;
            this.chartInstance.data.datasets[10].label = `Electricity Price (${localization.currency_symbol}/kWh)`;
            this.chartInstance.data.datasets[10].borderColor = 'rgba(255, 69, 0, 0.8)';
            this.chartInstance.data.datasets[10].borderDash = [];
            
            // Set dataset 11 (forecast prices) - gray
            if (!this.chartInstance.data.datasets[11]) {
                console.warn('[ChartManager] Dataset 11 does not exist for forecast visualization');
            } else {
                this.chartInstance.data.datasets[11].data = dataset11Data;
                this.chartInstance.data.datasets[11].label = `Electricity Price Forecast - ${priceInfo.forecast_type.replace(/_/g, ' ')} (${localization.currency_symbol}/kWh)`;
                this.chartInstance.data.datasets[11].borderColor = 'rgba(167, 167, 167, 0.7)';
                // this.chartInstance.data.datasets[11].borderDash = [5, 5];  // Dotted pattern
                this.chartInstance.data.datasets[11].borderWidth = 2;  // Thicker to see dashing
                this.chartInstance.data.datasets[11].type = 'line';
                this.chartInstance.data.datasets[11].yAxisID = 'y1';
                this.chartInstance.data.datasets[11].stepped = true;
                this.chartInstance.data.datasets[11].pointRadius = 0;
                this.chartInstance.data.datasets[11].pointHoverRadius = 4;
                this.chartInstance.data.datasets[11].fill = false;
                this.chartInstance.data.datasets[11].hidden = false;
            }
            
            this.chartInstance.options.scales.y1.title.text = `Price (${localization.currency_symbol}/kWh)`;
        } else {
            // No forecasting - all real prices
            this.chartInstance.data.datasets[10].data = priceData;
            this.chartInstance.data.datasets[10].label = `Electricity Price (${localization.currency_symbol}/kWh)`;
            this.chartInstance.data.datasets[10].borderColor = 'rgba(255, 69, 0, 0.8)';
            this.chartInstance.data.datasets[10].borderDash = [];
            
            // Hide dataset 11 if it exists
            if (this.chartInstance.data.datasets[11]) {
                this.chartInstance.data.datasets[11].data = [];
                this.chartInstance.data.datasets[11].hidden = true;
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
                    { label: 'Dynamic Discharge Allowed (PV > Load)', data: [], type: 'line', borderColor: 'rgba(50, 205, 50, 0.6)', backgroundColor: 'rgba(50, 205, 50, 0.1)', borderWidth: 1, fill: true, yAxisID: 'y3', pointRadius: 1, pointHoverRadius: 4, stepped: true, hidden: false },
                    { label: `Electricity Price (${localization.currency_symbol}/kWh)`, data: [], type: 'line', borderColor: 'rgba(255, 69, 0, 0.8)', backgroundColor: 'rgba(255, 165, 0, 0.2)', borderWidth: 1, yAxisID: 'y1', stepped: true, pointRadius: 1, pointHoverRadius: 4 },
                    { label: 'Electricity Price - Forecast', data: [], type: 'line', borderColor: 'rgba(167, 167, 167, 0.7)', backgroundColor: 'rgba(220, 20, 60, 0.05)', borderWidth: 2, yAxisID: 'y1', stepped: true, pointRadius: 1, pointHoverRadius: 4, fill: false, hidden: true },
                    { label: 'PV Charge Planned', data: [], type: 'line', borderColor: 'transparent', backgroundColor: 'transparent', borderWidth: 0, fill: false, yAxisID: 'y3', pointRadius: 0, pointHoverRadius: 0, stepped: true, hidden: true }
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
                    legend: { display: !isMobile(), labels: { color: 'lightgray', filter: item => {
                        if (item.text === 'PV Charge Planned') return false;
                        if (item.text === 'Dynamic Discharge Allowed (PV > Load)' &&
                            !(data_controls?.current_states?.dyn_override_discharge_allowed_enabled)) return false;
                        return true;
                    } } },
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
                                else if (label === 'PV forecast') {
                                    // Amber bar = PV charging battery (dc_charge=1)
                                    const isBatteryCharge = context.dataset.backgroundColor instanceof Array
                                        ? context.dataset.backgroundColor[context.dataIndex] !== '#FFA500'
                                        : false;
                                    return isBatteryCharge
                                        ? `${label}: ${value} kWh \u26a1 charges battery`
                                        : `${label}: ${value} kWh`;
                                }
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
                                else if (label === 'PV Charge Planned')
                                    return null; // hidden carrier, suppress tooltip entry
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
