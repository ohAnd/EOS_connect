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
            showMainMenu(data_controls["eos_connect_version"]);
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
        const { request: data_request, response: data_response, controls: data_controls } = allData;

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
            return;
        }

        // Update or create chart using chartManager
        if (chartManager.chartInstance) {
            chartManager.updateChart(data_request, data_response, data_controls);
            document.getElementById('overlay').style.display = 'none';
        } else {
            chartManager.createChart(data_request, data_response, data_controls);
            document.getElementById('overlay').style.display = 'none';
        }

        // Update all displays
        showStatistics(data_request, data_response);
        showSchedule(data_request, data_response, data_controls);
        setBatteryChargingData(data_response);
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
    }
}

// Initialize and start polling
init();
setInterval(init, 1000);
