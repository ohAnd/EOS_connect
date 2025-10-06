/**
 * UI helper functions, overlays, animations
 * Extracted from legacy index.html
 */

function isMobile() {
    return window.innerWidth <= 768;
}

function writeIfValueChanged(id, value) {
    const element = document.getElementById(id);
    if (element.innerText !== value) {
        element.innerText = value;
        element.classList.add('valueChange');
        setTimeout(() => {
            element.classList.remove('valueChange');
        }, 1000); // Remove the class after 1 second
    }
}

function overlayMenu(header, content, close = true) {
    const overlay = document.getElementById('overlay_menu');
    // Always update content, whether overlay is open or closed
    overlay.style.display = 'flex';
    document.getElementById('overlay_menu_head').innerHTML = header;
    document.getElementById('overlay_menu_content').innerHTML = content;
    document.getElementById('overlay_menu_close').style.display = close ? '' : 'none';

    // Block background scrolling
    document.body.style.overflow = 'hidden';
}

function closeOverlayMenu(direct = true) {
    const overlay = document.getElementById('overlay_menu');
    if (overlay.style.display === 'flex') {
        if (direct) {
            overlayMenu('', '', false);
            overlay.style.display = 'none';
            // Restore background scrolling
            document.body.style.overflow = '';
        } else {
            overlay.style.transition = 'opacity 1s';
            overlay.style.opacity = '0';
            setTimeout(() => {
                overlayMenu('', '', false);
                overlay.style.display = 'none';
                overlay.style.opacity = '1';
                // Restore background scrolling
                document.body.style.overflow = '';
            }, 250);
        }
    }
}

function getBatteryIcon(soc_value) {
    if (soc_value > 90) {
        return '<i class="fa-solid fa-battery-full"></i>';
    } else if (soc_value > 70) {
        return '<i class="fa-solid fa-battery-three-quarters"></i>';
    } else if (soc_value > 50) {
        return '<i class="fa-solid fa-battery-half"></i>';
    } else if (soc_value > 30) {
        return '<i class="fa-solid fa-battery-quarter"></i>';
    } else {
        return '<i class="fa-solid fa-battery-empty"></i>';
    }
}

// Initialize value change observers
function initializeValueChangeObservers() {
    Array.from(document.getElementsByClassName("valueChange")).forEach(function (element) {
        const observer = new MutationObserver(function (mutationsList, observer) {
            const elem = mutationsList[0].target;
            elem.style.color = "black";
            setTimeout(function () {
                elem.style.color = "inherit";
            }, 1000);
        });
        observer.observe(element, { characterData: false, childList: true, attributes: false });
    });
}

/**
 * Test Control Functions for Development and Testing
 */
function toggleTestPanel() {
    const testControls = document.getElementById('test_controls');

    if (testControls.style.display === 'none' || testControls.style.display === '') {
        testControls.style.display = 'block';
    } else {
        testControls.style.display = 'none';
    }
}

function switchTestScenario() {
    const select = document.getElementById('test_scenario_select');
    const scenario = select.value;

    if (scenario === 'live') {
        currentTestScenario = TEST_SCENARIOS.LIVE;
    } else {
        currentTestScenario = scenario;
    }

    console.log('[TestMode] Switched to scenario:', currentTestScenario);

    // Automatically refresh data when switching scenarios
    refreshTestData();
}

async function refreshTestData() {
    console.log('[TestMode] Refreshing data with scenario:', currentTestScenario);

    // Force refresh by calling init() which will use the current test scenario
    if (typeof init === 'function') {
        await init();
    }
}

function showTestPanel() {
    const panel = document.getElementById('test_control_panel');
    if (panel) {
        panel.style.display = 'block';
    }
}

function hideTestPanel() {
    const panel = document.getElementById('test_control_panel');
    if (panel) {
        panel.style.display = 'none';
    }
}

// Initialize test panel on page load
document.addEventListener('DOMContentLoaded', function () {
    // Show test panel ONLY if URL contains test=1 parameter
    const urlParams = new URLSearchParams(window.location.search);
    const isTestParam = urlParams.get('test') === '1';

    if (isTestParam) {
        console.log('[TestMode] Test mode activated via ?test=1 parameter');
        setTimeout(() => showTestPanel(), 1000); // Show after page loads
    } else {
        console.log('[TestMode] Test mode not activated (no ?test=1 parameter)');
    }
});

/**
 * Show main dropdown menu near the menu icon
 */
function showMainMenu(version) {
    // Remove existing dropdown if present
    const existingDropdown = document.getElementById('main-dropdown-menu');
    if (existingDropdown) {
        existingDropdown.remove();
        return; // Toggle behavior - close if already open
    }

    // Create dropdown menu
    const dropdown = document.createElement('div');
    dropdown.id = 'main-dropdown-menu';
    dropdown.style.cssText = `
        position: absolute;
        top: 45px;
        left: 10px;
        background-color: rgb(58, 58, 58);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        z-index: 1000;
        min-width: 180px;
        padding: 8px 0;
        // font-size: 0.9em;
        font-size: ${isMobile() ? '1.1em' : '0.9em'};
    `;

    dropdown.innerHTML = `
        <div onclick="showOverrideControlsMenu(); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-sliders" style="margin-right: 10px; color: #cccccc; width: 16px;"></i>
            <span>Override Controls</span>
        </div>
        
        <hr style="border: none; border-top: 1px solid rgba(255, 255, 255, 0.1); margin: 5px 0;">
        
        <div onclick="showAlarmsMenu(); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-triangle-exclamation" style="margin-right: 10px; color: #cccccc; width: 16px;"></i>
            <span>Alarms</span>
        </div>
        
        <div onclick="showLogsMenu(); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-list-ul" style="margin-right: 10px; color: #cccccc; width: 16px;"></i>
            <span>Logs</span>
        </div>
        
        <hr style="border: none; border-top: 1px solid rgba(255, 255, 255, 0.1); margin: 5px 0;">

        <div onclick="window.open('https://github.com/ohAnd/ha_addons/blob/master/eos_connect/CHANGELOG.md', '_blank'); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center; justify-content: space-between;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <div style="display: flex; align-items: center;">
                <i class="fa-solid fa-file-text" style="margin-right: 10px; color: #cccccc; width: 16px;"></i>
                <span>Changelog</span>
            </div>
            <i class="fa-solid fa-external-link-alt" style="font-size: 0.7em; color: #888888;"></i>
        </div>
        <div onclick="sendBugReport(); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <div style="display: flex; align-items: center;">
                <i class="fa-solid fa-bug" style="margin-right: 10px; color: #cccccc; width: 16px;"></i>
                <span>Bug Report</span>
            </div>
        </div>

        <div onclick="showInfoMenu('${version}'); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-info-circle" style="margin-right: 10px; color: #cccccc; width: 16px;"></i>
            <span>Info</span>
        </div>
    `;

    // Find the menu icon parent container to position relative to it
    const menuIcon = document.getElementById('current_header_left');
    const parentBox = menuIcon.closest('.top-box');

    // Add relative positioning to parent if not already present
    if (getComputedStyle(parentBox).position === 'static') {
        parentBox.style.position = 'relative';
    }

    // Append dropdown to parent container
    parentBox.appendChild(dropdown);

    // Update dropdown notifications using centralized system
    if (typeof MenuNotifications !== 'undefined') {
        MenuNotifications.updateDropdown();
    }

    // Add click outside listener to close dropdown
    setTimeout(() => {
        document.addEventListener('click', handleClickOutside, true);
    }, 0);
}

/**
 * Close dropdown menu
 */
function closeDropdownMenu() {
    const dropdown = document.getElementById('main-dropdown-menu');
    if (dropdown) {
        dropdown.remove();
        document.removeEventListener('click', handleClickOutside, true);
    }
}

/**
 * Hamburger Menu Dot Controller - Simple State-Aware System
 * Knows exactly what's displayed and only changes when needed
 */
const MenuNotifications = {
    displayedColor: null, // What's actually displayed: null, 'red', 'orange', 'white', 'gray'

    /**
     * Initialize the notification system
     */
    init() {
        console.log('[MenuNotifications] Simple state-aware system initialized');
    },

    /**
     * Show a dot with specific color (external interface)
     * Priority order: red > orange > white > gray > none
     * @param {string|null} requestedColor - 'red', 'orange', 'white', 'gray', or null
     */
    showDot(requestedColor) {
        // console.log(`[MenuNotifications] Request: show ${requestedColor}, currently displaying: ${this.displayedColor}`);

        // Determine what should be displayed based on priority
        let targetColor = this.getTargetColor(requestedColor);

        // Only update if the target is different from what's displayed
        if (targetColor !== this.displayedColor) {
            console.log(`[MenuNotifications] State change needed: '${this.displayedColor}' → '${targetColor}'`);
            this.displayedColor = targetColor;
            this.renderDot();
        } else {
            // console.log(`[MenuNotifications] No change needed - already displaying '${this.displayedColor}'`);
        }
    },

    /**
     * Determine target color based on priority rules
     */
    getTargetColor(requestedColor) {
        // For now, just return the requested color
        // Later can add priority logic for multiple sources
        return requestedColor;
    },

    /**
     * Render the dot based on displayedColor (only called when state changes)
     */
    renderDot() {
        const menuElement = document.getElementById('current_header_left');
        if (!menuElement) {
            console.log(`[MenuNotifications] Menu element not found`);
            return;
        }

        // Always remove existing dot first (clean slate)
        const existingDot = menuElement.querySelector('.notification-dot');
        if (existingDot) {
            existingDot.remove();
        }

        // Add new dot if needed
        if (this.displayedColor) {
            const colors = {
                'red': 'rgb(220, 53, 69)',
                'orange': 'rgb(255, 193, 7)',
                'white': 'rgb(255, 255, 255)',
                'gray': 'rgb(136, 136, 136)'
            };

            const dotColor = colors[this.displayedColor];
            if (dotColor) {
                const dot = document.createElement('div');
                dot.className = 'notification-dot';
                dot.style.cssText = `
                    position: absolute;
                    top: -2px;
                    right: -2px;
                    width: 6px;
                    height: 6px;
                    background-color: ${dotColor};
                    border-radius: 50%;
                    /* border: 1px solid darkgray; optional border */
                    z-index: 999;
                    pointer-events: none;
                `;
                menuElement.appendChild(dot);
                console.log(`[MenuNotifications] Rendered ${this.displayedColor} dot`);
            }
        } else {
            console.log(`[MenuNotifications] Removed dot (no color)`);
        }
    },

    /**
     * Update dropdown menu notifications
     */
    updateDropdown() {
        const dropdown = document.getElementById('main-dropdown-menu');
        if (!dropdown) return;

        // Only update Alarms menu item (not Logs)
        const alarmsItem = dropdown.querySelector('div[onclick*="showAlarmsMenu"]');
        if (alarmsItem) {
            // Convert our color system to old status system for dropdown
            let status = null;
            if (this.displayedColor === 'red') status = 'error';
            else if (this.displayedColor === 'orange') status = 'warning';

            this.addDropdownNotification(alarmsItem, status);
        }

        // Ensure Logs menu item has no notification dot
        const logsItem = dropdown.querySelector('div[onclick*="showLogsMenu"]');
        if (logsItem) {
            const existingDot = logsItem.querySelector('.dropdown-notification-dot');
            if (existingDot) {
                existingDot.remove();
            }
        }
    },

    /**
     * Add notification dot to dropdown menu item
     * @param {Element} menuItem - The menu item element
     * @param {string|null} status - The notification status
     */
    addDropdownNotification(menuItem, status) {
        // Remove existing notification dot
        const existingDot = menuItem.querySelector('.dropdown-notification-dot');
        if (existingDot) {
            existingDot.remove();
        }

        // Add new notification dot if needed
        if (status) {
            const dotColor = status === 'error' ? '#dc3545' : '#ffc107';
            const dot = document.createElement('div');
            dot.className = 'dropdown-notification-dot';
            dot.style.cssText = `
                width: 8px;
                height: 8px;
                background-color: ${dotColor};
                border-radius: 50%;
                margin-left: auto;
                margin-right: 8px;
                flex-shrink: 0;
                border: 1px solid rgba(255,255,255,0.2);
            `;

            menuItem.appendChild(dot);
        }
    },

    /**
     * Restore notification after menu element changes (only if actually missing)
     */
    restoreAfterMenuChange() {
        // Small delay to ensure DOM is updated
        setTimeout(() => {
            if (this.displayedColor) {
                // Check if dot actually exists before restoring
                const menuElement = document.getElementById('current_header_left');
                const existingDot = menuElement ? menuElement.querySelector('.notification-dot') : null;

                if (!existingDot) {
                    console.log(`[MenuNotifications] Dot missing, restoring ${this.displayedColor} dot`);
                    this.renderDot();
                } else {
                    console.log(`[MenuNotifications] Dot already exists, no restore needed`);
                }
            }
        }, 50);
    }
};

// Initialize and make globally available
MenuNotifications.init();
window.MenuNotifications = MenuNotifications;

/**
 * Handle clicks outside dropdown to close it
 */
function handleClickOutside(event) {
    const dropdown = document.getElementById('main-dropdown-menu');
    const menuIcon = document.getElementById('current_header_left');

    if (dropdown && !dropdown.contains(event.target) && !menuIcon.contains(event.target)) {
        closeDropdownMenu();
    }
}

/**
 * Show alarms menu using LoggingManager
 */
function showAlarmsMenu() {
    if (loggingManager) {
        loggingManager.showAlertsPanel();
    } else {
        overlayMenu("Alarms", "Logging system not initialized", false);
        setTimeout(() => closeOverlayMenu(false), 2000);
    }
}

/**
 * Show override controls menu using modern full-screen overlay
 */
function showOverrideControlsMenu() {
    if (controlsManager) {
        controlsManager.showOverrideMenuFullScreen();
    } else {
        showFullScreenOverlay("Override Controls", "<div style='text-align: center; color: #888; padding: 20px;'>Controls system not initialized</div>");
        setTimeout(() => closeFullScreenOverlay(), 2000);
    }
}

/**
 * Show logs menu using LoggingManager
 */
function showLogsMenu() {
    if (loggingManager) {
        loggingManager.showLogsPanel();
    } else {
        overlayMenu("Logs", "Logging system not initialized", false);
        setTimeout(() => closeOverlayMenu(false), 2000);
    }
}

/**
 * Show info menu using modern full-screen overlay
 */
function showInfoMenu(version) {
    const header = `
        <div style="display: flex; align-items: center; gap: 10px;">
            <i class="fas fa-info-circle" style="color: #17a2b8;"></i>
            <span>EOS connect Information</span>
        </div>
    `;

    const content = `
        <div style="height: calc(100% - 20px); overflow-y: auto; margin-top: 10px; text-align: center;">
            <!-- Version Section -->
            <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 30px; margin-bottom: 25px; border-left: 4px solid #17a2b8;">
                <div style="font-size: 1.2em; color: #17a2b8; margin-bottom: 15px; font-weight: bold;">
                    <i class="fas fa-code-branch" style="margin-right: 10px;"></i>Version Information
                </div>
                <div style="font-size: 0.9em; color: #888; margin-bottom: 15px;">Currently installed version:</div>
                <div style="font-size: 1.4em; color: #fff; font-weight: bold; background-color: rgba(255,255,255,0.1); padding: 12px 20px; border-radius: 6px; display: inline-block;">
                    ${version}
                </div>
            </div>
            
            <!-- Links Section -->
            <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 30px; border-left: 4px solid #28a745;">
                <div style="font-size: 1.2em; color: #28a745; margin-bottom: 20px; font-weight: bold;">
                    <i class="fas fa-external-link-alt" style="margin-right: 10px;"></i>Project Resources
                </div>
                
                <div style="display: flex; justify-content: center; gap: 30px; flex-wrap: wrap;">
                    <!-- GitHub Repository -->
                    <a href="https://github.com/ohAnd/EOS_connect" target="_blank" 
                       style="color: inherit; text-decoration: none; display: flex; flex-direction: column; align-items: center; padding: 20px; background-color: rgba(255,255,255,0.05); border-radius: 8px; transition: all 0.3s ease; min-width: 120px;"
                       onmouseover="this.style.backgroundColor='rgba(255,255,255,0.1)'; this.style.transform='translateY(-2px)'"
                       onmouseout="this.style.backgroundColor='rgba(255,255,255,0.05)'; this.style.transform='translateY(0)'">
                        <i class="fa-brands fa-github" style="font-size: 2.5em; margin-bottom: 10px; color: #fff;"></i>
                        <span style="font-size: 0.9em; font-weight: bold;">Repository</span>
                        <span style="font-size: 0.75em; color: #888; margin-top: 5px;">Source Code</span>
                    </a>
                    
                    <!-- Changelog -->
                    <a href="https://github.com/ohAnd/ha_addons/blob/master/eos_connect/CHANGELOG.md" target="_blank" 
                       style="color: inherit; text-decoration: none; display: flex; flex-direction: column; align-items: center; padding: 20px; background-color: rgba(255,255,255,0.05); border-radius: 8px; transition: all 0.3s ease; min-width: 120px;"
                       onmouseover="this.style.backgroundColor='rgba(255,255,255,0.1)'; this.style.transform='translateY(-2px)'"
                       onmouseout="this.style.backgroundColor='rgba(255,255,255,0.05)'; this.style.transform='translateY(0)'">
                        <i class="fas fa-file-text" style="font-size: 2.5em; margin-bottom: 10px; color: #ffc107;"></i>
                        <span style="font-size: 0.9em; font-weight: bold;">Changelog</span>
                        <span style="font-size: 0.75em; color: #888; margin-top: 5px;">Version History</span>
                    </a>
                    
                    <!-- Bug Reports -->
                    <a href="https://github.com/ohAnd/EOS_connect/issues" target="_blank" 
                       style="color: inherit; text-decoration: none; display: flex; flex-direction: column; align-items: center; padding: 20px; background-color: rgba(255,255,255,0.05); border-radius: 8px; transition: all 0.3s ease; min-width: 120px;"
                       onmouseover="this.style.backgroundColor='rgba(255,255,255,0.1)'; this.style.transform='translateY(-2px)'"
                       onmouseout="this.style.backgroundColor='rgba(255,255,255,0.05)'; this.style.transform='translateY(0)'">
                        <i class="fas fa-bug" style="font-size: 2.5em; margin-bottom: 10px; color: #dc3545;"></i>
                        <span style="font-size: 0.9em; font-weight: bold;">Issues</span>
                        <span style="font-size: 0.75em; color: #888; margin-top: 5px;">Bug Reports</span>
                    </a>
                </div>
            </div>
            
            <!-- Footer -->
            <div style="margin-top: 25px; padding: 20px; color: #888; font-size: 0.8em;">
                <i class="fas fa-heart" style="color: #dc3545; margin-right: 5px;"></i>
                Made with care for the EOS ecosystem
            </div>
        </div>
    `;

    showFullScreenOverlay(header, content);
}

/**
 * Create full-screen overlay for logs with responsive margins
 */
function showFullScreenOverlay(header, content, close = true) {
    // Create overlay if it doesn't exist
    let overlay = document.getElementById('full_screen_overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'full_screen_overlay';

        // Responsive padding: very small on mobile, larger on desktop
        const paddingValue = isMobile() ? '8px' : '60px';

        overlay.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.6);
            display: none;
            z-index: 1000;
            padding: ${paddingValue};
            box-sizing: border-box;
        `;
        document.body.appendChild(overlay);
    } else {
        // Update padding if overlay already exists (responsive on resize)
        const paddingValue = isMobile() ? '8px' : '60px';
        overlay.style.padding = paddingValue;
    }

    // Create content container with responsive padding
    const headerPadding = isMobile() ? '12px 15px' : '15px 20px';
    const contentPadding = isMobile() ? '15px' : '20px';
    const borderRadius = isMobile() ? '6px' : '10px';

    overlay.innerHTML = `
        <div style="
            background-color: rgb(78, 78, 78);
            border-radius: ${borderRadius};
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            box-shadow: 0 0 20px rgba(0, 0, 0, 0.5);
        ">
            <!-- Header -->
            <div id="full_screen_header" style="
                padding: ${headerPadding};
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: ${borderRadius} ${borderRadius} 0 0;
                background-color: rgb(58, 58, 58);
                color: lightgray;
                font-weight: bold;
                display: flex;
                justify-content: space-between;
                align-items: center;
                font-size: ${isMobile() ? '1.1em' : '1em'};
            ">
                ${header}
                ${close ? `<button onclick="closeFullScreenOverlay()" style="background: none; border: none; color: lightgray; font-size: 1.5em; cursor: pointer; padding: 0; width: ${isMobile() ? '28px' : '30px'}; height: ${isMobile() ? '28px' : '30px'}; display: flex; align-items: center; justify-content: center; border-radius: 50%; transition: background-color 0.2s;" onmouseover="this.style.backgroundColor='rgba(255,255,255,0.1)'" onmouseout="this.style.backgroundColor='transparent'">×</button>` : ''}
            </div>
            
            <!-- Content -->
            <div id="full_screen_content" style="
                flex: 1;
                padding: ${contentPadding};
                overflow: auto;
                color: lightgray;
                font-size: ${isMobile() ? '0.85em' : '1em'};
            ">
                ${content}
            </div>
        </div>
    `;

    overlay.style.display = 'flex';

    // Block background scrolling
    document.body.style.overflow = 'hidden';

    // Add escape key listener
    const escapeHandler = (e) => {
        if (e.key === 'Escape') {
            closeFullScreenOverlay();
        }
    };
    document.addEventListener('keydown', escapeHandler);
    overlay.escapeHandler = escapeHandler; // Store for cleanup
}

/**
 * Close full-screen overlay
 * @param {number} waittime - Optional delay before closing (ms)
 */
function closeFullScreenOverlay(waittime = 0) {
    const overlay = document.getElementById('full_screen_overlay');
    if (overlay) {
        setTimeout(() => {
            overlay.style.display = 'none';
        }, waittime);

        // Restore background scrolling
        document.body.style.overflow = '';

        // Remove escape key listener
        if (overlay.escapeHandler) {
            document.removeEventListener('keydown', overlay.escapeHandler);
        }
        // Stop auto-refresh when closing overlay
        if (typeof loggingManager !== 'undefined' && loggingManager.stopAutoRefresh) {
            loggingManager.stopAutoRefresh();
        }
    }
}












