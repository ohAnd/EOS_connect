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

        // Prefer the live totals the server derives from the same scaled array the PV
        // auto-scaling overlay renders. data_request is a snapshot of the last optimizer
        // run: an autoscaler factor recomputed since then leaves it disagreeing with the
        // overlay, and on evopt it also carries the partial-slot discount, which is an
        // optimizer input rather than part of a day's forecast. The sums above stay as
        // the fallback for a payload without the field.
        const pvTotals = (data_controls && data_controls["pv_forecast"]) || {};
        const liveToday = Number(pvTotals["today_wh"]);
        const liveTomorrow = Number(pvTotals["tomorrow_wh"]);
        if (pvTotals["today_wh"] !== null && isFinite(liveToday)) {
            yield_today = liveToday / 1000;
        }
        if (pvTotals["tomorrow_wh"] !== null && isFinite(liveTomorrow)) {
            yield_tomorrow = liveTomorrow / 1000;
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

            // The day's partitioning is defined once, in the backend, and served here so
            // this page never carries a second copy of the boundaries. The fallback only
            // covers a cached page talking to an older build - it renders the current
            // scheme rather than nothing.
            const timeframes = (Array.isArray(pa.timeframe_bounds) && pa.timeframe_bounds.length
                ? pa.timeframe_bounds
                : [{ id: 1, start: 0, end: 8 }, { id: 2, start: 8, end: 12 },
                   { id: 3, start: 12, end: 16 }, { id: 4, start: 16, end: 24 }]
            ).map(t => ({
                ...t,
                label: t.label || `${String(t.start).padStart(2, '0')}:00 - ${String(t.end - 1).padStart(2, '0')}:59`,
            }));
            // Cool through warm across the day, so the midday blocks stand out. Clamped
            // rather than cycled, so a fifth block would not read as another morning.
            const TF_COLORS = [
                { accent: '#4caf50', soft: '#7ccc7c', rgb: '76,175,80' },
                { accent: '#4caf50', soft: '#7ccc7c', rgb: '76,175,80' },
                { accent: '#ffc107', soft: '#ffd860', rgb: '255,193,7' },
                { accent: '#f44336', soft: '#f77777', rgb: '244,67,54' },
            ];
            const tfColor = i => TF_COLORS[Math.min(i, TF_COLORS.length - 1)];

            // Scale factors: a missing or unparseable multiplier means "no scaling".
            const toNum = v => Number(String(v || 1).replace(',', '.')) || 1.0;
            // Forecast slots: a missing or zero slot is zero energy, not 1 Wh. Reusing
            // toNum here would invent a watt-hour for every night slot.
            const slotWh = v => {
                const n = Number(String(v ?? '').replace(',', '.'));
                return isFinite(n) ? n : 0;
            };

            // Slot width comes from the resolution the backend reports, never from the
            // array length: an hourly install publishes a 48-value two-day horizon, which
            // read as "48 slots per day" would double every "today" total and push
            // "tomorrow" past the end of the array.
            const slotsPerHour = Math.max(1, Math.round(3600 / (Number(pa.used_time_frame_base) || 3600)));
            const slotsPerDay = 24 * slotsPerHour;

            /** Sum one timeframe of one day from a Wh-per-slot array, returning kWh. */
            const sumTimeframe = (arr, dayIndex, timeframeId) => {
                if (!arr || !arr.length) return 0;
                const tf = timeframes.find(t => t.id === timeframeId);
                if (!tf) return 0;
                const slotStart = dayIndex * slotsPerDay + tf.start * slotsPerHour;
                const slotEnd = dayIndex * slotsPerDay + tf.end * slotsPerHour;
                let sum = 0;
                for (let i = slotStart; i < slotEnd && i < arr.length; i++) {
                    sum += slotWh(arr[i]);
                }
                return sum / 1000;
            };

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

            const factorFor = id => toNum(sf[String(id)] || sf[id]);

            // Check data collection state
            const totalHoursRecorded = pa.total_hours_recorded || 0;
            const hoursRequired = Number(pa.min_data_hours_required) > 0
                ? Number(pa.min_data_hours_required)
                : 24;
            const hasHistoricalData = days.length > 0;
            const hasPartialToday = Object.keys(todays_partial).length > 0;
            const isInitializing = totalHoursRecorded === 0 && !hasPartialToday;
            const isCollecting = totalHoursRecorded < hoursRequired && !hasHistoricalData;

            // A stalled collector looks exactly like a fresh install unless the error is
            // shown: same "no data", same enabled tick. Surface it above everything else.
            const lastError = pa.last_error;
            const failures = Number(pa.consecutive_failures) || 0;

            // Build status banner
            let statusBanner = '';
            if (lastError) {
                statusBanner = `
                    <div style="background: linear-gradient(135deg, rgba(220, 53, 69, 0.18) 0%, rgba(220, 53, 69, 0.08) 100%); border: 1px solid rgba(220, 53, 69, 0.45); border-radius: 8px; padding: 15px;">
                        <div style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="font-size: 1.4em; margin-top: 2px;">⚠️</div>
                            <div>
                                <div style="font-weight: 600; color: #ef5350; margin-bottom: 6px;">
                                    PV Autoscaler cannot read the yield counter${failures > 1 ? ` (${failures} attempts in a row)` : ''}
                                </div>
                                <div style="font-size: 0.9em; color: #ffcdd2; line-height: 1.4;">
                                    <code style="background: rgba(0,0,0,0.25); padding: 2px 6px; border-radius: 4px;">${escapeHtml(String(lastError))}</code>
                                    <br><br>
                                    No new data is being collected, so the scale factors below will not update.
                                    Check <strong>Sensor entity</strong>${pa.sensor_entity_id ? ` (<code>${escapeHtml(String(pa.sensor_entity_id))}</code>)` : ''},
                                    the data source URL and the access token in the configuration.
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            } else if (enabled && pa.running === false) {
                statusBanner = `
                    <div style="background: rgba(255, 152, 0, 0.12); border: 1px solid rgba(255, 152, 0, 0.35); border-radius: 8px; padding: 15px;">
                        <div style="font-weight: 600; color: #ffb74d;">Autoscaler enabled but not collecting</div>
                        <div style="font-size: 0.9em; color: #ffe082; margin-top: 6px;">
                            The background collection service is not running. Restart EOS connect to start it.
                        </div>
                    </div>
                `;
            } else if (isInitializing) {
                statusBanner = `
                    <div style="background: linear-gradient(135deg, rgba(33, 150, 243, 0.15) 0%, rgba(100, 149, 237, 0.1) 100%); border: 1px solid rgba(100, 149, 237, 0.3); border-radius: 8px; padding: 15px;">
                        <div style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="font-size: 1.4em; margin-top: 2px;">🔄</div>
                            <div>
                                <div style="font-weight: 600; color: #64b5f6; margin-bottom: 6px;">Initializing PV Autoscaler</div>
                                <div style="font-size: 0.9em; color: #90caf9; line-height: 1.4;">
                                    No data collected yet. The autoscaler will collect hourly PV yield data and needs at least <strong>${hoursRequired} hours</strong> of historical data to calculate accurate scale factors.
                                    <br><br>
                                    <strong>What happens next:</strong>
                                    <ul style="margin: 8px 0 0 20px; padding: 0;">
                                        <li>Hourly data collection starts immediately (check "Today" section below)</li>
                                        <li>After ${hoursRequired} hours, scale factors will be calculated from yesterday's data</li>
                                        <li>Forecast multipliers will appear in the timeframe cards above</li>
                                        <li>Scaling improves with more historical data (recomm. 7+ days)</li>
                                    </ul>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            } else if (isCollecting) {
                const hoursRemaining = hoursRequired - totalHoursRecorded;
                const progressPercent = (totalHoursRecorded / hoursRequired) * 100;
                statusBanner = `
                    <div style="background: linear-gradient(135deg, rgba(255, 152, 0, 0.15) 0%, rgba(255, 193, 7, 0.1) 100%); border: 1px solid rgba(255, 152, 0, 0.3); border-radius: 8px; padding: 15px;">
                        <div style="display: flex; align-items: flex-start; gap: 12px;">
                            <div style="font-size: 1.4em; margin-top: 2px;">⏳</div>
                            <div style="flex: 1;">
                                <div style="font-weight: 600; color: #ffb74d; margin-bottom: 6px;">Collecting Historical Data (${totalHoursRecorded}/${hoursRequired} hours)</div>
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

            // Build forecast comparison
            let forecastComparison = '';
            if (forecastArray && forecastArray.length > 0) {
                const sumForecastTimeframe = (dayIndex, timeframeId) =>
                    sumTimeframe(forecastArray, dayIndex, timeframeId);

                let todayOriginal = 0, tomorrowOriginal = 0;
                let todayScaled = 0, tomorrowScaled = 0;

                // Prefer the scaled array the backend actually handed to the optimizer.
                // Re-multiplying here would drift from it, because apply_scaling rounds
                // every slot to one decimal.
                const haveScaled = forecastArrayScaled && forecastArrayScaled.length > 0;

                for (const { id: tf } of timeframes) {
                    const scale = factorFor(tf);
                    const todayTf = sumForecastTimeframe(0, tf);
                    const tomorrowTf = sumForecastTimeframe(1, tf);

                    todayOriginal += todayTf;
                    tomorrowOriginal += tomorrowTf;
                    todayScaled += haveScaled ? sumTimeframe(forecastArrayScaled, 0, tf) : todayTf * scale;
                    tomorrowScaled += haveScaled ? sumTimeframe(forecastArrayScaled, 1, tf) : tomorrowTf * scale;
                }

                forecastComparison = `
                    <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-chart-line" style="margin-right: 6px;"></i>
                            Forecast Comparison (Daily Total)
                        </div>
                        <div class="pv-scale-forecast-grid">
                            <div style="background-color: rgba(255,255,255,0.05); border-radius: 6px; padding: 12px; border-left: 3px solid #4a9eff;">
                                <div style="font-weight: 600; color: #ddd; margin-bottom: 10px;">Today</div>
                                <div class="pv-scale-kv" style="margin-bottom: 8px;">
                                    <span style="color: #aaa;">Original:</span>
                                    <span style="color: #fff; font-weight: 500;">${todayOriginal.toFixed(2)} kWh</span>
                                </div>
                                <div class="pv-scale-kv">
                                    <span style="color: #aaa;">Corrected:</span>
                                    <span><span style="color: #888; font-size: 0.85em;">(${todayScaled >= todayOriginal ? '+' : ''}${(todayScaled - todayOriginal).toFixed(2)} kWh)</span> <span style="color: #4caf50; font-weight: 500; font-size: 1.1em;">${todayScaled.toFixed(2)} kWh</span></span>
                                </div>
                            </div>
                            <div style="background-color: rgba(255,255,255,0.05); border-radius: 6px; padding: 12px; border-left: 3px solid #4a9eff;">
                                <div style="font-weight: 600; color: #ddd; margin-bottom: 10px;">Tomorrow</div>
                                <div class="pv-scale-kv" style="margin-bottom: 8px;">
                                    <span style="color: #aaa;">Original:</span>
                                    <span style="color: #fff; font-weight: 500;">${tomorrowOriginal.toFixed(2)} kWh</span>
                                </div>
                                <div class="pv-scale-kv">
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

                // Calculate today's forecast from forecastArray (both original and scaled)
                let todayForecastByTimeframeOriginal = {};
                let todayForecastByTimeframe = {};
                for (const { id: tf } of timeframes) {
                    todayForecastByTimeframeOriginal[tf] = sumTimeframe(forecastArray, 0, tf);
                    todayForecastByTimeframe[tf] = sumTimeframe(forecastArrayScaled, 0, tf);
                }

                todaysPartialHtml = `
                    <div style="background-color: rgba(180,180,180,0.12); border-radius: 8px; padding: 15px; border-left: 4px solid #aaa;">
                        <div style="font-weight: bold; color: #ccc; margin-bottom: 12px;">
                            <i class="fa-solid fa-calendar-day" style="margin-right: 6px;"></i>
                            Today (Partial) — Not Used for Scaling
                        </div>
                        <div style="background-color: rgba(255,255,255,0.06); border-radius: 6px; padding: 10px;">
                            <div class="pv-scale-kv" style="margin-bottom: 8px;">
                                <span style="font-weight: 500; color: #bbb;">Partial data being collected (${todaysHours}h) — Will be saved and used tomorrow</span>
                            </div>
                            <div class="pv-scale-tf-grid" style="grid-template-columns: repeat(${timeframes.length}, 1fr);">
                                ${timeframes.map((tf, i) => `
                                <div style="text-align: center; padding: 6px; background-color: rgba(${tfColor(i).rgb},0.15); border-radius: 4px;" title="${tf.label}">
                                    <div style="color: #999; font-size: 0.75em;">T${tf.id}</div>
                                    <div style="color: ${tfColor(i).soft};">R: ${formatKwh(todaysActual[String(tf.id)])}</div>
                                    <div style="color: #aaa;">F: ${todayForecastByTimeframeOriginal[tf.id]?.toFixed(2) || '--'} → ${todayForecastByTimeframe[tf.id]?.toFixed(2) || '--'} kWh</div>
                                </div>`).join('')}
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
                        <div class="pv-scale-history">
                `;

                // Drop rows with no usable date rather than rendering them as "Jan 1":
                // new Date(null) is the epoch, which reads as a real day in the list.
                const sortedDays = [...days]
                    .filter(day => day && day.date)
                    .sort((a, b) => new Date(b.date) - new Date(a.date));

                sortedDays.forEach(day => {
                    const dateObj = new Date(day.date);
                    const dateStr = isNaN(dateObj.getTime())
                        ? String(day.date)
                        : dateObj.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
                    const actual = day.actual_kwh || {};
                    const forecast = day.forecast_kwh || {};
                    const hours = day.hours_collected || 0;
                    // Days restored from a backup are not measurements from this
                    // system; label them so a seeded scale factor is never mistaken
                    // for one this install learnt.
                    const restored = day.origin && day.origin !== 'measured';
                    const originBadge = restored
                        ? `<span title="Restored from a backup${day.origin === 'seeded'
                            ? ', with its dates shifted into the current window'
                            : ''} - not measured on this system"
                                 style="font-size: 0.7em; color: #ffc107; border: 1px solid rgba(255,193,7,0.5); border-radius: 3px; padding: 1px 5px; margin-left: 6px;">
                            <i class="fa-solid fa-box-archive"></i> ${day.origin}
                           </span>`
                        : '';

                    historicalHtml += `
                        <div style="background-color: rgba(255,255,255,0.05); border-radius: 6px; padding: 10px; border-left: 3px solid ${restored ? '#ffc107' : '#4a9eff'};">
                            <div class="pv-scale-kv" style="margin-bottom: 8px;">
                                <span style="font-weight: 600; color: #ddd;">${dateStr}${originBadge}</span>
                                <span style="font-size: 0.8em; color: #888;">${hours} hours recorded</span>
                            </div>
                            <div class="pv-scale-tf-grid" style="grid-template-columns: repeat(${timeframes.length}, 1fr);">
                                ${timeframes.map((tf, i) => `
                                <div style="text-align: center; padding: 6px; background-color: rgba(${tfColor(i).rgb},0.1); border-radius: 4px;" title="${tf.label}">
                                    <div style="color: #888; font-size: 0.75em;">T${tf.id}</div>
                                    <div style="color: ${tfColor(i).accent};">R: ${formatKwh(actual[String(tf.id)])}</div>
                                    <div style="color: #aaa;">F: ${formatKwh(forecast[String(tf.id)])}</div>
                                </div>`).join('')}
                            </div>
                        </div>
                    `;
                });

                historicalHtml += `
                            </div>
                            <div style="font-size: 0.8em; color: #888; margin-top: 10px;">
                                <strong>Legend:</strong> R = Real yield | F = Forecast
                                ${(pa.restored_hours || 0) > 0
                                    ? ` | <span style="color: #ffc107;"><i class="fa-solid fa-box-archive"></i> ${pa.restored_hours} hour(s) restored from a backup, not measured here</span>`
                                    : ''}
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
            
            // Compute weighted average based on current forecast distribution
            let dailyAvg, percentChange, isWeighted = false;
            const arithmeticAvg = timeframes.reduce((a, t) => a + factorFor(t.id), 0) / timeframes.length;
            
            if (forecastArray && forecastArray.length > 0) {
                let totalForecast = 0;
                let weightedSum = 0;
                for (const { id: tf } of timeframes) {
                    const tfForecast = sumTimeframe(forecastArray, 0, tf);
                    const factor = factorFor(tf);
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
                <div class="pv-scale-root">
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

                    <div class="pv-scale-panel" style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px;">
                        <div>
                            <div class="pv-scale-section-head">
                                <div style="font-weight: bold; color: #ccc;">Timeframe Scale Factors</div>
                                <div style="font-size: 0.85em; color: #888;">Today's forecast multipliers</div>
                            </div>

                            <div class="pv-scale-tiles">
                                ${timeframes.map((tf, i) => `
                                <div style="background-color: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #888; margin-bottom: 6px;">Timeframe ${tf.id}</div>
                                    <div style="font-size: 0.8em; color: #aaa; margin-bottom: 8px;">${tf.label}</div>
                                    <div class="pv-scale-total-value" style="color: ${tfColor(i).accent};">${factorFor(tf.id).toFixed(3)}×</div>
                                </div>`).join('')}

                                <div class="pv-scale-tile-total" style="background-color: rgba(65, 105, 225, 0.15); border: 2px solid rgba(100, 149, 237, 0.4); border-radius: 6px; padding: 12px; text-align: center;">
                                    <div style="font-size: 0.85em; color: #6495ed; margin-bottom: 4px; font-weight: 600;">WHOLE DAY${isWeighted ? '<br><span style="font-size: 0.7em; font-weight: normal;">(forecast weighted)</span>' : ''}</div>
                                    <div class="pv-scale-total-value" style="color: #6495ed;">${dailyAvg.toFixed(3)}×<span class="pv-scale-total-pct">(${percentChange >= 0 ? '+' : ''}${percentChange.toFixed(1)}%)</span></div>
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
