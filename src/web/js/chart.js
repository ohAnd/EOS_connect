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
    updateChart(data_request, data_response, data_controls) {
        if (!this.chartInstance) {
            console.warn('[ChartManager] No chart instance to update');
            return;
        }

        // Use server timestamp consistently for data processing
        const serverTime = new Date(data_response["timestamp"]);
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
                    // For hourly intervals, add i*1h to serverTime
                    labelTime = new Date(serverTime.getTime() + i * 60 * 60 * 1000);
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

        // this.chartInstance.data.labels = Array.from(
        //     { length: data_response["result"]["Last_Wh_pro_Stunde"].length },
        //     (_, i) => {
        //         var labelTime = null;
        //         if (time_frame_base === 900) {
        //             // For 15-minute intervals, show every 15 minutes
        //             // For 15-minute intervals, show only :15, :30, :45, :00 starting from current minute
        //             const startMinute = serverTime.getMinutes();
        //             const minuteSteps = [15, 30, 45, 0];
        //             const stepIndex = i % 4;
        //             let minute = minuteSteps[stepIndex];
        //             let hour = serverTime.getHours() + Math.floor((startMinute + i * 15) / 60);
        //             if (minute === 0) {
        //                 hour += 1; // Move to next hour for :00
        //             }
        //             labelTime = new Date(serverTime.getFullYear(), serverTime.getMonth(), serverTime.getDate(), hour, minute, 0);
        //         } else {
        //             // For hourly intervals, show every hour
        //             labelTime = new Date(serverTime.getTime() + (i * 60 * 60 * 1000));
        //         }
        //         if (evopt_in_charge && i === 0) {
        //             // Show current time as HH:MM for the first entry
        //             const hour = labelTime.getHours();
        //             const minute = labelTime.getMinutes();
        //             return `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`;
        //         } else {
        //             if (time_frame_base === 900) {
        //                 // Show HH:15, HH:30, HH:45 for 15-minute intervals
        //                 const hour = labelTime.getHours();
        //                 const minute = labelTime.getMinutes();
        //                 return `${hour.toString().padStart(2, '0')}:${minute.toString().padStart(2, '0')}`;
        //             } else {
        //                 // Show HH:00 for all other entries
        //                 const hour = labelTime.getHours();
        //                 return `${hour.toString().padStart(2, '0')}:00`;
        //             }
        //         }
        //     }
        // );

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
            const currentMinutes = serverTime.getMinutes();
            const currentSlot = serverTime.getHours() * 4 + Math.floor(currentMinutes / 15);
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
            const originalAcChargeValue = data_response["ac_charge"].slice(currentHour).concat(data_response["ac_charge"].slice(24, 48))[index] * max_charge_power_w;
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
        this.chartInstance.data.datasets[8].data = data_response["result"]["Electricity_price"].map(value => value * 1000);
        this.chartInstance.data.datasets[8].label = `Electricity Price (${localization.currency_symbol}/kWh)`;
        this.chartInstance.options.scales.y1.title.text = `Price (${localization.currency_symbol}/kWh)`;
        this.chartInstance.data.datasets[9].data = data_response["discharge_allowed"].slice(currentHour).concat(data_response["discharge_allowed"].slice(24, 48));

        this.chartInstance.update('none'); // Update without animation
    }

    /**
     * Create new chart instance
     */
    createChart(data_request, data_response, data_controls) {
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
                    { label: `Electricity Price (${localization.currency_symbol}/kWh)`, data: [], type: 'line', borderColor: 'rgba(255, 69, 0, 0.8)', backgroundColor: 'rgba(255, 165, 0, 0.2)', borderWidth: 1, yAxisID: 'y1', stepped: true, pointRadius: 1, pointHoverRadius: 4 },
                    { label: 'Discharge Allowed', data: [], type: 'line', borderColor: 'rgba(144, 238, 144, 0.3)', backgroundColor: 'rgba(144, 238, 144, 0.05)', borderWidth: 1, fill: true, yAxisID: 'y3', pointRadius: 1, pointHoverRadius: 4, stepped: true }
                ]
            },
            options: {
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

        this.updateChart(data_request, data_response, data_controls); // Feed the content immediately after creation
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
function createChart(data_request, data_response, data_controls) {
    if (chartManager) {
        chartManager.createChart(data_request, data_response, data_controls);
    }
}

function updateChart(data_request, data_response, data_controls) {
    if (chartManager) {
        chartManager.updateChart(data_request, data_response, data_controls);
    }
}

function updateLegendVisibility() {
    if (chartManager) {
        chartManager.updateLegendVisibility();
    }
}
