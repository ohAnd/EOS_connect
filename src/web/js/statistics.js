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

    /**
     * Show PV autoscaling details in a full-screen overlay
     */
    /**
     * Show PV autoscaling details in a full-screen overlay
     */
    async showPvAutoscalingOverlay() {
        try {
            const res = await fetch('api/pv_autoscaling/status?nocache=' + Date.now());
            if (!res.ok) {
                showFullScreenOverlay("PV Autoscaling", "<div style='color: #dc3545;'>Failed to load PV autoscaling data</div>");
                return;
            }

            const data = await res.json();
            const pa = data.pv_autoscaling || {};
            const enabled = pa.enabled || false;
            const sf = pa.scale_factors || pa.computed_scale_factors || {};
            const lastReading = pa.last_reading_timestamp || pa.last_reading;
            const aggregated = pa.aggregated_history || data.aggregated || {};
            const days = aggregated.days || [];
            const todays_partial = pa.todays_partial_data || {};
            const forecastArray = pa.current_forecast_array_raw || [];
            const forecastArrayScaled = pa.current_forecast_array_scaled || [];

            // Helper to safely convert to number
            const toNum = v => Number(String(v || 1).replace(',', '.')) || 1.0;

            // Helper to format kWh with proper handling of zero vs missing data
            const formatKwh = (value) => {
                // Handle missing/undefined values
                if (value === undefined || value === null || value === '') {
                    return '-- kWh';
                }
                const num = Number(String(value || '').replace(',', '.'));
                if (isNaN(num)) {
                    return '-- kWh';
                }
                // Show "0 kWh" for actual zero values (e.g., no PV at night)
                return num.toFixed(2) + ' kWh';
            };

            const s1 = toNum(sf['1'] || sf[1]);
            const s2 = toNum(sf['2'] || sf[2]);
            const s3 = toNum(sf['3'] || sf[3]);
            const s4 = toNum(sf['4'] || sf[4]);

            // Check data collection state
            const totalHoursRecorded = pa.total_hours_recorded || 0;
            const hasHistoricalData = days.length > 0;
            const hasPartialToday = Object.keys(todays_partial).length > 0;
            const isInitializing = totalHoursRecorded === 0 && !hasPartialToday;
            const isCollecting = totalHoursRecorded < 24 && !hasHistoricalData;

            // Build status banner
            let statusBanner = '';
            if (isInitializing) {
                statusBanner = `
                    <div style="background: linear-gradient(135deg, rgba(33, 150, 243, 0.15) 0%, rgba(100, 149, 237, 0.1) 100%); border: 1px solid rgba(100, 149, 237, 0.3); border-radius: 8px; padding: 15px;">
                        <div style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="font-size: 1.4em; margin-top: 2px;">🔄</div>
                            <div>
                                <div style="font-weight: 600; color: #64b5f6; margin-bottom: 6px;">Initializing PV Autoscaler</div>
                                <div style="font-size: 0.9em; color: #90caf9; line-height: 1.4;">
                                    No data collected yet. The autoscaler will collect hourly PV yield data and needs at least <strong>24 hours</strong> of historical data to calculate accurate scale factors.
                                    <br><br>
                                    <strong>What happens next:</strong>
                                    <ul style="margin: 8px 0 0 20px; padding: 0;">
                                        <li>Hourly data collection starts immediately (check "Today" section below)</li>
                                        <li>After 24 hours, scale factors will be calculated from yesterday's data</li>
                                        <li>Forecast multipliers will appear in the timeframe cards above</li>
                                        <li>Scaling improves with more historical data (recomm. 7+ days)</li>
                                    </ul>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            } else if (isCollecting) {
                const hoursRemaining = 24 - totalHoursRecorded;
                const progressPercent = (totalHoursRecorded / 24) * 100;
                statusBanner = `
                    <div style="background: linear-gradient(135deg, rgba(255, 152, 0, 0.15) 0%, rgba(255, 193, 7, 0.1) 100%); border: 1px solid rgba(255, 152, 0, 0.3); border-radius: 8px; padding: 15px;">
                        <div style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="font-size: 1.4em; margin-top: 2px;">⏳</div>
                            <div style="flex: 1;">
                                <div style="font-weight: 600; color: #ffb74d; margin-bottom: 6px;">Collecting Historical Data (${totalHoursRecorded}/24 hours)</div>
                                <div style="background-color: rgba(0,0,0,0.2); height: 6px; border-radius: 3px; overflow: hidden; margin-bottom: 8px;">
                                    <div style="background: linear-gradient(90deg, #ffc107 0%, #ff9800 100%); height: 100%; width: ${progressPercent}%;"></div>
                                </div>
                                <div style="font-size: 0.85em; color: #ffe082;">
                                    ${hoursRemaining > 0 ? `<strong>${hoursRemaining} hours</strong> remaining until first scale factors are calculated` : 'Data collection complete!'}
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            }

            // Build scale factors section
            const s1Str = s1.toFixed(3);
            const s2Str = s2.toFixed(3);
            const s3Str = s3.toFixed(3);
            const s4Str = s4.toFixed(3);

            // Build forecast comparison
            let forecastComparison = '';
            if (forecastArray && forecastArray.length > 0) {
                const slotsPerDay = forecastArray.length >= 48 ? Math.min(forecastArray.length, 96) : 24;
                const slotsPerHour = slotsPerDay / 24;

                const sumForecastTimeframe = (dayIndex, timeframeId) => {
                    const hourStart = (timeframeId - 1) * 6;
                    const slotStart = dayIndex * slotsPerDay + hourStart * slotsPerHour;
                    const slotEnd = slotStart + 6 * slotsPerHour;
                    let sum = 0;
                    for (let i = slotStart; i < slotEnd && i < forecastArray.length; i++) {
                        sum += toNum(forecastArray[i]);
                    }
                    return sum / 1000;
                };

                let todayOriginal = 0, tomorrowOriginal = 0;
                let todayScaled = 0, tomorrowScaled = 0;

                for (let tf = 1; tf <= 4; tf++) {
                    const scale = toNum(sf[tf.toString()] || sf[tf]);
                    const todayTf = sumForecastTimeframe(0, tf);
                    const tomorrowTf = sumForecastTimeframe(1, tf);

                    todayOriginal += todayTf;
                    tomorrowOriginal += tomorrowTf;
                    todayScaled += todayTf * scale;
                    tomorrowScaled += tomorrowTf * scale;
                }

                forecastComparison = `
                    <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-chart-line" style="margin-right: 6px;"></i>
                            Forecast Comparison (Daily Total)
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                            <div style="background-color: rgba(255,255,255,0.05); border-radius: 6px; padding: 12px; border-left: 3px solid #4a9eff;">
                                <div style="font-weight: 600; color: #ddd; margin-bottom: 10px;">Today</div>
                                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                                    <span style="color: #aaa;">Original:</span>
                                    <span style="color: #fff; font-weight: 500;">${todayOriginal.toFixed(2)} kWh</span>
                                </div>
                                <div style="display: flex; justify-content: space-between;">
                                    <span style="color: #aaa;">Corrected:</span>
                                    <span><span style="color: #888; font-size: 0.85em;">(${todayScaled >= todayOriginal ? '+' : ''}${(todayScaled - todayOriginal).toFixed(2)} kWh)</span> <span style="color: #4caf50; font-weight: 500; font-size: 1.1em;">${todayScaled.toFixed(2)} kWh</span></span>
                                </div>
                            </div>
                            <div style="background-color: rgba(255,255,255,0.05); border-radius: 6px; padding: 12px; border-left: 3px solid #4a9eff;">
                                <div style="font-weight: 600; color: #ddd; margin-bottom: 10px;">Tomorrow</div>
                                <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                                    <span style="color: #aaa;">Original:</span>
                                    <span style="color: #fff; font-weight: 500;">${tomorrowOriginal.toFixed(2)} kWh</span>
                                </div>
                                <div style="display: flex; justify-content: space-between;">
                                    <span style="color: #aaa;">Corrected:</span>
                                    <span><span style="color: #888; font-size: 0.85em;">(${tomorrowScaled >= tomorrowOriginal ? '+' : ''}${(tomorrowScaled - tomorrowOriginal).toFixed(2)} kWh)</span> <span style="color: #4caf50; font-weight: 500; font-size: 1.1em;">${tomorrowScaled.toFixed(2)} kWh</span></span>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            }

            // Build today's partial data section
            let todaysPartialHtml = '';
            if (hasPartialToday) {
                const todaysActual = todays_partial.actual_kwh || {};
                const todaysHours = todays_partial.hours_collected || 0;

                // Calculate today's forecast from forecastArray (we already know this!)
                let todayForecastByTimeframe = {};
                if (forecastArrayScaled && forecastArrayScaled.length > 0) {
                    const slotsPerDay = forecastArrayScaled.length >= 48 ? Math.min(forecastArrayScaled.length, 96) : 24;
                    const slotsPerHour = slotsPerDay / 24;

                    for (let tf = 1; tf <= 4; tf++) {
                        const hourStart = (tf - 1) * 6;
                        const slotStart = hourStart * slotsPerHour;
                        const slotEnd = slotStart + 6 * slotsPerHour;
                        let sum = 0;
                        for (let i = slotStart; i < slotEnd && i < forecastArrayScaled.length; i++) {
                            sum += toNum(forecastArrayScaled[i]);
                        }
                        todayForecastByTimeframe[tf] = sum / 1000;  // Convert Wh to kWh
                    }
                }

                todaysPartialHtml = `
                    <div style="background-color: rgba(180,180,180,0.12); border-radius: 8px; padding: 15px; border-left: 4px solid #aaa;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-calendar-day" style="margin-right: 6px;"></i>
                            Today (Partial) — Not Used for Scaling
                        </div>
                        <div style="background-color: rgba(255,255,255,0.06); border-radius: 6px; padding: 10px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-weight: 500; color: #bbb;">Partial data being collected (${todaysHours}h) — Will be saved and used tomorrow</span>
                            </div>
                            <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; font-size: 0.8em;">
                                <div style="text-align: center; padding: 6px; background-color: rgba(76,175,80,0.15); border-radius: 4px;">
                                    <div style="color: #999; font-size: 0.75em;">T1</div>
                                    <div style="color: #7ccc7c;">R: ${formatKwh(todaysActual['1'])}</div>
                                    <div style="color: #aaa;">F: ${todayForecastByTimeframe[1] ? todayForecastByTimeframe[1].toFixed(2) + ' kWh' : '-- kWh'}</div>
                                </div>
                                <div style="text-align: center; padding: 6px; background-color: rgba(76,175,80,0.15); border-radius: 4px;">
                                    <div style="color: #999; font-size: 0.75em;">T2</div>
                                    <div style="color: #7ccc7c;">R: ${formatKwh(todaysActual['2'])}</div>
                                    <div style="color: #aaa;">F: ${todayForecastByTimeframe[2] ? todayForecastByTimeframe[2].toFixed(2) + ' kWh' : '-- kWh'}</div>
                                </div>
                                <div style="text-align: center; padding: 6px; background-color: rgba(255,193,7,0.15); border-radius: 4px;">
                                    <div style="color: #999; font-size: 0.75em;">T3</div>
                                    <div style="color: #ffd860;">R: ${formatKwh(todaysActual['3'])}</div>
                                    <div style="color: #aaa;">F: ${todayForecastByTimeframe[3] ? todayForecastByTimeframe[3].toFixed(2) + ' kWh' : '-- kWh'}</div>
                                </div>
                                <div style="text-align: center; padding: 6px; background-color: rgba(244,67,54,0.15); border-radius: 4px;">
                                    <div style="color: #999; font-size: 0.75em;">T4</div>
                                    <div style="color: #f77777;">R: ${formatKwh(todaysActual['4'])}</div>
                                    <div style="color: #aaa;">F: ${todayForecastByTimeframe[4] ? todayForecastByTimeframe[4].toFixed(2) + ' kWh' : '-- kWh'}</div>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            } else if (isCollecting) {
                todaysPartialHtml = `
                    <div style="background-color: rgba(180,180,180,0.12); border-radius: 8px; padding: 15px; border-left: 4px solid #aaa;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-calendar-day" style="margin-right: 6px;"></i>
                            Today (Partial) — Not Used for Scaling
                        </div>
                        <div style="text-align: center; padding: 20px 15px; color: #888;">
                            <div style="font-size: 1.2em; margin-bottom: 8px;">⏳</div>
                            <div style="color: #aaa; font-size: 0.9em;">Waiting for first hourly reading... Data will appear here as it's collected.</div>
                        </div>
                    </div>
                `;
            }

            // Build historical data section
            let historicalHtml = '';
            if (days.length > 0) {
                historicalHtml = `
                    <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-history" style="margin-right: 6px;"></i>
                            Historical Data Used for Scaling (Yesterday & Before)
                        </div>
                        <div style="display: flex; flex-direction: column; gap: 8px; max-height: 300px; overflow-y: auto;">
                `;

                const sortedDays = [...days].sort((a, b) => new Date(b.date) - new Date(a.date));

                sortedDays.forEach(day => {
                    const dateObj = new Date(day.date);
                    const dateStr = dateObj.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
                    const actual = day.actual_kwh || {};
                    const forecast = day.forecast_kwh || {};
                    const hours = day.hours_recorded || 0;

                    historicalHtml += `
                        <div style="background-color: rgba(255,255,255,0.05); border-radius: 6px; padding: 10px; border-left: 3px solid #4a9eff;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                <span style="font-weight: 600; color: #ddd;">${dateStr}</span>
                                <span style="font-size: 0.8em; color: #888;">${hours} hours recorded</span>
                            </div>
                            <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; font-size: 0.8em;">
                                <div style="text-align: center; padding: 6px; background-color: rgba(76,175,80,0.1); border-radius: 4px;">
                                    <div style="color: #888; font-size: 0.75em;">T1</div>
                                    <div style="color: #4caf50;">R: ${formatKwh(actual['1'])}</div>
                                    <div style="color: #aaa;">F: ${formatKwh(forecast['1'])}</div>
                                </div>
                                <div style="text-align: center; padding: 6px; background-color: rgba(76,175,80,0.1); border-radius: 4px;">
                                    <div style="color: #888; font-size: 0.75em;">T2</div>
                                    <div style="color: #4caf50;">R: ${formatKwh(actual['2'])}</div>
                                    <div style="color: #aaa;">F: ${formatKwh(forecast['2'])}</div>
                                </div>
                                <div style="text-align: center; padding: 6px; background-color: rgba(255,193,7,0.1); border-radius: 4px;">
                                    <div style="color: #888; font-size: 0.75em;">T3</div>
                                    <div style="color: #ffc107;">R: ${formatKwh(actual['3'])}</div>
                                    <div style="color: #aaa;">F: ${formatKwh(forecast['3'])}</div>
                                </div>
                                <div style="text-align: center; padding: 6px; background-color: rgba(244,67,54,0.1); border-radius: 4px;">
                                    <div style="color: #888; font-size: 0.75em;">T4</div>
                                    <div style="color: #f44336;">R: ${formatKwh(actual['4'])}</div>
                                    <div style="color: #aaa;">F: ${formatKwh(forecast['4'])}</div>
                                </div>
                            </div>
                        </div>
                    `;
                });

                historicalHtml += `
                            </div>
                            <div style="font-size: 0.8em; color: #888; margin-top: 10px;">
                                <strong>Legend:</strong> R = Real yield | F = Forecast
                            </div>
                        </div>
                    `;
            } else if (!hasHistoricalData && !isInitializing) {
                historicalHtml = `
                    <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-history" style="margin-right: 6px;"></i>
                            Historical Data Used for Scaling (Yesterday & Before)
                        </div>
                        <div style="text-align: center; padding: 30px 15px; color: #888;">
                            <div style="font-size: 1.4em; margin-bottom: 10px;">⏳</div>
                            <div style="color: #aaa;">
                                <strong>Waiting for historical data...</strong><br>
                                <span style="font-size: 0.9em;">Once a complete day passes, it will appear here for scale factor calculation.</span>
                            </div>
                        </div>
                    </div>
                `;
            }

            // Build header
            const header = `
                <div style="display: flex; align-items: center; gap: 10px;">
                    <i class="fa-solid fa-solar-panel" style="color: #cccccc;"></i>
                    <span>PV Autoscaling Details</span>
                </div>
            `;

            // Build status icon and text
            const statusIcon = enabled ? '<i class="fa-solid fa-check-circle" style="color: #4caf50;"></i>' : '<i class="fa-solid fa-times-circle" style="color: #f44336;"></i>';
            const statusText = enabled ? '<span style="color: #4caf50;">Enabled</span>' : '<span style="color: #f44336;">Disabled</span>';

            // Calculate daily average - weighted by forecast distribution
            const s1f = toNum(sf['1'] || sf[1]);
            const s2f = toNum(sf['2'] || sf[2]);
            const s3f = toNum(sf['3'] || sf[3]);
            const s4f = toNum(sf['4'] || sf[4]);
            
            // Compute weighted average based on current forecast distribution
            let dailyAvg, percentChange, isWeighted = false;
            const arithmeticAvg = (s1f + s2f + s3f + s4f) / 4;
            
            if (forecastArray && forecastArray.length > 0) {
                const slotsPerDay = forecastArray.length >= 48 ? Math.min(forecastArray.length, 96) : 24;
                const slotsPerHour = slotsPerDay / 24;
                const sumForecastTimeframe = (dayIndex, timeframeId) => {
                    const hourStart = (timeframeId - 1) * 6;
                    const slotStart = dayIndex * slotsPerDay + hourStart * slotsPerHour;
                    const slotEnd = slotStart + 6 * slotsPerHour;
                    let sum = 0;
                    for (let i = slotStart; i < slotEnd && i < forecastArray.length; i++) {
                        sum += toNum(forecastArray[i]);
                    }
                    return sum;
                };
                
                let totalForecast = 0;
                let weightedSum = 0;
                for (let tf = 1; tf <= 4; tf++) {
                    const tfForecast = sumForecastTimeframe(0, tf);
                    const factor = toNum(sf[tf.toString()] || sf[tf]);
                    totalForecast += tfForecast;
                    weightedSum += tfForecast * factor;
                }
                
                if (totalForecast > 0) {
                    dailyAvg = weightedSum / totalForecast;
                    isWeighted = true;
                } else {
                    dailyAvg = arithmeticAvg;
                }
            } else {
                dailyAvg = arithmeticAvg;
            }
            
            percentChange = ((dailyAvg - 1.0) * 100);
            let percentColor = '#90ee90';
            if (dailyAvg < 0.98) {
                percentColor = '#90caf9';
            } else if (dailyAvg > 1.02) {
                percentColor = '#ffab91';
            }

            // Build final content
            const content = `
                <div style="height: 100%; overflow: hidden; padding: 10px; display: flex; flex-direction: column; gap: 15px; box-sizing: border-box;">
                    ${statusBanner}

                    <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px; border-left: 4px solid ${enabled ? '#4caf50' : '#f44336'};">
                        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                            <span style="color: #ccc; font-weight: bold;">Status</span>
                            <div>${statusIcon} ${statusText}</div>
                        </div>
                        <div style="font-size: 0.9em; color: #aaa;">
                            ${lastReading ? 'Last updated: ' + new Date(lastReading).toLocaleString() : 'No data available'}
                        </div>
                    </div>

                    <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px; flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 15px;">
                        <div>
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                                <div style="font-weight: bold; color: #ccc;">Timeframe Scale Factors</div>
                                <div style="font-size: 0.85em; color: #888;">Today's forecast multipliers</div>
                            </div>

                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px;">
                                <div style="background-color: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #888; margin-bottom: 6px;">Timeframe 1</div>
                                    <div style="font-size: 0.8em; color: #aaa; margin-bottom: 8px;">00:00 - 05:59</div>
                                    <div style="font-size: 1.8em; font-weight: bold; color: #4caf50; font-family: monospace;">${s1Str}×</div>
                                </div>

                                <div style="background-color: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #888; margin-bottom: 6px;">Timeframe 2</div>
                                    <div style="font-size: 0.8em; color: #aaa; margin-bottom: 8px;">06:00 - 11:59</div>
                                    <div style="font-size: 1.8em; font-weight: bold; color: #4caf50; font-family: monospace;">${s2Str}×</div>
                                </div>

                                <div style="background-color: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #888; margin-bottom: 6px;">Timeframe 3</div>
                                    <div style="font-size: 0.8em; color: #aaa; margin-bottom: 8px;">12:00 - 17:59</div>
                                    <div style="font-size: 1.8em; font-weight: bold; color: #ffc107; font-family: monospace;">${s3Str}×</div>
                                </div>

                                <div style="background-color: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #888; margin-bottom: 6px;">Timeframe 4</div>
                                    <div style="font-size: 0.8em; color: #aaa; margin-bottom: 8px;">18:00 - 23:59</div>
                                    <div style="font-size: 1.8em; font-weight: bold; color: #f44336; font-family: monospace;">${s4Str}×</div>
                                </div>

                                <div style="background-color: rgba(65, 105, 225, 0.15); border: 2px solid rgba(100, 149, 237, 0.4); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #6495ed; margin-bottom: 4px; font-weight: 600;">WHOLE DAY${isWeighted ? '<br><span style="font-size: 0.7em; font-weight: normal;">(forecast weighted)</span>' : ''}</div>
                                    <div style="font-size: 1.8em; font-weight: bold; color: #6495ed; font-family: monospace;">${dailyAvg.toFixed(3)}× (${percentChange >= 0 ? '+' : ''}${percentChange.toFixed(1)}%)</div>
                                </div>
                            </div>
                        </div>

                        ${forecastComparison}
                        ${todaysPartialHtml}
                        ${historicalHtml}

                        <div style="padding-top: 15px; border-top: 1px solid rgba(255, 255, 255, 0.1); font-size: 0.85em; color: #999;">
                            <div style="margin-bottom: 8px;">
                                <i class="fa-solid fa-info-circle" style="color: #2196f3; margin-right: 6px;"></i>
                                <strong>How it works:</strong>
                            </div>
                            <div style="margin-left: 24px; line-height: 1.5;">
                                The autoscaler learns from complete days of historical data (yesterday and before) to calculate scale factors for today's forecasts. Each timeframe gets a unique multiplier based on how actual PV production compares to the forecasted values.
                                <br><br>
                                <strong>Today's partial data</strong> (if shown) is <strong>not yet used</strong> for calculation — it's being collected for later use. Once today completes (after midnight), this data becomes part of the historical database and will be used tomorrow.
                                <br><br>
                                Factors closer to 1.0 indicate accurate forecasts; factors &lt; 1.0 mean the forecast was optimistic, while factors &gt; 1.0 mean it was pessimistic.
                            </div>
                        </div>
                    </div>
                </div>
            `;

            showFullScreenOverlay(header, content);
        } catch (err) {
            console.error('[PV Autoscaling] Failed to load overlay:', err);
            showFullScreenOverlay("PV Autoscaling", "<div style='color: #dc3545;'>Error loading PV autoscaling data: " + err.message + "</div>");
        }
    }
}

// Legacy compatibility function
function showStatistics(data_request, data_response, data_controls) {
    if (statisticsManager) {
        statisticsManager.showStatistics(data_request, data_response, data_controls);
    }
}
