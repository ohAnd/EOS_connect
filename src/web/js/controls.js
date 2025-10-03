/**
 * Controls Manager for EOS Connect
 * Handles all control-related functionality including override controls, mode changes, and UI interactions
 */

class ControlsManager {
    constructor() {
        this.menuControlEventListener = null;
        this.icons = [
            { icon: "fa-plug-circle-bolt", color: COLOR_MODE_CHARGE_FROM_GRID, title: "Charge from grid" },
            { icon: "fa-lock", color: COLOR_MODE_AVOID_DISCHARGE, title: "Avoid discharge" },
            { icon: "fa-battery-half", color: COLOR_MODE_DISCHARGE_ALLOWED, title: "Discharge allowed" },
            { icon: "fa-charging-station", color: COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST, title: "Avoid discharge due to e-car fast charge" },
            { icon: "fa-charging-station", color: COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV, title: "Discharge allowed during e-car charging in pv mode" },
            { icon: "fa-charging-station", color: COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV, title: "Discharge allowed during e-car charging in min+pv mode" }
        ];
    }

    /**
     * Initialize controls manager
     */
    init() {
        console.log('[ControlsManager] Initialized');
    }

    /**
     * Adjust grid charge power by delta amount
     */
    adjustGridChargePower(delta) {
        const input = document.getElementById("grid_charge_power");
        if (!input) return;

        let newValue = parseFloat(input.value) + delta;
        newValue = Math.max(parseFloat(input.min), Math.min(parseFloat(input.max), newValue));
        input.value = newValue.toFixed(1);
    }

    /**
     * Create and show the override controls menu
     */
    showOverrideMenu(maxChargePower, overrideActive) {
        const buttons = this.icons.map((icon, index) => {
            if (index > 2) return; // without special evcc modes
            const isDisabled = false; // Could be: index === inverter_mode_num;
            
            return `<button id="mode_${index}" class="button" style="
                cursor: ${isDisabled ? 'not-allowed' : 'pointer'}; 
                font-size: 0.8em; padding: 0.15em 0.3em; margin: 10px; 
                color: ${isDisabled ? 'gray' : icon.color}; 
                transition: color 0.3s; 
                background-color: ${isDisabled ? '#333' : 'rgb(58, 58, 58)'}; 
                border-radius: 10px; 
                border: 1px solid ${isDisabled ? 'gray' : icon.color};"
                ${isDisabled ? ' disabled' : ` 
                    onmouseover="this.style.color='white'" 
                    onmouseout="this.style.color='${icon.color}'" 
                    onmousedown="this.style.color='#000000'" 
                    onmouseup="this.style.color='${icon.color}'" 
                    onclick="controlsManager.handleModeChange(${index})"`}>
                <i class="fa-solid ${icon.icon}"></i>
            </button>`;
        }).join('') + (overrideActive ? 
            `<br><span style="font-size: x-small; margin: 40px;"><i>Back To Automatic</i></span><br>
            <button id="mode_auto" class="button" style="
                font-size: 0.8em; padding: 0.15em 0.3em; margin: 10px; 
                color: green; transition: color 0.3s; 
                background-color: rgb(58, 58, 58); 
                border-radius: 10px; border: 1px solid grey; cursor: pointer;"
                onmouseover="this.style.color='white'" 
                onmouseout="this.style.color='green'" 
                onmousedown="this.style.color='#000000'" 
                onmouseup="this.style.color='green'" 
                onclick="controlsManager.handleModeChange('-2')">
                <i class="fa-solid fa-clock-rotate-left"></i>
            </button>` : ''
        );

        const durationEntry = `<select id="duration_time" style="padding: 10px; font-size: 1em; border-radius: 5px; border: 1px solid #ccc; background-color: #444; color: white;">
            ${Array.from({ length: 48 }, (_, i) => {
                const hours = Math.floor((i + 1) / 2);
                const minutes = ((i + 1) % 2) * 30;
                const timeLabel = `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`;
                return `<option value="${timeLabel}" ${hours === 2 && minutes === 0 ? 'selected' : ''}>${timeLabel}</option>`;
            }).join('')}
        </select>`;

        const acChargePower = `<div style="display: flex; justify-content: center; align-items: center; margin-top: 10px; padding-bottom: 10px;">
            <button id="charge-power-decrease" onclick="controlsManager.adjustGridChargePower(-0.1)" style="padding: 10px; font-size: 1em; margin-right: 10px; border-radius: 5px; border: 1px solid #ccc; background-color: #444; color: white; transition: color 0.3s;" 
                onmouseover="this.style.color='gray'" 
                onmouseout="this.style.color='white'" 
                onmousedown="this.style.backgroundColor='lightblue'" 
                onmouseup="this.style.color='white';this.style.backgroundColor='#444'">-</button>
            <input id="grid_charge_power" type="number" step="0.25" min="0.5" max="${maxChargePower.toFixed(1)}" value="${maxChargePower.toFixed(1)}" style="padding: 10px; font-size: 1em; text-align: center; width: 1.4em; border-radius: 5px; border: 1px solid #ccc; background-color: #444; color: white;">
            <button id="charge-power-increase" onclick="controlsManager.adjustGridChargePower(0.1)" style="padding: 10px; font-size: 1em; margin-left: 10px; border-radius: 5px; border: 1px solid #ccc; background-color: #444; color: white; transition: color 0.3s;" 
                onmouseover="this.style.color='gray'" 
                onmouseout="this.style.color='white'" 
                onmousedown="this.style.backgroundColor='lightblue'" 
                onmouseup="this.style.color='white';this.style.backgroundColor='#444'">+</button>
        </div>`;

        // Add passive touch event listeners after overlay is created
        setTimeout(() => {
            const decreaseBtn = document.getElementById('charge-power-decrease');
            const increaseBtn = document.getElementById('charge-power-increase');
            
            if (decreaseBtn) {
                decreaseBtn.addEventListener('touchstart', function() {
                    this.style.backgroundColor = 'lightblue';
                }, { passive: true });
                decreaseBtn.addEventListener('touchend', function() {
                    this.style.color = 'white';
                    this.style.backgroundColor = '#444';
                }, { passive: true });
            }
            
            if (increaseBtn) {
                increaseBtn.addEventListener('touchstart', function() {
                    this.style.backgroundColor = 'lightblue';
                }, { passive: true });
                increaseBtn.addEventListener('touchend', function() {
                    this.style.color = 'white';
                    this.style.backgroundColor = '#444';
                }, { passive: true });
            }
        }, 0);

        const content = `<div style="font-size: 2em;">${buttons}</div>
            <hr style='border-color: rgba(44, 44, 44, 0.596);'>
            Duration <br>
            <div style="display: flex; justify-content: center; align-items: center; margin-top: 10px; padding-bottom: 10px;">${durationEntry}</div>
            <hr style='border-color: rgba(44, 44, 44, 0.596);'>
            Grid Charge Power (kW)<br>${acChargePower}`;

        overlayMenu("Override Current Controls", content);
    }

    /**
     * Handle mode change button clicks
     */
    async handleModeChange(mode) {
        const durationElement = document.getElementById('duration_time');
        const gridChargePowerElement = document.getElementById('grid_charge_power');
        
        if (!durationElement || !gridChargePowerElement) {
            console.error('[ControlsManager] Duration or grid charge power elements not found');
            return;
        }

        const duration = durationElement.value;
        const gridChargePower = gridChargePowerElement.value;
        const controlData = { 
            "mode": mode, 
            "duration": duration, 
            "grid_charge_power": parseFloat(gridChargePower) 
        };

        try {
            console.log('[ControlsManager] Sending mode change:', controlData);
            const result = await dataManager.setOverrideControl(controlData);
            
            console.log('[ControlsManager] Override control set successfully:', result);
            closeOverlayMenu();
            overlayMenu('<span style="color:orange;">Success</span>', "Mode changed successfully.", false);
            
            setTimeout(() => {
                closeOverlayMenu(false);
            }, 2000);
            
        } catch (error) {
            console.error('[ControlsManager] Failed to change mode:', error);
            overlayMenu("Error", `Failed to change mode: ${error.message}`);
            setTimeout(() => {
                closeOverlayMenu(false);
            }, 2000);
        }
    }

    /**
     * Update current controls display
     */
    updateCurrentControls(controlsData) {
        if (!controlsData || !controlsData.current_states) {
            console.warn('[ControlsManager] Invalid controls data provided');
            return;
        }

        const states = controlsData.current_states;
        const overrideActive = states.override_active;
        const overrideEndTime = states.override_end_time;
        const inverterModeText = states.inverter_mode;
        const inverterModeNum = states.inverter_mode_num;

        // Update overall state
        const cleanModeText = inverterModeText.replace("MODE ", "");
        document.getElementById('control_overall').innerHTML = overrideActive ? 
            `<i class="fa-solid fa-triangle-exclamation"></i> ${cleanModeText}` : cleanModeText;

        // Update controls based on override state
        if (overrideActive) {
            this.updateOverrideControls(states, overrideEndTime, inverterModeNum);
        } else {
            this.updateNormalControls(states);
        }

        // Update mode icon and click handler
        this.updateModeIcon(inverterModeNum, overrideActive, controlsData.battery.max_charge_power_dyn);
    }

    /**
     * Update controls when override is active
     */
    updateOverrideControls(states, overrideEndTime, inverterModeNum) {
        const overrideEndFormatted = new Date(overrideEndTime * 1000).toLocaleString(navigator.language, { 
            hour: '2-digit', 
            minute: '2-digit' 
        });

        document.getElementById('control_ac_charge_desc').innerText = "Override Active";
        document.getElementById('control_ac_charge_desc').style.color = "orange";
        document.getElementById('control_ac_charge').innerText = "until " + overrideEndFormatted;
        document.getElementById('control_ac_charge').style.color = "orange";

        if (inverterModeNum === 0) {
            document.getElementById('control_dc_charge_desc').innerText = "AC Charge Power";
            document.getElementById('control_dc_charge').innerText = (states.current_ac_charge_demand / 1000).toFixed(1) + " kW";
        } else if (inverterModeNum === 2) {
            document.getElementById('control_dc_charge_desc').innerText = "DC Charge Power";
            document.getElementById('control_dc_charge').innerText = (states.current_dc_charge_demand / 1000).toFixed(1) + " kW";
        } else {
            document.getElementById('control_dc_charge_desc').innerText = "";
            document.getElementById('control_dc_charge').innerText = "";
        }

        document.getElementById('control_discharge_allowed_desc').innerText = "";
        document.getElementById('control_discharge_allowed').innerText = "";
        document.getElementById('current_controls_box').style.border = "1px solid orange";
    }

    /**
     * Update controls in normal mode
     */
    updateNormalControls(states) {
        document.getElementById('control_ac_charge_desc').innerText = "AC Charge";
        document.getElementById('control_ac_charge_desc').style.color = "";
        document.getElementById('control_ac_charge').innerText = (states.current_ac_charge_demand / 1000).toFixed(1) + " kW";
        document.getElementById('control_ac_charge').style.color = "";
        
        document.getElementById('control_dc_charge_desc').innerText = "DC Charge";
        document.getElementById('control_dc_charge').innerText = (states.current_dc_charge_demand / 1000).toFixed(1) + " kW";
        
        document.getElementById('control_discharge_allowed_desc').innerText = "Discharge allowed";
        document.getElementById('control_discharge_allowed').innerText = states.current_discharge_allowed ? "Yes" : "No";
        
        document.getElementById('current_controls_box').style.border = "";
    }

    /**
     * Update the mode icon and setup click handler
     */
    updateModeIcon(inverterModeNum, overrideActive, maxChargePowerDyn) {
        const iconElement = document.getElementById('current_header_right');
        if (!iconElement) return;

        iconElement.innerHTML = ""; // Clear previous content

        const iconData = this.icons[inverterModeNum] || {};
        const { icon, color, title } = iconData;
        
        iconElement.innerHTML = `<i class="fa-solid ${icon}"></i>`;
        iconElement.style.color = color || "";
        iconElement.title = title || "";

        if (overrideActive) {
            iconElement.innerHTML = '<i style="color:orange;" class="fa-solid fa-triangle-exclamation"></i> ' + iconElement.innerHTML;
        }

        // Setup click handler for override controls
        this.setupOverrideClickHandler(iconElement, maxChargePowerDyn / 1000, overrideActive);
    }

    /**
     * Setup click handler for override controls
     */
    setupOverrideClickHandler(iconElement, maxChargePower, overrideActive) {
        const newListener = () => {
            console.log('[ControlsManager] Override active:', overrideActive, '- Max charge power:', maxChargePower);
            this.showOverrideMenu(maxChargePower, overrideActive);
        };

        // Remove old listener if it exists
        if (this.menuControlEventListener) {
            iconElement.removeEventListener('click', this.menuControlEventListener);
        }

        // Add new listener
        this.menuControlEventListener = newListener;
        iconElement.addEventListener('click', this.menuControlEventListener);
    }

    /**
     * Show control details (left header click)
     */
    showControlDetails() {
        // This could show detailed control information
        console.log('[ControlsManager] Show control details clicked');
        // Implementation depends on what you want to show
    }

    /**
     * Cleanup when shutting down
     */
    cleanup() {
        const iconElement = document.getElementById('current_header_right');
        if (iconElement && this.menuControlEventListener) {
            iconElement.removeEventListener('click', this.menuControlEventListener);
            this.menuControlEventListener = null;
        }
    }
}

// ControlsManager instance is created in main.js during initialization

// Legacy compatibility functions - keep for backward compatibility
function adjustGridChargePower(delta) {
    if (controlsManager) {
        return controlsManager.adjustGridChargePower(delta);
    }
}

function menu_controls_override(icons, ac_charge_max, auto_active) {
    if (controlsManager) {
        return controlsManager.showOverrideMenu(ac_charge_max, auto_active);
    }
}

function handleModeChange(mode) {
    if (controlsManager) {
        return controlsManager.handleModeChange(mode);
    }
}