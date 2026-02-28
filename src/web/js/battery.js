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
     * Show battery overview in a full-screen overlay
     */
    showBatteryOverview() {
        if (typeof data_controls === 'undefined' || !data_controls) {
            console.error('[BatteryManager] No data available for battery overview');
            return;
        }

        const battery = data_controls.battery || {};
        const stored = battery.stored_energy || {};
        const sessions = stored.charging_sessions || [];
        const lookbackHours = stored.duration_of_analysis || 96;
        const isEnabled = stored.enabled !== false;

        const header = `
            <div style="display: flex; align-items: center; gap: 10px;">
                <i class="fa-solid fa-battery-full" style="color: #cccccc;"></i>
                <span>Battery Overview</span>
            </div>
        `;

        // Format values
        const soc = battery.soc || 0;
        const usableKWh = (battery.usable_capacity / 1000).toFixed(1);
        const maxChargeKW = (battery.max_charge_power_dyn / 1000).toFixed(2);
        const wac = stored.stored_energy_price !== undefined ? (stored.stored_energy_price * 10000).toFixed(2) : "--";
        const pvRatio = isEnabled && stored.ratio !== undefined ? stored.ratio.toFixed(1) : "--";
        const lastUpdate = stored.last_update ? new Date(stored.last_update).toLocaleString() : "Never";

        let priceSubLabel = "Inventory Valuation";
        if (!isEnabled) {
            priceSubLabel = stored.price_source === "sensor" ? "External Sensor" : "Fixed Value";
        }

        // Max charge power details
        const chargingCurveEnabled = battery.charging_curve_enabled !== false;
        const maxChargePowerFix = battery.max_charge_power_fix || battery.max_charge_power_w || 0;
        const maxChargeFixKW = (maxChargePowerFix / 1000).toFixed(1);
        const batteryTemp = battery.temperature;
                
        // Temperature status and icon
        let tempIcon = '';
        let tempColor = '#888';
        let tempStatus = '';
        if (batteryTemp !== undefined && batteryTemp !== null) {
            if (batteryTemp < 0) {
                tempIcon = '<i class="fa-solid fa-snowflake"></i>';
                tempColor = '#64b5f6';
                tempStatus = 'Cold protection';
            } else if (batteryTemp < 15) {
                tempIcon = '<i class="fa-solid fa-triangle-exclamation"></i>';
                tempColor = '#ffa726';
                tempStatus = 'Reduced power';
            } else if (batteryTemp <= 45) {
                tempIcon = '<i class="fa-solid fa-circle-check"></i>';
                tempColor = '#66bb6a';
                tempStatus = 'Optimal';
            } else if (batteryTemp < 55) {
                tempIcon = '<i class="fa-solid fa-triangle-exclamation"></i>';
                tempColor = '#ffa726';
                tempStatus = 'Heat warning';
            } else {
                tempIcon = '<i class="fa-solid fa-fire"></i>';
                tempColor = '#ef5350';
                tempStatus = 'Heat protection';
            }
        }

        // Build max charge details HTML
        let maxChargeDetails = '';
        if (chargingCurveEnabled) {
            const mobile = isMobile();
            const tempAffectsPower = batteryTemp !== undefined && batteryTemp !== null &&
                                     (batteryTemp < 15 || batteryTemp > 45);
            
            if (mobile) {
                // Compact mobile view
                maxChargeDetails = `<span style="color: #66bb6a;">● ${maxChargeFixKW}kW</span>`;
                if (tempAffectsPower) {
                    maxChargeDetails += ` | <span style="color: ${tempColor};" title="${tempStatus}">${tempIcon} ${batteryTemp.toFixed(0)}°C</span>`;
                }
            } else {
                // Full desktop view
                maxChargeDetails = `<span style="color: #66bb6a;">● Dynamic</span><br>Config: ${maxChargeFixKW} kW`;
                if (batteryTemp !== undefined && batteryTemp !== null) {
                    maxChargeDetails += ` | Temp: ${batteryTemp.toFixed(1)}°C <span style="color: ${tempColor};" title="${tempStatus}">${tempIcon}</span>`;
                }
            }
        } else {
            maxChargeDetails = '<span style="color: #888;">⚪ Static/Fixed</span>';
        }

        const content = `
            <div class="battery-overview-section" style="height: 100%; overflow: hidden; padding: 10px; display: flex; flex-direction: column; gap: 15px; box-sizing: border-box;">
                
                <!-- Top Stats Cards -->
                <div class="battery-stats-container" style="flex: 0 0 auto; display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px;">
                    <div class="battery-stat-card">
                        <div class="label">State of Charge</div>
                        <div class="value">${soc}%</div>
                        <div class="sub-label">${getBatteryIcon(soc)}</div>
                    </div>
                    <div class="battery-stat-card">
                        <div class="label">Usable Energy</div>
                        <div class="value">${usableKWh} <span style="font-size: 0.6em;">kWh</span></div>
                        <div class="sub-label">Current capacity</div>
                    </div>
                    <div class="battery-stat-card">
                        <div class="label">Max Charge Power</div>
                        <div class="value">${maxChargeKW} <span style="font-size: 0.6em;">kW</span></div>
                        <div class="sub-label" style="font-size: 0.75em; line-height: 1.3;">${maxChargeDetails}</div>
                    </div>
                    <div class="battery-stat-card" style="border-left: 4px solid #2196f3;">
                        <div class="label">Stored Energy Price</div>
                        <div class="value">${wac} <span style="font-size: 0.6em;">ct/kWh</span></div>
                        <div class="sub-label">${priceSubLabel}</div>
                    </div>
                    <div class="battery-stat-card" style="border-left: 4px solid #4caf50;">
                        <div class="label">PV Share</div>
                        <div class="value">${pvRatio}${isEnabled ? '%' : ''}</div>
                        <div class="sub-label">Solar vs Grid</div>
                    </div>
                </div>

                <!-- Chart Section -->
                <div class="battery-overview-card" style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px; flex: 1 1 0; min-height: 0; display: flex; flex-direction: column;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex: 0 0 auto;">
                        <div style="font-weight: bold; color: #ccc;">Recent Charging Sessions</div>
                        <div style="font-size: 0.8em; color: #888;">${isEnabled ? `Last analysis: ${lastUpdate}` : 'Analysis disabled'}</div>
                    </div>
                    <div style="flex: 1 1 0; position: relative; min-height: 0; display: flex; align-items: center; justify-content: center;">
                        ${isEnabled ? '<canvas id="batterySessionsChart"></canvas>' : '<div style="color: #666; font-style: italic;">Dynamic price calculation is not enabled in configuration.</div>'}
                    </div>
                </div>

                <!-- Session List -->
                <div class="battery-overview-card battery-sessions-list" style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px; flex: 0 1 30%; min-height: 120px; display: flex; flex-direction: column;">
                    <div style="font-weight: bold; color: #ccc; margin-bottom: 10px; flex: 0 0 auto;">
                        ${isEnabled ? `Session Details (${sessions.length} sessions in last ${lookbackHours}h)` : 'Session Details'}
                    </div>
                    <div style="flex: 1 1 0; overflow-y: auto; min-height: 0;">
                        ${isEnabled ? `
                        <table style="width: 100%; font-size: 0.85em; border-collapse: collapse;">
                            <thead>
                                <tr style="border-bottom: 1px solid #444; color: #888; position: sticky; top: 0; background: rgb(58, 58, 58); z-index: 1;">
                                    <th style="text-align: left; padding: 5px;">Time</th>
                                    <th style="text-align: right; padding: 5px;">Duration</th>
                                    <th style="text-align: right; padding: 5px;">Total (kWh)</th>
                                    <th style="text-align: right; padding: 5px; color: #4caf50;">PV (kWh)</th>
                                    <th style="text-align: right; padding: 5px; color: #2196f3;">Grid (kWh)</th>
                                    <th style="text-align: right; padding: 5px;">Cost</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${sessions.length === 0 ? '<tr><td colspan="6" style="text-align: center; padding: 20px; color: #666;">No recent sessions found</td></tr>' : 
                                    sessions.slice().reverse().map(s => {
                                        const start = new Date(s.start_time);
                                        const end = new Date(s.end_time);
                                        const diffMs = end - start;
                                        const diffSec = Math.round(diffMs / 1000);
                                        let durationStr;
                                        if (diffSec < 60) {
                                            durationStr = diffSec + ' sec';
                                        } else if (diffSec < 3600) {
                                            durationStr = Math.round(diffSec / 60) + ' min';
                                        } else {
                                            durationStr = (diffSec / 3600).toFixed(1) + ' h';
                                        }
                                        const isGridHeavy = s.ratio < 50;
                                        const isInventory = s.is_inventory;
                                        const inventoryEnergy = s.inventory_energy || 0;
                                        
                                        let rowStyle = isGridHeavy ? `color: ${COLOR_MODE_CHARGE_FROM_GRID};` : '';
                                        if (isInventory) {
                                            rowStyle += 'background-color: rgba(76, 175, 80, 0.05); border-left: 3px solid #4caf50;';
                                        } else {
                                            rowStyle += 'opacity: 0.5;';
                                        }
                                        
                                        const inventoryInfo = isInventory && inventoryEnergy < s.charged_energy 
                                            ? `<div style="font-size: 0.8em; color: #4caf50;">Stored: ${(inventoryEnergy/1000).toFixed(3)}</div>` 
                                            : '';

                                        return `
                                        <tr style="border-bottom: 1px solid #333; ${rowStyle}">
                                            <td style="padding: 8px 5px;">
                                                ${start.toLocaleDateString()} ${start.getHours().toString().padStart(2, '0')}:${start.getMinutes().toString().padStart(2, '0')}
                                                ${isInventory ? ' <i class="fa-solid fa-box-archive" title="In Inventory" style="font-size: 0.8em; color: #4caf50;"></i>' : ''}
                                            </td>
                                            <td style="text-align: right; padding: 8px 5px;">${durationStr}</td>
                                            <td style="text-align: right; padding: 8px 5px; font-weight: bold;">
                                                ${(s.charged_energy / 1000).toFixed(3)}
                                                ${inventoryInfo}
                                            </td>
                                            <td style="text-align: right; padding: 8px 5px;">${(s.charged_from_pv / 1000).toFixed(3)}</td>
                                            <td style="text-align: right; padding: 8px 5px;">${(s.charged_from_grid / 1000).toFixed(3)}</td>
                                            <td style="text-align: right; padding: 8px 5px;">${s.cost.toFixed(2)} ${localization.currency_symbol}</td>
                                        </tr>
                                        `;
                                    }).join('')}
                            </tbody>
                        </table>
                        ` : '<div style="text-align: center; padding: 20px; color: #666; font-style: italic;">Session history is only available when dynamic price calculation is enabled.</div>'}
                    </div>
                </div>
            </div>
        `;

        showFullScreenOverlay(header, content);

        // Render Chart
        if (isEnabled && sessions.length > 0) {
            this.renderSessionsChart(sessions);
        }
    }

    /**
     * Render the charging sessions chart
     */
    renderSessionsChart(sessions) {
        const canvas = document.getElementById('batterySessionsChart');
        const ctx = canvas.getContext('2d');
        
        // Create hatched patterns for historical data
        const createPattern = (color) => {
            const pCanvas = document.createElement('canvas');
            const pCtx = pCanvas.getContext('2d');
            pCanvas.width = 5;
            pCanvas.height = 5;
            pCtx.strokeStyle = color;
            pCtx.lineWidth = 1;
            pCtx.beginPath();
            pCtx.moveTo(0, 5);
            pCtx.lineTo(5, 0);
            pCtx.stroke();
            return ctx.createPattern(pCanvas, 'repeat');
        };

        const pvPattern = createPattern('rgba(76, 175, 80, 0.6)');
        const gridPattern = createPattern('rgba(33, 150, 243, 0.6)');

        // Prepare data
        const labels = sessions.map(s => {
            const d = new Date(s.start_time);
            return `${d.getDate()}.${d.getMonth()+1} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`;
        });
        
        // Split data into Inventory and Historical parts
        const pvInventory = [];
        const pvHistorical = [];
        const gridInventory = [];
        const gridHistorical = [];

        sessions.forEach(s => {
            const total = s.charged_energy || 1;
            const inv = s.inventory_energy || 0;
            const hist = s.charged_energy - inv;
            const pvRatio = s.charged_from_pv / total;
            const gridRatio = s.charged_from_grid / total;

            pvInventory.push((inv * pvRatio) / 1000);
            pvHistorical.push((hist * pvRatio) / 1000);
            gridInventory.push((inv * gridRatio) / 1000);
            gridHistorical.push((hist * gridRatio) / 1000);
        });

        const mobile = isMobile();
        const fontSize = mobile ? 9 : 12;
        const tickSize = mobile ? 9 : 11;

        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'PV (Inventory)',
                        data: pvInventory,
                        backgroundColor: '#4caf50',
                        stack: 'Stack 0',
                    },
                    {
                        label: 'PV (Historical)',
                        data: pvHistorical,
                        backgroundColor: pvPattern,
                        stack: 'Stack 0',
                    },
                    {
                        label: 'Grid (Inventory)',
                        data: gridInventory,
                        backgroundColor: '#2196f3',
                        stack: 'Stack 0',
                    },
                    {
                        label: 'Grid (Historical)',
                        data: gridHistorical,
                        backgroundColor: gridPattern,
                        stack: 'Stack 0',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: window.innerWidth > 600,
                        position: 'top',
                        labels: { 
                            color: '#ccc',
                            font: { size: fontSize },
                            boxWidth: mobile ? 10 : 20,
                            // Filter legend to show only main categories
                            filter: (item) => !item.text.includes('Historical')
                        }
                    },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            label: function(context) {
                                let label = context.dataset.label || '';
                                if (label) label += ': ';
                                if (context.parsed.y !== undefined) {
                                    label += context.parsed.y.toFixed(3) + ' kWh';
                                }
                                return label;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        stacked: true,
                        ticks: { 
                            color: '#888', 
                            maxRotation: 45, 
                            minRotation: 45,
                            font: { size: tickSize }
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' }
                    },
                    y: {
                        stacked: true,
                        title: { 
                            display: !mobile, 
                            text: 'Energy (kWh)', 
                            color: '#888',
                            font: { size: fontSize }
                        },
                        ticks: { 
                            color: '#888',
                            font: { size: tickSize }
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' }
                    }
                }
            }
        });
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
