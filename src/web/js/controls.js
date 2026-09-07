/**
 * Controls Manager for EOS Connect
 * Handles all control-related functionality including override controls, mode changes, and UI interactions
 */

class ControlsManager {
    constructor() {
        this.menuControlEventListener = null;
        this.toastContainer = null;
        // The dropdown menu reads this to decide whether to offer a Managed Loads
        // entry, and it can be opened before the first poll has landed.
        this.managedLoads = [];
    }

    /**
     * Initialize controls manager
     */
    init() {
        console.log('[ControlsManager] Initialized');
        this.createToastContainer();
    }

    /**
     * Create toast notification container if it doesn't exist
     */
    createToastContainer() {
        if (!this.toastContainer) {
            this.toastContainer = document.createElement('div');
            this.toastContainer.id = 'toast-container';
            this.toastContainer.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 10002;
                display: flex;
                flex-direction: column;
                gap: 10px;
                pointer-events: none;
            `;
            document.body.appendChild(this.toastContainer);
            console.log('[ControlsManager] Toast container created');
        }
    }

    /**
     * Show a toast notification
     * @param {string} message - The message to display
     * @param {string} type - 'info', 'success', 'warning', or 'error'
     * @param {number} duration - Duration in ms before auto-dismiss (0 = no auto-dismiss)
     */
    showToast(message, type = 'info', duration = 3000) {
        this.createToastContainer();

        const toast = document.createElement('div');
        const typeStyles = {
            info: { bg: 'rgba(59, 59, 59, 0.99)', border: '#1aa1f3', icon: 'fa-circle-info', color: '#1aa1f3' },
            success: { bg: 'rgba(59, 59, 59, 0.99)', border: '#28a745', icon: 'fa-check-circle', color: '#28a745' },
            warning: { bg: 'rgba(59, 59, 59, 0.99)', border: '#ffc107', icon: 'fa-exclamation-circle', color: '#ffc107' },
            error: { bg: 'rgba(59, 59, 59, 0.99)', border: '#dc3545', icon: 'fa-exclamation-triangle', color: '#dc3545' }
        };

        const style = typeStyles[type] || typeStyles.info;

        toast.style.cssText = `
            display: flex;
            align-items: center;
            gap: 12px;
            background-color: ${style.bg};
            border: 2px solid ${style.border};
            border-radius: 8px;
            padding: 14px 18px;
            color: #e0e0e0;
            font-size: 0.95em;
            font-weight: 500;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            pointer-events: auto;
            animation: slideIn 0.3s ease-out;
            max-width: 350px;
            word-wrap: break-word;
            opacity: 0.9;
        `;

        toast.innerHTML = `
            <i class="fas ${style.icon}" style="color: ${style.color}; flex-shrink: 0;"></i>
            <span>${message}</span>
            <button style="
                background: none;
                border: none;
                color: #999;
                cursor: pointer;
                font-size: 1.1em;
                padding: 0;
                margin-left: 8px;
                flex-shrink: 0;
                transition: color 0.2s;
            " onmouseover="this.style.color='#e0e0e0'" onmouseout="this.style.color='#999'" onclick="this.parentElement.remove()">
                ✕
            </button>
        `;

        this.toastContainer.appendChild(toast);
        console.log(`[ControlsManager] Toast shown: ${message}`);

        // Auto-dismiss after 5 seconds
        if (duration > 0) {
            setTimeout(() => {
                if (toast.parentElement) {
                    toast.style.animation = 'slideOut 0.3s ease-out forwards';
                    setTimeout(() => toast.remove(), 300);
                }
            }, 5000);
        }
    }

    /**
     * Show loading modal overlay
     */
    showLoadingModal() {
        let loadingModal = document.getElementById('loading-modal');
        if (!loadingModal) {
            loadingModal = document.createElement('div');
            loadingModal.id = 'loading-modal';
            loadingModal.innerHTML = `
                <div class="loading-modal-content">
                    <div class="spinner"></div>
                    <div class="loading-text">Applying override...</div>
                </div>
            `;
            document.body.appendChild(loadingModal);
        }
        loadingModal.classList.add('show');
    }

    /**
     * Hide loading modal overlay
     */
    hideLoadingModal() {
        const loadingModal = document.getElementById('loading-modal');
        if (loadingModal) {
            loadingModal.classList.remove('show');
        }
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

                <div style="margin-top: auto;">
                </div>

                <!-- Mode Selection Section -->
                <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 25px; margin-bottom: 20px; border-left: 4px solid lightgray;">
                    <div style="font-size: 1.2em; margin-bottom: 20px; font-weight: bold;">
                        <i class="fas fa-cog" style="margin-right: 10px;"></i>Battery Mode Selection
                    </div>
                    
                    <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; margin-bottom: 20px;">
                        ${EOS_CONNECT_ICONS.slice(0, 3).map((icon, index) => {
            // Identify if this is the currently active mode
            const isCurrentMode = (currentModeNum === (index));
            // All buttons are now enabled - users can select any mode including the current one
            const buttonColor = icon.color;
            const bgColor = 'rgba(58, 58, 58, 0.8)';
            // Add subtle glow effect for current mode to show it's active
            const boxShadow = isCurrentMode ? `inset 0 0 12px ${icon.color}40, 0 0 12px ${icon.color}60` : 'none';
            console.log(`[ControlsManager] Mode ${index} - isCurrentMode: ${isCurrentMode}, currentModeNum: ${currentModeNum}`);

            return `
                            <button id="mode_${index}" onclick="controlsManager.handleModeChangeFullScreen(${index})"
                                style="
                                    padding: 20px 25px;
                                    font-size: 1.5em;
                                    color: ${buttonColor};
                                    background-color: ${bgColor};
                                    border: 2px solid ${icon.color};
                                    border-radius: 12px;
                                    cursor: pointer;
                                    transition: all 0.3s ease;
                                    min-width: 175px;
                                    display: flex;
                                    flex-direction: column;
                                    align-items: center;
                                    gap: 8px;
                                    opacity: 1;
                                    box-shadow: ${boxShadow};
                                "
                                onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'; this.style.transform='translateY(-2px)'; this.style.boxShadow='0 0 16px ${icon.color}80'"
                                onmouseout="this.style.backgroundColor='${bgColor}'; this.style.transform='translateY(0)'; this.style.boxShadow='${boxShadow === 'none' ? 'none' : `inset 0 0 12px ${icon.color}40, 0 0 12px ${icon.color}60`}'">
                                <i class="fa-solid ${icon.icon}"></i>
                                <span style="font-size: 0.6em; color: #ccc;">
                                    ${icon.title || 'Mode ' + (index)}
                                </span>
                                ${isCurrentMode ? `<span style="display: inline-block; background-color: ${icon.color}; color: #1a1a1a; padding: 4px 10px; border-radius: 12px; font-size: 0.45em; font-weight: 700; animation: pulseCheckmark 2s infinite; margin-top: 4px;">ACTIVE</span>` : ''}
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

        // Check if user is selecting the same mode that's currently active
        let currentModeNum = -1;
        if (typeof data_controls !== 'undefined' && data_controls && data_controls.current_states) {
            currentModeNum = data_controls.current_states.inverter_mode_num;
        }

        const isSameModeAsActive = parseInt(mode) === currentModeNum;
        if (isSameModeAsActive && parseInt(mode) !== -2) {
            const modeTitle = EOS_CONNECT_ICONS[parseInt(mode)]?.title || `Mode ${mode}`;
            console.log('[ControlsManager] User selected same mode as currently active - timer will restart');
            // Show info toast about restarting the override timer
            this.showToast(`Mode '${modeTitle}' override timer restarted (${duration})`, 'info', 3000);
        }

        console.log('[ControlsManager] Sending override control data:', controlData);

        // Show loading modal
        this.showLoadingModal();

        try {
            const result = await dataManager.setOverrideControl(controlData);
            console.log('[ControlsManager] Override control set successfully:', result);

            // Hide loading modal with a short delay
            setTimeout(() => {
                this.hideLoadingModal();
                // Close the overlay after successful operation
                closeFullScreenOverlay(250);
            }, 3000);

            // Refresh data to show updated state
            if (typeof init === 'function') {
                setTimeout(init, 1500); // Small delay to allow server to process
            }
        } catch (error) {
            console.error('[ControlsManager] Error setting override control:', error);
            this.hideLoadingModal();
            this.showToast('Failed to set override control: ' + error.message, 'error', 4000);
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
     * Check if mode is an EVCC charging mode (3-6)
     * @param {number} modeNum - Mode number to check
     * @returns {boolean} True if mode is EVCC charging
     */
    isEVCCMode(modeNum) {
        return modeNum >= 3 && modeNum <= 6;
    }

    /**
     * Update current controls display
     * Priority order: Manual Override > EVCC Modes > Dynamic Override > Normal Mode
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
        const dynOverrideActive = states.dyn_override_discharge_allowed_active;
        const isEVCCActive = this.isEVCCMode(inverterModeNum);

        // Update overall state display with proper priority
        // Priority: Manual Override (orange) > EVCC (no triangle) > Dynamic Override (green) > Normal
        const cleanModeText = inverterModeText.replace("MODE ", "");
        if (overrideActive) {
            // Manual override has highest priority
            document.getElementById('control_overall').innerHTML = `<i style="color:orange;" class="fa-solid fa-triangle-exclamation"></i> ${cleanModeText}`;
        } else if (isEVCCActive) {
            // EVCC modes have second priority - completely hide dynamic override
            document.getElementById('control_overall').innerHTML = cleanModeText;
        } else if (dynOverrideActive) {
            // Dynamic override for non-EVCC modes
            document.getElementById('control_overall').innerHTML = `<i style="color:#32CD32;" class="fa-solid fa-triangle-exclamation"></i> ${cleanModeText}`;
        } else {
            // Normal mode
            document.getElementById('control_overall').innerHTML = cleanModeText;
        }

        // Update controls based on priority (Manual Override > EVCC > Dynamic Override > Normal)
        if (overrideActive) {
            this.updateOverrideControls(states, overrideEndTime, inverterModeNum);
        } else if (isEVCCActive) {
            // EVCC modes completely mask dynamic override
            this.updateEVCCControls(states, inverterModeNum);
        } else if (dynOverrideActive) {
            this.updateDynamicOverrideControls(states, inverterModeNum);
        } else {
            this.updateNormalControls(states);
        }

        // Update mode icon and click handler
        // When EVCC is active, never show dynamic override indicators
        this.updateModeIcon(inverterModeNum, overrideActive, controlsData.battery.max_charge_power_dyn, isEVCCActive ? false : dynOverrideActive);

        this.updateManagedLoads(controlsData.managed_loads);

        // Show experimental banner if optimization source is ??? (t.b.d.) - was introduced in early phase of evopt
        if (controlsData.used_optimization_source === "tbd") {
            document.getElementById("experimental-banner").style.display = "flex";
        } else {
            document.getElementById("experimental-banner").style.display = "none";
        }
    }

    /**
     * Render the managed loads summary tile.
     *
     * A summary, deliberately: one line per load with its name and its state, and
     * everything else behind the overlay. The first cut put the temperature, the
     * target, the next start and the energy on every row, which wrapped to three lines
     * per load at 1680px and took so much width that Statistics and Battery State
     * started wrapping too.
     *
     * @param {Object[]|undefined} loads - The managed_loads array from current_controls
     */
    updateManagedLoads(loads) {
        const box = document.getElementById('managed_loads_box');
        const rows = document.getElementById('managed_loads_rows');
        if (!box || !rows) {
            return;
        }

        this.managedLoads = Array.isArray(loads) ? loads : [];
        if (this.managedLoads.length === 0) {
            box.style.display = 'none';
            return;
        }
        box.style.display = '';

        // More than this and the tile grows taller than its neighbours; the rest are
        // one click away in the overlay.
        const MAX_ROWS = 4;
        const shown = this.managedLoads.slice(0, MAX_ROWS);
        const hidden = this.managedLoads.length - shown.length;

        let totalWh = 0;
        for (const load of this.managedLoads) {
            totalWh += Number(load.planned_wh) || 0;
        }

        let html = shown.map(load => {
            const state = this.managedLoadState(load);
            return `<tr>
                <td class="top_box_info_text managed-load-name" title="${this.escapeHtml(this.managedLoadTooltip(load))}">
                    ${this.escapeHtml(load.id)}
                </td>
                <td class="managed-load-state">${state.icon} ${state.short}</td>
            </tr>`;
        }).join('');

        if (hidden > 0) {
            html += `<tr><td colspan="2" class="top_box_info_text managed-load-more">
                +${hidden} more
            </td></tr>`;
        }

        rows.innerHTML = html;

        const totalEl = document.getElementById('managed_loads_total');
        if (totalEl) {
            totalEl.innerHTML = `${(totalWh / 1000).toFixed(1)} <span style="font-size: 0.8em;">kWh</span>`;
            totalEl.title = 'Energy planned for managed loads — click for details';
        }
    }

    /**
     * Icon and short label for one load's current state.
     * @param {Object} load - One managed_loads entry
     * @returns {{icon: string, short: string, label: string}}
     */
    managedLoadState(load) {
        if (load.released === true) {
            return {
                icon: '<i style="color:#32CD32;" class="fa-solid fa-play"></i>',
                short: 'on',
                label: 'Released',
            };
        }
        if (load.released === false) {
            const next = load.next_release_start
                ? new Date(load.next_release_start).toLocaleTimeString(navigator.language, {
                    hour: '2-digit', minute: '2-digit'
                })
                : 'off';
            return {
                icon: '<i style="color:#888;" class="fa-solid fa-pause"></i>',
                short: next,
                label: 'Blocked',
            };
        }
        // No release signal at all: a pushed profile is a forecast, not something we
        // switch on and off.
        return {
            icon: '<i style="color:#888;" class="fa-solid fa-chart-line"></i>',
            short: 'fc',
            label: 'Forecast only',
        };
    }

    /**
     * The detail that no longer fits on the tile row, as a hover title.
     * @param {Object} load - One managed_loads entry
     * @returns {string} Plain text
     */
    managedLoadTooltip(load) {
        const parts = [load.id];
        if (load.temperature_c !== null && load.temperature_c !== undefined) {
            parts.push(`${load.temperature_c}°C of ${load.target_temperature_c}°C`);
        }
        const needed = Number(load.energy_needed_wh) || 0;
        if (needed > 0) {
            parts.push(`${(needed / 1000).toFixed(1)} kWh needed`);
        }
        if (load.reason) {
            parts.push(load.reason);
        }
        return parts.join(' — ');
    }

    /**
     * Show every managed load in a full-screen overlay.
     *
     * The tile is a summary by necessity - it shares a row with four others. This is
     * where the rest lives: why each load is in the state it is, what it still needs,
     * when it will next run, and how far the calibration has settled.
     */
    async showManagedLoadsOverlay() {
        const header = '<i class="fa-solid fa-sliders"></i> Managed Loads';
        try {
            const res = await fetch('api/managed_loads/?nocache=' + Date.now());
            if (res.status === 404) {
                showFullScreenOverlay(header, this._managedLoadsEmptyHtml());
                return;
            }
            if (!res.ok) {
                showFullScreenOverlay(header,
                    "<div style='color:#dc3545;'>Failed to load managed load details.</div>");
                return;
            }

            const data = await res.json();
            const loads = (data.loads || []).filter(l => l.enabled);
            if (loads.length === 0) {
                showFullScreenOverlay(header, this._managedLoadsEmptyHtml());
                return;
            }

            const slotSeconds = Number(data.time_frame_base) || 3600;
            const now = new Date();
            const currentSlot = Math.floor(
                (now.getHours() * 3600 + now.getMinutes() * 60) / slotSeconds
            );

            const total = Number(data.contribution_total_wh) || 0;
            const budget = Number(data.max_power_w) || 0;

            let html = `<div style="margin-bottom: 16px; opacity: 0.85;">
                ${loads.length} load${loads.length === 1 ? '' : 's'} &middot;
                <strong>${(total / 1000).toFixed(1)} kWh</strong> added to the load forecast
                ${budget > 0 ? `&middot; shared limit ${budget} W` : ''}
            </div>`;

            html += loads.map(l => this._managedLoadCard(l, slotSeconds, currentSlot)).join('');
            showFullScreenOverlay(header, html);
        } catch (err) {
            console.error('[ControlsManager] Managed loads overlay failed:', err);
            showFullScreenOverlay(header,
                "<div style='color:#dc3545;'>Failed to load managed load details.</div>");
        }
    }

    /**
     * What the overlay shows when nothing is configured.
     * @returns {string} HTML
     */
    _managedLoadsEmptyHtml() {
        return `<div style="opacity: 0.85; line-height: 1.6;">
            <p>No managed loads are configured yet.</p>
            <p>A managed load is an appliance the household load forecast cannot follow on
               its own &mdash; a pool heat pump, a sauna, a hot water tank, or a heating
               profile pushed in from Home Assistant.</p>
            <p>Add one under <strong>Menu &rsaquo; Configuration &rsaquo; Managed Loads</strong>.</p>
        </div>`;
    }

    /**
     * One load's detail card.
     *
     * Laid out to answer the questions in the order they get asked: what is it doing,
     * how much energy does that take and can it actually get it, what is the model
     * standing on, and how much of the model is measured rather than assumed.
     *
     * @param {Object} load - An entry from GET /api/managed_loads
     * @param {number} slotSeconds - Seconds per optimizer slot
     * @param {number} currentSlot - Index of the slot happening now
     * @returns {string} Card HTML
     */
    _managedLoadCard(load, slotSeconds, currentSlot) {
        const release = load.release || null;
        const detail = load.detail || {};
        const model = load.model || {};

        let pill;
        if (!release) {
            pill = this._pill('#555', 'forecast only');
        } else if (release.released) {
            pill = this._pill('#2e7d32', 'running');
        } else {
            pill = this._pill('#555', 'blocked');
        }

        return `<div style="background:rgb(54,54,54);border-radius:10px;padding:14px;margin-bottom:14px;">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;
                        margin-bottom:6px;flex-wrap:wrap;">
                <strong style="font-size:1.1em;">${this.escapeHtml(load.id)}</strong>
                <span style="opacity:0.6;font-size:0.85em;">${this.escapeHtml(load.type)}</span>
                ${pill}
            </div>
            <div style="opacity:0.75;font-size:0.9em;margin-bottom:12px;">
                ${this.escapeHtml((release && release.reason) || load.reason || '')}
            </div>
            ${this._managedLoadEnergy(load, detail)}
            ${this._managedLoadFacts(load, detail, model, release)}
            ${this._managedLoadCalibration(load, model)}
            ${this._managedLoadPlanStrip(load.plan || [], slotSeconds, currentSlot)}
        </div>`;
    }

    /**
     * A coloured pill.
     * @param {string} colour - CSS background
     * @param {string} text - Label
     * @returns {string} HTML
     */
    _pill(colour, text) {
        return `<span style="background:${colour};padding:3px 10px;border-radius:12px;
                     font-size:0.8em;">${this.escapeHtml(text)}</span>`;
    }

    /**
     * The energy block: what is needed, split into why, against what can be delivered.
     *
     * "Energy needed" on its own was the most misread number on the page. For a pool it
     * is dominated by standing losses over the rest of the horizon, not by the gap to
     * the target, so a 1.3 degree rise reads as 77 kWh and looks absurd. Splitting it
     * and saying how long the horizon is makes it ordinary.
     *
     * @param {Object} load - The load entry
     * @param {Object} detail - Its model detail
     * @returns {string} HTML
     */
    _managedLoadEnergy(load, detail) {
        const needed = Number(load.energy_needed_wh) || 0;
        const planned = Number(load.planned_wh) || 0;
        if (needed <= 0 && planned <= 0) {
            return '';
        }

        const cop = Number(detail.mean_cop) || 0;
        const kwh = wh => `${(wh / 1000).toFixed(1)} kWh`;
        // The split is thermal; divide by the same COP the model used so the parts add
        // up to the electrical total shown above them.
        const toElectric = thermal => (cop > 0 ? thermal / cop : 0);
        const heatUp = Number(detail.heat_up_wh_thermal) || 0;
        const losses = Number(detail.standing_losses_wh_thermal) || 0;

        let split = '';
        if (heatUp > 0 || losses > 0) {
            const hours = Number(detail.horizon_hours) || 0;
            split = `<div style="opacity:0.7;font-size:0.85em;margin:2px 0 8px 0;line-height:1.5;">
                ${heatUp > 0 ? `${kwh(toElectric(heatUp))} to reach
                    ${detail.target_temperature_c}&nbsp;&deg;C` : ''}
                ${heatUp > 0 && losses > 0 ? ' &middot; ' : ''}
                ${losses > 0 ? `${kwh(toElectric(losses))} to hold it
                    ${hours ? `for the next ${Math.round(hours)}&nbsp;h` : ''}` : ''}
            </div>`;
        }

        // Planned against needed, as a state rather than two numbers to compare.
        let coverage = '';
        if (needed > 0) {
            const pct = Math.min(100, Math.round((planned / needed) * 100));
            const short = pct < 98;
            const colour = short ? '#e0a030' : '#4a9eff';
            coverage = `
                <div style="height:6px;background:rgba(255,255,255,0.12);border-radius:3px;
                            overflow:hidden;margin-top:8px;">
                    <div style="width:${pct}%;height:100%;background:${colour};"></div>
                </div>
                <div style="font-size:0.85em;opacity:0.75;margin-top:4px;">
                    ${short
                        ? `Planned ${kwh(planned)} &mdash; covers ${pct}% of it. The load
                           cannot get enough runtime; widen its window or raise the daily
                           cap.`
                        : `Planned ${kwh(planned)} &mdash; fully covered.`}
                </div>`;
        }

        return `<div style="background:rgba(0,0,0,0.15);border-radius:6px;padding:10px;
                            margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;">
                <span style="opacity:0.7;">Energy needed</span>
                <strong>${kwh(needed)}</strong>
            </div>
            ${split}
            ${coverage}
        </div>`;
    }

    /**
     * The measured inputs and the appliance's rating.
     * @param {Object} load - The load entry
     * @param {Object} detail - Its model detail
     * @param {Object} model - Its model status
     * @param {Object|null} release - Its release state
     * @returns {string} HTML
     */
    _managedLoadFacts(load, detail, model, release) {
        const facts = [];

        if (detail.temperature_c !== undefined && detail.temperature_c !== null) {
            facts.push(['Temperature',
                `${detail.temperature_c} &deg;C &rarr; ${detail.target_temperature_c} &deg;C`]);
        }

        // The input that drives everything, and where it came from. Without the second
        // half a prediction standing on a guessed constant looks exactly like one
        // standing on a forecast.
        //
        // Both numbers are shown when they differ, because "Outside now" promises a
        // measurement: reading 17.9 from a regional forecast while the thermometer in
        // the garden says 14.9 is not a rounding difference -- on a pool it is a
        // quarter of the standing loss.
        if (detail.ambient_now_c !== undefined && detail.ambient_now_c !== null) {
            const SOURCES = {
                forecast: 'from the weather forecast',
                forecast_corrected: 'forecast, corrected to your sensor',
                sensor: 'from your sensor, held flat',
                fallback: 'a fixed guess &mdash; no forecast and no sensor',
            };
            const note = SOURCES[detail.ambient_source] || '';
            const warn = detail.ambient_source === 'fallback';
            const measured = detail.ambient_measured_c;
            const differs = measured !== undefined && measured !== null
                && Math.abs(measured - detail.ambient_now_c) >= 0.5;

            const value = differs
                ? `${measured} &deg;C measured
                   <span style="opacity:0.6;">&middot; model using ${detail.ambient_now_c} &deg;C</span>`
                : `${detail.ambient_now_c} &deg;C`;

            facts.push(['Outside now',
                `${value}
                 <div style="opacity:0.6;font-size:0.85em;${warn ? 'color:#e0a030;' : ''}">${note}</div>`]);
        }

        if (release && release.next_release_start) {
            facts.push(['Next start', new Date(release.next_release_start)
                .toLocaleString(navigator.language,
                    { weekday: 'short', hour: '2-digit', minute: '2-digit' })]);
        }

        // Both currencies. The rating is electrical and everything above it is derived
        // from heat, which made it the one number on the card that did not compare.
        if (detail.electrical_power_w || model.rated_power_w) {
            const electrical = detail.electrical_power_w || model.rated_power_w;
            const thermal = detail.thermal_power_w;
            facts.push(['Power', thermal
                ? `${electrical} W electrical &rarr;
                   <span style="opacity:0.8;">${(thermal / 1000).toFixed(1)} kW of heat</span>`
                : `${electrical} W electrical`]);
        }
        if (detail.mean_cop) {
            facts.push(['Efficiency now', `COP ${detail.mean_cop}`]);
        }
        if (release && release.override) {
            facts.push(['Override', `${release.override} until ` + new Date(release.override_until)
                .toLocaleTimeString(navigator.language, { hour: '2-digit', minute: '2-digit' })]);
        }
        if (load.error) {
            facts.push(['Error', this.escapeHtml(load.error)]);
        }

        return facts.map(([k, v]) => `
            <div style="display:flex;justify-content:space-between;gap:12px;padding:3px 0;">
                <span style="opacity:0.7;">${k}</span><span style="text-align:right;">${v}</span>
            </div>`).join('');
    }

    /**
     * How much of the model is measured rather than assumed, and a way to start over.
     * @param {string} load - The load entry
     * @param {Object} model - Its model status
     * @returns {string} HTML
     */
    _managedLoadCalibration(load, model) {
        if (model.confidence === undefined || model.confidence === null) {
            return '';
        }
        const pct = Math.round(model.confidence * 100);
        const settled = pct >= 50;

        return `<div style="display:flex;align-items:center;justify-content:space-between;
                            gap:12px;padding:8px 0 0 0;margin-top:8px;
                            border-top:1px solid rgba(255,255,255,0.08);">
            <div style="min-width:0;">
                <div style="opacity:0.7;">Calibration ${pct}%</div>
                <div style="opacity:0.6;font-size:0.85em;">
                    ${settled
                        ? `Heat loss and efficiency measured from ${model.loss_samples || 0}
                           cooling and ${model.cop_samples || 0} heating periods.`
                        : 'Still learning &mdash; the plan is running on the values from the configuration form.'}
                    ${(model.fit_quality !== undefined && model.fit_quality < 0.5)
                        ? `<div style="color:#e0a030;">The readings do not fit the model
                           well &mdash; often a temperature sensor too coarse to measure
                           how slowly this store changes.</div>`
                        : ''}
                </div>
            </div>
            <button class="config-btn" style="flex:0 0 auto;"
                    onclick="controlsManager.resetManagedLoadCalibration('${this.escapeHtml(load.id)}')"
                    title="Discard what has been learned and start again from the configured values">
                <i class="fas fa-rotate-left"></i> Reset
            </button>
        </div>`;
    }

    /**
     * Discard a load's learned coefficients and its recorded samples.
     *
     * Worth doing when the inputs it was fitted against turn out to have been wrong -
     * the samples carry those inputs, so they keep dragging the fit until they age out
     * of the retention window on their own.
     *
     * @param {string} loadId - Which load
     */
    async resetManagedLoadCalibration(loadId) {
        try {
            const res = await fetch(`api/managed_loads/${encodeURIComponent(loadId)}/calibration/reset`,
                { method: 'POST' });
            const data = await res.json();
            if (!res.ok) {
                this.showToast(data.error || 'Reset failed.', 'error');
                return;
            }
            this.showToast(`${loadId}: calibration reset — learning again from scratch.`, 'info');
            // Reopen so the card shows the reset state rather than the stale one.
            this.showManagedLoadsOverlay();
        } catch (err) {
            console.error('[ControlsManager] Calibration reset failed:', err);
            this.showToast('Reset request failed.', 'error');
        }
    }

    /**
     * A strip showing which slots of the horizon this load is planned to run in.
     *
     * The bars alone answer "roughly when"; the axis and the hover answer "exactly
     * when", which is the question you have once you are deciding whether the plan is
     * sensible.
     *
     * @param {number[]} plan - Wh per slot, starting at local midnight today
     * @param {number} slotSeconds - Seconds per slot
     * @param {number} currentSlot - Index of the slot happening now
     * @returns {string} Strip HTML, or "" when nothing is planned
     */
    _managedLoadPlanStrip(plan, slotSeconds, currentSlot) {
        if (!plan.length) {
            return '';
        }
        const peak = Math.max(...plan.map(v => Number(v) || 0));
        if (peak <= 0) {
            return '';
        }

        const slotsPerHour = Math.max(1, Math.round(3600 / slotSeconds));
        const slotsPerDay = 24 * slotsPerHour;

        // Slot 0 is local midnight today, by the same convention the optimizer array
        // uses, so a slot index converts straight to a wall-clock time.
        const midnight = new Date();
        midnight.setHours(0, 0, 0, 0);
        const slotStart = i => new Date(midnight.getTime() + i * slotSeconds * 1000);

        const hhmm = d => d.toLocaleTimeString(navigator.language,
            { hour: '2-digit', minute: '2-digit' });
        const dayName = d => d.toLocaleDateString(navigator.language,
            { weekday: 'short', day: 'numeric', month: 'short' });

        let running = 0;
        const bars = plan.map((value, i) => {
            const v = Number(value) || 0;
            running += v;
            const from = slotStart(i);
            const to = slotStart(i + 1);
            const isNow = i === currentSlot;

            const tip = [
                `${dayName(from)} ${hhmm(from)}\u2013${hhmm(to)}`,
                v > 0 ? `${Math.round(v)} Wh planned` : 'not planned',
                v > 0 ? `${(running / 1000).toFixed(1)} kWh cumulative` : null,
                isNow ? 'happening now' : null,
            ].filter(Boolean).join(' \u00b7 ');

            const height = v > 0 ? Math.max(18, Math.round((v / peak) * 100)) : 6;
            const colour = v > 0 ? '#4a9eff' : 'rgba(255,255,255,0.12)';
            return `<div title="${this.escapeHtml(tip)}" style="flex:1 1 0;height:${height}%;
                background:${colour};align-self:flex-end;
                ${isNow ? 'outline:1px solid #fff;outline-offset:-1px;' : ''}"></div>`;
        }).join('');

        // A tick every six hours: enough to read the shape against the clock without
        // crowding a strip that is only a few hundred pixels wide.
        const TICK_HOURS = 6;
        const ticks = [];
        for (let hour = 0; hour * slotsPerHour < plan.length; hour += TICK_HOURS) {
            const slot = hour * slotsPerHour;
            ticks.push({
                left: (slot / plan.length) * 100,
                label: String(slotStart(slot).getHours()).padStart(2, '0'),
                major: hour % 24 === 0,
            });
        }

        const tickHtml = ticks.map(t => `
            <span style="position:absolute;left:${t.left}%;transform:translateX(-50%);
                         font-size:0.75em;opacity:${t.major ? 0.85 : 0.5};
                         ${t.major ? 'font-weight:600;' : ''}">${t.label}</span>`).join('');

        const gridHtml = ticks.map(t => `
            <div style="position:absolute;top:0;bottom:0;left:${t.left}%;
                        border-left:1px ${t.major ? 'dashed' : 'dotted'}
                        rgba(255,255,255,${t.major ? 0.35 : 0.15});"></div>`).join('');

        const dayLabels = [];
        for (let day = 0; day * slotsPerDay < plan.length; day++) {
            const span = Math.min(slotsPerDay, plan.length - day * slotsPerDay);
            dayLabels.push(`<span style="flex:${span} 1 0;text-align:center;opacity:0.65;">
                ${dayName(slotStart(day * slotsPerDay))}</span>`);
        }

        return `<div style="margin-top:12px;">
            <div style="opacity:0.7;font-size:0.85em;margin-bottom:4px;">
                Planned slots &mdash; hover a bar for the time and energy
            </div>
            <div style="position:relative;display:flex;align-items:flex-end;gap:1px;height:44px;
                        background:rgba(0,0,0,0.15);border-radius:4px;padding:2px;">
                ${gridHtml}
                ${bars}
            </div>
            <div style="position:relative;height:1.2em;margin-top:2px;">${tickHtml}</div>
            <div style="display:flex;font-size:0.8em;margin-top:2px;">${dayLabels.join('')}</div>
        </div>`;
    }

    /**
     * Escape text taken from configuration before putting it in the DOM.
     * @param {string} str - Raw text
     * @returns {string} Escaped text
     */
    escapeHtml(str) {
        if (str === null || str === undefined) {
            return '';
        }
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
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
            const acPowerKw = (states.current_ac_charge_power / 1000).toFixed(2);
            document.getElementById('control_dc_charge').innerText = acPowerKw + " kW";
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
     * Update controls when EVCC charging is active (Modes 3-6)
     * EVCC completely masks dynamic override - never show any PV>Load indicators
     */
    updateEVCCControls(states, inverterModeNum) {
        const modeTitle = EOS_CONNECT_ICONS[inverterModeNum]?.title || `Mode ${inverterModeNum}`;
        
        // Show EVCC mode information
        document.getElementById('control_ac_charge_desc').innerText = "E-Car Charging Mode";
        document.getElementById('control_ac_charge_desc').style.color = "";
        document.getElementById('control_ac_charge').innerHTML = modeTitle;
        document.getElementById('control_ac_charge').style.color = "";

        // Show AC charging power for all EVCC modes
        document.getElementById('control_dc_charge_desc').innerText = "AC Charge Power";
        const acPowerKw = (states.current_ac_charge_power / 1000).toFixed(2);
        document.getElementById('control_dc_charge').innerText = acPowerKw + " kW";

        // EVCC always masks dynamic override - never show it
        document.getElementById('control_discharge_allowed_desc').innerText = "";
        document.getElementById('control_discharge_allowed').innerText = "";
        document.getElementById('current_controls_box').style.border = "";
    }

    /**
     * Update controls when dynamic override is active (for non-EVCC modes)
     */
    updateDynamicOverrideControls(states, inverterModeNum) {
        document.getElementById('control_ac_charge_desc').innerText = "Dynamic Override Active";
        document.getElementById('control_ac_charge_desc').style.color = "#32CD32";
        document.getElementById('control_ac_charge').innerText = "PV > Load";
        document.getElementById('control_ac_charge').style.color = "#32CD32";

        if (inverterModeNum === 0) {
            document.getElementById('control_dc_charge_desc').innerText = "AC Charge Power";
            const acPowerKw = (states.current_ac_charge_power / 1000).toFixed(2);
            document.getElementById('control_dc_charge').innerText = acPowerKw + " kW";
        } else if (inverterModeNum === 2) {
            document.getElementById('control_dc_charge_desc').innerText = "DC Charge Power";
            document.getElementById('control_dc_charge').innerText = (states.current_dc_charge_demand / 1000).toFixed(1) + " kW";
        } else {
            document.getElementById('control_dc_charge_desc').innerText = "";
            document.getElementById('control_dc_charge').innerText = "";
        }

        document.getElementById('control_discharge_allowed_desc').innerText = "";
        document.getElementById('control_discharge_allowed').innerText = "";
        document.getElementById('current_controls_box').style.border = "1px solid #32CD32";
    }

    /**
     * Update controls in normal mode
     */
    updateNormalControls(states) {
        document.getElementById('control_ac_charge_desc').innerText = "AC Charge Power";
        document.getElementById('control_ac_charge_desc').style.color = "";
        const acPowerKw = (states.current_ac_charge_power / 1000).toFixed(2);
        const acEnergyKwh = (states.current_ac_charge_demand / 1000).toFixed(3);
        console.log('[CHARGE_DEMAND] Dashboard AC Charge: power=' + states.current_ac_charge_power + ' W, energy=' + states.current_ac_charge_demand + ' Wh');
        document.getElementById('control_ac_charge').innerHTML = acPowerKw + " kW <span style='font-size: 0.75em;'>("+ acEnergyKwh + " kWh)</span>";
        document.getElementById('control_ac_charge').style.color = "";

        document.getElementById('control_dc_charge_desc').innerText = "DC Charge";
        document.getElementById('control_dc_charge').innerText = (states.current_dc_charge_demand / 1000).toFixed(1) + " kW";

        document.getElementById('control_discharge_allowed_desc').innerText = "Discharge allowed";
        document.getElementById('control_discharge_allowed').innerText = states.current_discharge_allowed ? "Yes" : "No";

        document.getElementById('current_controls_box').style.border = "";

    }

    /**
     * Update the mode icon and setup click handler
     * Priority: Manual Override (orange) > EVCC (no triangle) > Dynamic Override (green) > Normal
     * When EVCC is active, dynOverrideActive should already be false
     */
    updateModeIcon(inverterModeNum, overrideActive, maxChargePowerDyn, dynOverrideActive = false) {
        const iconElement = document.getElementById('current_header_right');
        if (!iconElement) return;

        iconElement.innerHTML = ""; // Clear previous content

        const iconData = EOS_CONNECT_ICONS[inverterModeNum] || {};
        const { icon, color, title } = iconData;

        iconElement.innerHTML = `<i class="fa-solid ${icon}"></i>`;
        iconElement.style.color = color || "";
        iconElement.title = title || "";

        // Add warning/indicator icons based on priority
        if (overrideActive) {
            // Manual override: show orange warning (highest priority)
            iconElement.innerHTML = '<i style="color:orange;" class="fa-solid fa-triangle-exclamation"></i> ' + iconElement.innerHTML;
        } else if (dynOverrideActive) {
            // Dynamic override only (EVCC would have masked this): show green warning
            iconElement.innerHTML = '<i style="color:#32CD32;" class="fa-solid fa-triangle-exclamation"></i> ' + iconElement.innerHTML;
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


/**
 * Open the managed loads overlay.
 *
 * Global because the dropdown menu and the tile's header chip both call it inline, the
 * same way showBatteryOverviewMenu and the rest are reached.
 */
function showManagedLoadsMenu() {
    if (typeof controlsManager !== 'undefined' && controlsManager) {
        controlsManager.showManagedLoadsOverlay();
    }
}
