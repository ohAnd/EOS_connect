/**
 * Controls Manager for EOS Connect
 * Handles all control-related functionality including override controls, mode changes, and UI interactions
 */

class ControlsManager {
    constructor() {
        this.menuControlEventListener = null;
    }

    /**
     * Initialize controls manager
     */
    init() {
        console.log('[ControlsManager] Initialized');
    }

    /**
     * Create and show the override controls menu using modern full-screen overlay
     */
    showOverrideMenuFullScreen(maxChargePower = null, overrideActive = false) {
        let currentModeNum = -1;

        // Use current data if available
        if (typeof data_controls !== 'undefined' && data_controls) {
            if (!maxChargePower) {
                maxChargePower = data_controls.battery?.max_charge_power_dyn ? data_controls.battery.max_charge_power_dyn / 1000 : 5.0;
            }

            // Check multiple ways override could be indicated
            if (data_controls.current_states) {
                overrideActive = data_controls.current_states.override_active === true;
                currentModeNum = data_controls.current_states.inverter_mode_num;
            }
        }

        if (!maxChargePower) {
            maxChargePower = 5.0; // Default fallback
        }

        // Also check global variable as fallback for mode number
        if ((currentModeNum === -1 || currentModeNum === null || currentModeNum === undefined) && typeof inverter_mode_num !== 'undefined') {
            currentModeNum = inverter_mode_num;
        }

        console.log('[ControlsManager] Override menu - maxChargePower:', maxChargePower, 'overrideActive:', overrideActive, 'currentModeNum:', currentModeNum);

        // Safely log data_controls only if it exists
        if (typeof data_controls !== 'undefined' && data_controls) {
            console.log('[ControlsManager] Full data_controls object:', data_controls);
            if (data_controls.current_states) {
                console.log('[ControlsManager] current_states details:', {
                    override_active: data_controls.current_states.override_active,
                    inverter_mode_num: data_controls.current_states.inverter_mode_num,
                    inverter_mode: data_controls.current_states.inverter_mode
                });
            }
        } else {
            console.log('[ControlsManager] data_controls is not available globally');
        }

        const header = `
            <div style="display: flex; align-items: center; gap: 10px;">
                <i class="fas fa-sliders" style="color: #cccccc;"></i>
                <span>Override Current Controls</span>
            </div>
        `;

        const content = `
            <div style="height: calc(100% - 20px); overflow-y: auto; margin-top: 10px; text-align: center;">

                <!-- Duration Selection Section -->
                <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 25px; margin-bottom: 20px; border-left: 4px solid #17a2b8;">
                    <div style="font-size: 1.1em; color: #17a2b8; margin-bottom: 15px; font-weight: bold;">
                        <i class="fas fa-clock" style="margin-right: 10px;"></i>Override Duration<br> <span style="font-size: 0.75em; color: #888; font-weight: normal;">Selected duration will be taken over with mode change</span>
                    </div>
                    
                    <select id="duration_time" style="
                        padding: 12px 20px;
                        font-size: 1em;
                        border-radius: 8px;
                        border: 2px solid #17a2b8;
                        background-color: rgba(58, 58, 58, 0.8);
                        color: white;
                        cursor: pointer;
                        min-width: 150px;
                    ">
                        ${Array.from({ length: 48 }, (_, i) => {
            const hours = Math.floor((i + 1) / 2);
            const minutes = ((i + 1) % 2) * 30;
            const timeLabel = `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`;
            return `<option value="${timeLabel}" ${hours === 2 && minutes === 0 ? 'selected' : ''}>${timeLabel}</option>`;
        }).join('')}
                    </select>
                </div>

                <!-- Grid Charge Power Section (Only for Mode 0) -->
                ${currentModeNum === 0 && overrideActive ? '' : `
                <div id="grid-power-section" style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 25px; border-left: 4px solid ${EOS_CONNECT_ICONS[0].color}; margin-bottom: 15px; ">
                    <div style="font-size: 1.1em; color: ${EOS_CONNECT_ICONS[0].color}; margin-bottom: 15px; font-weight: bold;">
                        <i class="fas fa-bolt" style="margin-right: 10px;"></i>Grid Charge Power (kW)<br> <span style="font-size: 0.75em; color: #888; font-weight: normal;">Mode '${EOS_CONNECT_ICONS[0].title}' Only</span>
                    </div>
                    
                    <div style="display: flex; justify-content: center; align-items: center; gap: 15px; flex-wrap: wrap;">
                        <button id="charge-power-decrease" onclick="controlsManager.adjustGridChargePowerFullScreen(-0.1)" 
                            style="
                                padding: 12px 18px;
                                font-size: 1.2em;
                                background-color: rgba(58, 58, 58, 0.8);
                                color: white;
                                border: 2px solid #666;
                                border-radius: 8px;
                                cursor: pointer;
                                transition: all 0.3s ease;
                                min-width: 50px;
                            "
                            onmouseover="this.style.backgroundColor='rgba(220, 53, 69, 0.2)'; this.style.borderColor='${EOS_CONNECT_ICONS[0].color}'"
                            onmouseout="this.style.backgroundColor='rgba(58, 58, 58, 0.8)'; this.style.borderColor='#666'">
                            <i class="fas fa-minus"></i>
                        </button>
                        
                        <input id="grid_charge_power" type="number" step="0.25" min="0.5" max="${maxChargePower.toFixed(1)}" value="${maxChargePower.toFixed(1)}" 
                            style="
                                padding: 12px;
                                font-size: 1.1em;
                                text-align: center;
                                width: 120px;
                                border-radius: 8px;
                                border: 2px solid ${EOS_CONNECT_ICONS[0].color};
                                background-color: rgba(58, 58, 58, 0.8);
                                color: white;
                            ">
                        
                        <button id="charge-power-increase" onclick="controlsManager.adjustGridChargePowerFullScreen(0.1)" 
                            style="
                                padding: 12px 18px;
                                font-size: 1.2em;
                                background-color: rgba(58, 58, 58, 0.8);
                                color: white;
                                border: 2px solid #666;
                                border-radius: 8px;
                                cursor: pointer;
                                transition: all 0.3s ease;
                                min-width: 50px;
                            "
                            onmouseover="this.style.backgroundColor='rgba(220, 53, 69, 0.2)'; this.style.borderColor='#dc3545'"
                            onmouseout="this.style.backgroundColor='rgba(58, 58, 58, 0.8)'; this.style.borderColor='#666'">
                            <i class="fas fa-plus"></i>
                        </button>
                    </div>
                    
                    <div style="margin-top: 10px; font-size: 0.75em; color: #888;">
                        Range: 0.5 - ${maxChargePower.toFixed(1)} kW
                    </div>
                </div>
                `}

                <div style="margin-top: auto;">
                </div>

                <!-- Mode Selection Section -->
                <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 25px; margin-bottom: 20px; border-left: 4px solid lightgray;">
                    <div style="font-size: 1.2em; margin-bottom: 20px; font-weight: bold;">
                        <i class="fas fa-cog" style="margin-right: 10px;"></i>Battery Mode Selection
                    </div>
                    
                    <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; margin-bottom: 20px;">
                        ${EOS_CONNECT_ICONS.slice(0, 3).map((icon, index) => {
            // Mode numbers in data are 1-based (1,2,3) but our array is 0-based (0,1,2)
            // So we need to compare (currentModeNum - 1) with index, OR currentModeNum with (index + 1)
            // const isCurrentMode = overrideActive && (currentModeNum === (index + 1));
            const isCurrentMode = (currentModeNum === (index));
            const isDisabled = isCurrentMode;
            const buttonColor = isDisabled ? '#666' : icon.color;
            const bgColor = isDisabled ? 'rgba(51, 51, 51, 0.8)' : 'rgba(58, 58, 58, 0.8)';
            console.log(`[ControlsManager] Mode ${index} - isCurrentMode: ${isCurrentMode}, isDisabled: ${isDisabled}, currentModeNum: ${currentModeNum}, bgcolor: ${bgColor}`);
            // const borderColor = isDisabled ? '#444' : icon.color;
            const borderColor = icon.color; // show border color if Disabled it will shown with 0.5 opacity
            const cursor = isDisabled ? 'not-allowed' : 'pointer';

            return `
                            <button id="mode_${index}" ${isDisabled ? '' : `onclick="controlsManager.handleModeChangeFullScreen(${index})"`}
                                ${isDisabled ? 'disabled' : ''} 
                                style="
                                    padding: 20px 25px;
                                    font-size: 1.5em;
                                    color: ${buttonColor};
                                    background-color: ${bgColor};
                                    border: 2px solid ${borderColor};
                                    border-radius: 12px;
                                    cursor: ${cursor};
                                    transition: all 0.3s ease;
                                    min-width: 175px;
                                    display: flex;
                                    flex-direction: column;
                                    align-items: center;
                                    gap: 8px;
                                    opacity: ${isDisabled ? '0.5' : '1'};
                                "
                                ${!isDisabled ? `
                                    onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'; this.style.transform='translateY(-2px)'"
                                    onmouseout="this.style.backgroundColor='${bgColor}'; this.style.transform='translateY(0)'"
                                ` : ''}>
                                <i class="fa-solid ${icon.icon}"></i>
                                <span style="font-size: 0.6em; color: ${isDisabled ? '#888' : '#ccc'};">
                                    ${icon.title || 'Mode ' + (index)}
                                    <!-- ${isCurrentMode ? ' ...' : ''} -->
                                </span>
                            </button>
                        `;
        }).join('')}
                    </div>
                    
                </div>
                
                ${overrideActive ? `
                    <!-- Back to Automatic Section -->
                    <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 25px; margin-bottom: 20px; border-left: 4px solid #28a745;">
                        <div style="font-size: 1.1em; color: #28a745; margin-bottom: 15px; font-weight: bold;">
                            <i class="fas fa-undo" style="margin-right: 10px;"></i>Return to Automatic Mode
                        </div>
                        <div style="margin-bottom: 15px; font-size: 0.9em; color: #888;">
                            Override is currently active (Mode ${EOS_CONNECT_ICONS[currentModeNum].title}). Click to cancel the override and return to automatic optimization mode.
                        </div>
                        <button id="mode_auto" onclick="controlsManager.handleModeChangeFullScreen('-2')" 
                            style="
                                padding: 18px 30px;
                                font-size: 1.1em;
                                color: #28a745;
                                background-color: rgba(40, 167, 69, 0.1);
                                border: 2px solid #28a745;
                                border-radius: 10px;
                                cursor: pointer;
                                transition: all 0.3s ease;
                                display: inline-flex;
                                align-items: center;
                                gap: 12px;
                                font-weight: bold;
                            "
                            onmouseover="this.style.backgroundColor='rgba(40, 167, 69, 0.2)'; this.style.transform='translateY(-2px)'; this.style.boxShadow='0 4px 12px rgba(40, 167, 69, 0.3)'"
                            onmouseout="this.style.backgroundColor='rgba(40, 167, 69, 0.1)'; this.style.transform='translateY(0)'; this.style.boxShadow='none'">
                            <i class="fa-solid fa-clock-rotate-left"></i>
                            <span>Back to Automatic</span>
                        </button>
                    </div>
                ` : ''}
                
                <!-- Debug Section
                <div style="background-color: rgba(0,0,0,0.2); border-radius: 8px; padding: 15px; margin-bottom: 20px; border-left: 4px solid #666;">
                    <div style="font-size: 0.9em; color: #888; margin-bottom: 10px;">
                        <strong>Debug Info:</strong><br>
                        Override Active: ${overrideActive ? 'YES' : 'NO'}<br>
                        Current Mode: ${currentModeNum}<br>
                        Max Charge Power: ${maxChargePower} kW
                    </div>
                </div>
                -->
                
                


                

                
            </div>
        `;

        showFullScreenOverlay(header, content);

        // Add mode-specific control logic and touch event listeners
        setTimeout(() => {
            // Add click handlers for mode buttons to show/hide relevant controls
            EOS_CONNECT_ICONS.slice(0, 3).forEach((icon, index) => {
                const button = document.getElementById(`mode_${index}`);
                if (button && !button.disabled) {
                    const originalOnClick = button.getAttribute('onclick');
                    button.setAttribute('onclick', `controlsManager.selectModeForOverride(${index}); ${originalOnClick}`);
                }
            });

            // Initialize with mode 0 (grid charge) selected by default
            // this.selectModeForOverride(0);

            // Add touch event listeners for power adjustment buttons
            const decreaseBtn = document.getElementById('charge-power-decrease');
            const increaseBtn = document.getElementById('charge-power-increase');

            [decreaseBtn, increaseBtn].forEach(btn => {
                if (btn) {
                    btn.addEventListener('touchstart', function () {
                        this.style.backgroundColor = 'rgba(220, 53, 69, 0.3)';
                    }, { passive: true });
                    btn.addEventListener('touchend', function () {
                        this.style.backgroundColor = 'rgba(58, 58, 58, 0.8)';
                    }, { passive: true });
                }
            });
        }, 100);
    }

    /**
     * Select mode for override and show/hide relevant controls
     */
    selectModeForOverride(mode) {
        // Highlight selected mode button
        EOS_CONNECT_ICONS.slice(0, 3).forEach((icon, index) => {
            const button = document.getElementById(`mode_${index}`);
            if (button) {
                if (index === mode) {
                    // Highlight selected mode
                    button.style.backgroundColor = 'rgba(255, 193, 7, 0.2)';
                    button.style.borderColor = '#ffc107';
                    button.style.boxShadow = '0 0 10px rgba(255, 193, 7, 0.3)';
                } else if (!button.disabled) {
                    // Reset non-selected modes
                    button.style.backgroundColor = 'rgba(58, 58, 58, 0.8)';
                    button.style.borderColor = icon.color;
                    button.style.boxShadow = 'none';
                }
            }
        });

        // Show/hide grid charge power section based on mode
        const gridPowerSection = document.getElementById('grid-power-section');
        if (gridPowerSection) {
            if (mode === 0) {
                // Mode 0 (Grid Charge) - show power controls
                gridPowerSection.style.display = 'block';
            } else {
                // Mode 1 & 2 (Avoid Discharge, Allow Discharge) - hide power controls
                gridPowerSection.style.display = 'none';
            }
        }

        // Store selected mode for later use
        this.selectedOverrideMode = mode;
    }

    /**
     * Handle mode change for full-screen overlay
     */
    async handleModeChangeFullScreen(mode) {
        const durationElement = document.getElementById('duration_time');
        // Only get gridChargePowerElement if mode is 0 (Grid Charge)
        // const gridChargePowerElement = (mode === 0 || mode === "0") ? document.getElementById('grid_charge_power') : null;
        const gridChargePowerElement = document.getElementById('grid_charge_power');

        // Duration is always required, grid_charge_power only for mode 0
        if (!durationElement || (mode === 0 || mode === "0") && !gridChargePowerElement) {
            console.error('[ControlsManager] Duration or grid charge power elements not found');
            return;
        }

        const duration = durationElement.value;
        const gridChargePower = gridChargePowerElement ? gridChargePowerElement.value : 0.5;
        const controlData = {
            mode: mode,
            duration: duration,
            grid_charge_power: parseFloat(gridChargePower)
        };

        // Only add grid_charge_power for mode 0
        if (gridChargePowerElement) {
            controlData.grid_charge_power = parseFloat(gridChargePowerElement.value);
        }

        console.log('[ControlsManager] Sending override control data:', controlData);

        try {
            const result = await dataManager.setOverrideControl(controlData);
            console.log('[ControlsManager] Override control set successfully:', result);

            // Close the overlay after successful operation
            closeFullScreenOverlay(2500);

            // Refresh data to show updated state
            if (typeof init === 'function') {
                setTimeout(init, 500); // Small delay to allow server to process
            }
        } catch (error) {
            console.error('[ControlsManager] Error setting override control:', error);
            alert('Failed to set override control: ' + error.message);
        }
    }

    /**
     * Adjust grid charge power for full-screen overlay
     */
    adjustGridChargePowerFullScreen(delta) {
        const input = document.getElementById('grid_charge_power');
        if (!input) return;

        const currentValue = parseFloat(input.value) || 0;
        const maxValue = parseFloat(input.max) || 10;
        const minValue = parseFloat(input.min) || 0.5;

        const newValue = Math.max(minValue, Math.min(maxValue, currentValue + delta));
        input.value = newValue.toFixed(1);
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

        if (controlsData.used_optimization_source === "evopt") {
            document.getElementById("experimental-banner").style.display = "flex";
        } else {
            document.getElementById("experimental-banner").style.display = "none";
        }
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

        const iconData = EOS_CONNECT_ICONS[inverterModeNum] || {};
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
            // this.showOverrideMenu(maxChargePower, overrideActive);
            this.showOverrideMenuFullScreen(maxChargePower, overrideActive);
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
