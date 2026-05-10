/**
 * Main application initialization and coordination
 * This should contain ALL the initialization logic from the legacy file
 */
/*
* TIME HANDLING STRATEGY
* ======================
* 
* SERVER TIME: Used for all data processing and calculations (from data_response["timestamp"])
* USER TIME: Used for display - chart labels and schedule show in user's local timezone
* 
* This allows users in different timezones to see when events happen in their local time
* while maintaining consistent server-based data processing.
*/
if (isTestMode) {
    document.body.style.backgroundColor = 'lightgreen';
}

let max_charge_power_w = 0;
let inverter_mode_num = -1;
let chartInstance = null;
let menuControlEventListener = null;
let data_controls = null; // Global data_controls for use across modules
let localization = {
    "currency": "EUR*",
    "currency_symbol": "\u20ac*",
    "currency_minor_unit": "ct*"
}

// Set up chart resize handler
window.addEventListener('resize', () => {
    if (chartManager) {
        chartManager.updateLegendVisibility();
    }
});

function parseAlertMeta(rawMessage) {
    const message = String(rawMessage || '');
    const configMatch = message.match(/\|\s*Config:\s*([^|]+)/i);
    const hasActionRequired = /\|\s*ACTION REQUIRED/i.test(message);
    const cleaned = message
        .replace(/\|\s*Config:\s*[^|]+/gi, '')
        .replace(/\|\s*ACTION REQUIRED/gi, '')
        .trim();

    return {
        text: cleaned,
        configLink: configMatch ? configMatch[1].trim() : null,
        actionRequired: hasActionRequired,
    };
}

function dedupeAlerts(alerts) {
    const grouped = new Map();

    for (const alert of alerts) {
        const meta = parseAlertMeta(alert.message);
        const key = `${alert.level}|${meta.text}`;

        if (!grouped.has(key)) {
            grouped.set(key, {
                ...alert,
                message: meta.text,
                configLink: meta.configLink,
                actionRequired: meta.actionRequired,
                occurrences: 1,
                firstTimestamp: alert.timestamp,
                lastTimestamp: alert.timestamp,
            });
            continue;
        }

        const current = grouped.get(key);
        current.occurrences += 1;
        current.actionRequired = current.actionRequired || meta.actionRequired;
        if (!current.configLink && meta.configLink) {
            current.configLink = meta.configLink;
        }

        if (new Date(alert.timestamp) < new Date(current.firstTimestamp)) {
            current.firstTimestamp = alert.timestamp;
        }
        if (new Date(alert.timestamp) > new Date(current.lastTimestamp)) {
            current.lastTimestamp = alert.timestamp;
        }
    }

    return Array.from(grouped.values()).sort((a, b) => {
        const aRequired = a.actionRequired ? 1 : 0;
        const bRequired = b.actionRequired ? 1 : 0;
        if (aRequired !== bRequired) return bRequired - aRequired;
        return new Date(b.lastTimestamp) - new Date(a.lastTimestamp);
    });
}

function renderAlertSection(title, iconClass, titleColor, cardColor, borderColor, alerts) {
    if (!alerts.length) return '';

    const maxVisible = 6;
    const visibleAlerts = alerts.slice(0, maxVisible);
    let html = '';

    html += '<div style="margin-bottom: 10px;">';
    html += `<span style="color: ${titleColor}; font-weight: 700; font-size: 0.9em;">`;
    html += `<i class="fas ${iconClass}" style="margin-right: 6px;"></i>${escapeHtml(title)} (${alerts.length})`;
    html += '</span>';
    html += '</div>';

    for (const alert of visibleAlerts) {
        const message = escapeHtml(String(alert.message || '').substring(0, 220));
        const lastTime = new Date(alert.lastTimestamp || alert.timestamp).toLocaleTimeString();
        const repeatInfo = alert.occurrences > 1
            ? `<div style="color:#bdbdbd; font-size:0.75em; margin-top:4px;"><i class="fas fa-repeat" style="margin-right:5px;"></i>${alert.occurrences} occurrences</div>`
            : '';
        const actionBadge = alert.actionRequired
            ? '<span style="display:inline-block; background: rgba(255, 193, 7, 0.18); color:#ffd54f; border:1px solid rgba(255, 193, 7, 0.4); border-radius:6px; padding:2px 7px; font-size:0.72em; margin-top:6px;">ACTION REQUIRED</span>'
            : '';

        const linkTarget = alert.configLink
            ? `#${String(alert.configLink).replace(/^#/, '')}`
            : '#configOverlay';
        const actionLink = alert.actionRequired || alert.configLink
            ? `<div style="margin-top:8px;"><a href="${escapeHtml(linkTarget)}" onclick="if(typeof showConfigurationMenu === 'function'){ showConfigurationMenu(); } return true;" style="color:#4a9eff; text-decoration:none; font-size:0.82em;"><i class="fas fa-sliders-h" style="margin-right:5px;"></i>Open Configuration</a></div>`
            : '';

        html += `<div style="background:${cardColor}; border-left:3px solid ${borderColor}; padding:10px 12px; margin-bottom:8px; border-radius:4px;">`;
        html += `<div style="color:${titleColor}; font-weight:600;">${message}</div>`;
        html += `<div style="color:#9e9e9e; font-size:0.75em; margin-top:4px;"><i class="fas fa-clock" style="margin-right:5px;"></i>Last seen ${escapeHtml(lastTime)}</div>`;
        html += repeatInfo;
        html += actionBadge;
        html += actionLink;
        html += '</div>';
    }

    if (alerts.length > maxVisible) {
        html += `<div style="color:#bdbdbd; font-size:0.78em; margin-bottom:10px;"><i class="fas fa-list" style="margin-right:5px;"></i>${alerts.length - maxVisible} more messages hidden to keep startup view readable</div>`;
    }

    return html;
}

// Display startup errors in the errors panel
async function displayStartupErrors(data_response) {
    try {
        const response = await fetch('/logs/alerts?startup_only=1');
        if (!response.ok) throw new Error('Failed to fetch alerts');

        const alertsData = await response.json();
        const allAlerts = alertsData.alerts || [];

        const panel = document.getElementById('startup-errors-panel');
        const list = document.getElementById('startup-errors-list');

        if (!panel || !list) {
            console.warn('[displayStartupErrors] Panel or list not found');
            return false;
        }

        if (allAlerts.length === 0) {
            panel.style.display = 'none';
            return false;
        }

        const deduped = dedupeAlerts(allAlerts);
        const critical = deduped.filter(a => a.level === 'CRITICAL');
        const errors = deduped.filter(a => a.level === 'ERROR');
        const warnings = deduped.filter(a => a.level === 'WARNING');
        const actionCount = deduped.filter(a => a.actionRequired).length;
        const lastUpdated = new Date(alertsData.timestamp || Date.now()).toLocaleTimeString();

        let html = '';
        html += '<div style="margin-bottom:10px; color:#cfcfcf; font-size:0.82em;">';
        html += `<i class="fas fa-info-circle" style="margin-right:6px;"></i>${allAlerts.length} raw events, ${deduped.length} unique issues`;
        html += ` &middot; updated ${escapeHtml(lastUpdated)}`;
        if (actionCount > 0) {
            html += ` &middot; <span style="color:#ffd54f;"><i class="fas fa-bell" style="margin-right:4px;"></i>${actionCount} require action</span>`;
        }
        html += '</div>';

        html += renderAlertSection(
            'Critical',
            'fa-fire',
            '#ff6b6b',
            'rgba(255, 23, 68, 0.2)',
            '#ff1744',
            critical
        );

        if (critical.length && (errors.length || warnings.length)) {
            html += '<div style="margin: 8px 0; border-top: 1px solid rgba(255, 255, 255, 0.1);"></div>';
        }

        html += renderAlertSection(
            'Errors',
            'fa-exclamation-circle',
            '#ff8a80',
            'rgba(211, 47, 47, 0.17)',
            '#d32f2f',
            errors
        );

        if (warnings.length && (critical.length || errors.length)) {
            html += '<div style="margin: 8px 0; border-top: 1px solid rgba(255, 255, 255, 0.1);"></div>';
        }

        html += renderAlertSection(
            'Warnings',
            'fa-exclamation-triangle',
            '#ffd54f',
            'rgba(255, 193, 7, 0.12)',
            '#ffc107',
            warnings
        );

        list.innerHTML = html;
        panel.style.display = 'block';
        return true;

    } catch (err) {
        console.error('[displayStartupErrors] Caught error:', err);
        const panel = document.getElementById('startup-errors-panel');
        if (panel) panel.style.display = 'none';
        return false;
    }
}

// Helper function to escape HTML in error messages
function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

// Use handlingErrorInResponse from data.js
function handlingErrorInResponse(data_response) {
    if (dataManager.hasErrorInResponse(data_response)) {
        const errorInfo = dataManager.getErrorInfo(data_response);

        const overlay = document.getElementById('overlay');
        const waitingText = document.getElementById('waiting_text');
        const errorText = document.getElementById('waiting_error_text');

        if (overlay) overlay.style.display = 'flex';
        if (waitingText) waitingText.innerText = errorInfo.title;
        if (errorText) errorText.innerText = errorInfo.message;

        return true;
    }
    return false;
}

async function showCurrentData() {
    //console.log("------- showCurrentControls -------");
    data_controls = await dataManager.fetchCurrentControls(currentTestScenario);
    
    // Display startup errors from /logs/alerts endpoint (live updates)
    await displayStartupErrors(data_controls);
    
    showCarChargingData(data_controls);

    // Use controlsManager to update controls (check if it exists first)
    if (typeof controlsManager !== 'undefined' && controlsManager.updateCurrentControls) {
        controlsManager.updateCurrentControls(data_controls);
    }

    // battery and version display
    document.getElementById('battery_soc').innerText = data_controls["battery"]["soc"] + " %";
    document.getElementById('battery_icon_main').innerHTML = getBatteryIcon(data_controls["battery"]["soc"]);
    document.getElementById('current_max_charge_dyn').innerHTML = "<i>" + (data_controls["battery"]["max_charge_power_dyn"] / 1000).toFixed(2) + " kW</i>";
    document.getElementById('battery_usable_capacity').innerHTML = '<i class="fa-solid fa-database"></i> ' + (data_controls["battery"]["usable_capacity"] / 1000).toFixed(1) + ' <span style="font-size: 0.6em;">kWh</span>';
    document.getElementById('battery_usable_capacity').title = "usable capacity: " + (data_controls["battery"]["usable_capacity"] / 1000).toFixed(1) + " kWh";

    // Add click events for battery overview if not already present
    const batterySoc = document.getElementById('battery_soc');
    const batteryUsable = document.getElementById('battery_usable_capacity');
    const batteryIcon = document.getElementById('battery_icon_main');

    if (batterySoc && !batterySoc.onclick) {
        batterySoc.onclick = () => batteryManager.showBatteryOverview();
        batterySoc.title = "Click to open Battery Overview";
    }
    if (batteryUsable && !batteryUsable.onclick) {
        batteryUsable.onclick = () => batteryManager.showBatteryOverview();
        // Keep existing title but append info
        const currentTitle = batteryUsable.title;
        if (!currentTitle.includes("Click")) {
            batteryUsable.title = currentTitle + " - Click to open Battery Overview";
        }
    }
    if (batteryIcon && !batteryIcon.onclick) {
        batteryIcon.onclick = () => batteryManager.showBatteryOverview();
        batteryIcon.style.cursor = 'pointer';
        batteryIcon.title = "Click to open Battery Overview";
    }

    // timestamp and version
    const timestamp_last_run = new Date(data_controls.state.last_response_timestamp);
    const timestamp_next_run = new Date(data_controls.state.next_run);
    const timestamp_last_run_formatted = timestamp_last_run.toLocaleString(navigator.language, {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
    document.getElementById('timestamp_last_run').innerText = timestamp_last_run_formatted;
    document.getElementById('timestamp_last_run').title = "last run";
    let time_to_next_run = Math.floor((timestamp_next_run - new Date()) / 1000);
    let minutes = Math.floor(Math.abs(time_to_next_run) / 60);
    let seconds = Math.abs(time_to_next_run % 60);
    if (time_to_next_run < 0) {
        document.getElementById('timestamp_next_run').style.color = "lightgreen";
        document.getElementById('timestamp_next_run').title = "current optimization running for " + minutes.toString().padStart(2, '0') + " min and " + seconds.toString().padStart(2, '0') + " sec";
    } else {
        document.getElementById('timestamp_next_run').style.color = "orange";
        document.getElementById('timestamp_next_run').title = "next optimization run in " + minutes.toString().padStart(2, '0') + " min and " + seconds.toString().padStart(2, '0') + " sec";
    }
    document.getElementById('timestamp_next_run').innerText = minutes.toString().padStart(2, '0') + ":" + seconds.toString().padStart(2, '0') + " min";

    // display current eos connect version
    document.getElementById('version_overlay').innerText = "EOS connect version: " + data_controls["eos_connect_version"];

    const menuElement = document.getElementById('current_header_left');

    // Only update menu element if it doesn't have the correct icon already
    const expectedIcon = '<i class="fa-solid fa-bars" style="color: #cccccc;"></i>';
    const currentIcon = menuElement.querySelector('i.fa-solid.fa-bars');

    if (!currentIcon || currentIcon.outerHTML !== expectedIcon) {
        // Preserve any existing notification dot
        const existingDot = menuElement.querySelector('.notification-dot');

        // Update the icon
        menuElement.innerHTML = expectedIcon;
        menuElement.title = "Menu";

        // Restore notification dot if it existed
        if (existingDot) {
            menuElement.appendChild(existingDot);
        }

        // Remove any existing event listeners by cloning the element
        const newMenuElement = menuElement.cloneNode(true);
        menuElement.parentNode.replaceChild(newMenuElement, menuElement);

        // Add single event listener
        newMenuElement.addEventListener('click', function () {
            showMainMenu(data_controls["eos_connect_version"], data_controls["used_optimization_source"], data_controls["used_time_frame_base"]);
        });

        console.log('[Main] Updated menu element and preserved notification dot');
    }
}

// Use manager functions for statistics and schedule

// function to observe changed values of doc elements from class "valueChange" and animate the change
Array.from(document.getElementsByClassName("valueChange")).forEach(function (element) {
    const observer = new MutationObserver(function (mutationsList, observer) {
        const elem = mutationsList[0].target;
        // elem.classList.add("animateValue");
        elem.style.color = "black"; //"#2196f3"; //lightgreen
        //elem.style.fontSize = "95%";
        setTimeout(function () {
            elem.style.color = "inherit";// "#eee";
            //elem.style.fontSize = "100%";
        }, 1000);

    });
    observer.observe(element, { characterData: false, childList: true, attributes: false });
});

// Updated init function to use dataManager
async function init() {
    try {
        // Initialize managers if not already done
        if (!dataManager) {
            dataManager = new DataManager();
        }
        if (!controlsManager) {
            controlsManager = new ControlsManager();
        }
        if (!scheduleManager) {
            scheduleManager = new ScheduleManager();
        }
        if (!statisticsManager) {
            statisticsManager = new StatisticsManager();
        }
        if (!chartManager) {
            chartManager = new ChartManager();
        }
        if (!evccManager) {
            evccManager = new EVCCManager();
        }
        if (!batteryManager) {
            batteryManager = new BatteryManager();
        }
        if (!loggingManager) {
            loggingManager = new LoggingManager();
            // Initialize logging manager with slight delay to ensure DOM is ready
            setTimeout(() => {
                loggingManager.init();
            }, 1000);
        }

        // Fetch all data using the dataManager
        const allData = await dataManager.fetchAllData(isTestMode, currentTestScenario);
        const { request: data_request, response: data_response, controls: data_controls, priceInfo: data_priceInfo } = allData;

        // Extract max_charge_power_w from request data
        max_charge_power_w = data_request["pv_akku"] && data_request["pv_akku"].hasOwnProperty("max_ladeleistung_w")
            ? data_request["pv_akku"]["max_ladeleistung_w"]
            : data_request["pv_akku"] ? data_request["pv_akku"]["max_charge_power_w"] : 0;

        // localization settings from server
        localization["currency"] = data_controls["localization"]["currency"] || "EUR*";
        localization["currency_symbol"] = data_controls["localization"]["currency_symbol"] || "\u20ac*";
        localization["currency_minor_unit"] = data_controls["localization"]["currency_minor_unit"] || "ct*";

        // Initialize controls manager if not done yet (check if it exists first)
        if (typeof controlsManager !== 'undefined' && !controlsManager.initialized) {
            controlsManager.init();
            controlsManager.initialized = true;
        }

        // Show current data - THIS NEEDS TO BE CALLED EVERY TIME TO UPDATE TIMESTAMPS
        await showCurrentData();

        // Handle errors in response
        if (handlingErrorInResponse(data_response)) {
            // Even on error, check if setup wizard should be shown (fresh install)
            if (typeof checkWizardStatus === "function") {
                checkWizardStatus();
            }
            return;
        }

        // Update or create chart using chartManager
        if (chartManager.chartInstance) {
            chartManager.updateChart(data_request, data_response, data_controls, data_priceInfo);
            document.getElementById('overlay').style.display = 'none';
        } else {
            chartManager.createChart(data_request, data_response, data_controls, data_priceInfo);
            document.getElementById('overlay').style.display = 'none';
        }

        // Check if setup wizard should be shown (first launch)
        if (typeof checkWizardStatus === "function") {
            checkWizardStatus();
        }

        // Update all displays
        showStatistics(data_request, data_response, data_controls);
        showSchedule(data_request, data_response, data_controls, data_priceInfo);
        setBatteryChargingData(data_response, data_controls);
        chartManager.updateLegendVisibility();

    } catch (error) {
        console.error('[EOS Connect] Error during initialization:', error);

        // Show error in overlay
        const overlay = document.getElementById('overlay');
        const waitingText = document.getElementById('waiting_text');
        const errorText = document.getElementById('waiting_error_text');

        if (overlay) overlay.style.display = 'flex';
        if (waitingText) waitingText.innerText = "Connection Error";
        if (errorText) errorText.innerText = error.message;

        // Even on error, check if setup wizard should be shown (fresh install)
        if (typeof checkWizardStatus === "function") {
            checkWizardStatus();
        }
    }
}

// Initialize and start polling
init();
setInterval(init, 1000);

// Show the restart-required hint on the startup overlay when config changes are pending.
(async function _checkStartupRestartHint() {
    try {
        const res = await fetch("api/config/restart-required");
        if (!res.ok) return;
        const data = await res.json();
        if (data.fields && data.fields.length > 0) {
            const hint = document.getElementById("startup-restart-hint");
            if (hint) hint.style.display = "block";
        }
    } catch { /* non-critical */ }
})();
