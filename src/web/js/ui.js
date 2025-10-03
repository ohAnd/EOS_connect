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
}

function closeOverlayMenu(direct = true) {
    const overlay = document.getElementById('overlay_menu');
    if (overlay.style.display === 'flex') {
        if (direct) {
            overlayMenu('', '', false);
            overlay.style.display = 'none';
        } else {
            overlay.style.transition = 'opacity 1s';
            overlay.style.opacity = '0';
            setTimeout(() => {
                overlayMenu('', '', false);
                overlay.style.display = 'none';
                overlay.style.opacity = '1';
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
document.addEventListener('DOMContentLoaded', function() {
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
        font-size: 0.9em;
    `;
    
    dropdown.innerHTML = `
        <div onclick="showAlarmsMenu(); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-triangle-exclamation" style="margin-right: 10px; color: #ff6b35; width: 16px;"></i>
            <span>Alarms</span>
        </div>
        
        <div onclick="showLogsMenu(); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-file-lines" style="margin-right: 10px; color: #4a9eff; width: 16px;"></i>
            <span>Logs</span>
        </div>
        
        <div onclick="showInfoMenu('${version}'); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <i class="fa-solid fa-circle-info" style="margin-right: 10px; color: #00d4aa; width: 16px;"></i>
            <span>Info</span>
        </div>
        
        <hr style="border: none; border-top: 1px solid rgba(255, 255, 255, 0.1); margin: 5px 0;">
        
        <div onclick="window.open('https://github.com/ohAnd/ha_addons/blob/master/eos_connect/CHANGELOG.md', '_blank'); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center; justify-content: space-between;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <div style="display: flex; align-items: center;">
                <i class="fa-solid fa-file-invoice" style="margin-right: 10px; color: #f39c12; width: 16px;"></i>
                <span>Changelog</span>
            </div>
            <i class="fa-solid fa-external-link-alt" style="font-size: 0.7em; opacity: 0.7;"></i>
        </div>
        
        <div onclick="window.open('https://github.com/ohAnd/EOS_connect/issues', '_blank'); closeDropdownMenu();" style="cursor: pointer; padding: 10px 15px; transition: background-color 0.2s; display: flex; align-items: center; justify-content: space-between;" 
            onmouseover="this.style.backgroundColor='rgba(100, 100, 100, 0.5)'" 
            onmouseout="this.style.backgroundColor='transparent'">
            <div style="display: flex; align-items: center;">
                <i class="fa-solid fa-bug" style="margin-right: 10px; color: #e74c3c; width: 16px;"></i>
                <span>Bug Report</span>
            </div>
            <i class="fa-solid fa-external-link-alt" style="font-size: 0.7em; opacity: 0.7;"></i>
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
 * Show info menu (original version info)
 */
function showInfoMenu(version) {
    const infoContent = 
        '<i style="font-size: smaller;">currently installed:</i><br><br>' + version + "<br><br>" +
        "<hr style='border-color: rgba(44, 44, 44, 0.596);'>" +
        '<div style="font-size: 2em;">' +
        '<a href="https://github.com/ohAnd/EOS_connect" target="_blank" style="padding-right: 20px; color: inherit; text-decoration: none;" title="GitHub Repository"><i class="fa-brands fa-square-github"></i></a>' +
        '<a href="https://github.com/ohAnd/ha_addons/blob/master/eos_connect/CHANGELOG.md" target="_blank" style="padding-right: 20px; color: inherit; text-decoration: none;" title="Changelog"><i class="fa-solid fa-file-invoice"></i></a>' +
        '<a href="https://github.com/ohAnd/EOS_connect/issues" target="_blank" style="color: inherit; text-decoration: none;" title="Bug Reports"><i class="fa-solid fa-bug"></i></a>' +
        "</div>";
    
    overlayMenu("Version Info", infoContent);
}

/**
 * Create full-screen overlay for logs with small margins
 */
function showFullScreenOverlay(header, content, close = true) {
    // Create overlay if it doesn't exist
    let overlay = document.getElementById('full_screen_overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'full_screen_overlay';
        overlay.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.6);
            display: none;
            z-index: 1000;
            padding: 60px;
            box-sizing: border-box;
        `;
        document.body.appendChild(overlay);
    }

    // Create content container
    overlay.innerHTML = `
        <div style="
            background-color: rgb(78, 78, 78);
            border-radius: 10px;
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            box-shadow: 0 0 20px rgba(0, 0, 0, 0.5);
        ">
            <!-- Header -->
            <div id="full_screen_header" style="
                padding: 15px 20px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 10px 10px 0 0;
                background-color: rgb(58, 58, 58);
                color: lightgray;
                font-weight: bold;
                display: flex;
                justify-content: space-between;
                align-items: center;
            ">
                ${header}
                ${close ? '<button onclick="closeFullScreenOverlay()" style="background: none; border: none; color: lightgray; font-size: 1.5em; cursor: pointer; padding: 0; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; border-radius: 50%; transition: background-color 0.2s;" onmouseover="this.style.backgroundColor=\'rgba(255,255,255,0.1)\'" onmouseout="this.style.backgroundColor=\'transparent\'">×</button>' : ''}
            </div>
            
            <!-- Content -->
            <div id="full_screen_content" style="
                flex: 1;
                padding: 20px;
                overflow: auto;
                color: lightgray;
            ">
                ${content}
            </div>
        </div>
    `;

    overlay.style.display = 'flex';
    
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
 */
function closeFullScreenOverlay() {
    const overlay = document.getElementById('full_screen_overlay');
    if (overlay) {
        overlay.style.display = 'none';
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
