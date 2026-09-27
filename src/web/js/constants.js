/**
 * Constants for EOS Connect
 * Color modes and other application constants
 * Extracted from legacy index.html
 */

// Battery mode color constants
const COLOR_MODE_CHARGE_FROM_GRID = "rgb(255, 144, 144)";
const COLOR_MODE_AVOID_DISCHARGE = "lightgray";
const COLOR_MODE_DISCHARGE_ALLOWED = "lightgreen";
const COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST = "#3399FF";
const COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV = "lightgreen";
const COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV = "darkorange";
const COLOR_MODE_CHARGE_FROM_GRID_EVCC_FAST = "rgb(255, 144, 144)";
const COLOR_DYN_OVERRIDE_PV_GREATER_LOAD = "#32CD32";  // Lime green for dynamic PV>Load override

const EOS_CONNECT_ICONS = [
    { icon: "fa-plug-circle-bolt", color: COLOR_MODE_CHARGE_FROM_GRID, title: "Charge From Grid" },
    { icon: "fa-lock", color: COLOR_MODE_AVOID_DISCHARGE, title: "Avoid Discharge" },
    { icon: "fa-battery-half", color: COLOR_MODE_DISCHARGE_ALLOWED, title: "Discharge Allowed" },
    { icon: "fa-charging-station", color: COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST, title: "Avoid Discharge Due to E-Car Fast Charge" },
    { icon: "fa-charging-station", color: COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV, title: "Discharge Allowed During E-Car Charging in PV Mode" },
    { icon: "fa-charging-station", color: COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV, title: "Discharge Allowed During E-Car Charging in Min+PV Mode" },
    { icon: "fa-charging-station", color: COLOR_MODE_CHARGE_FROM_GRID_EVCC_FAST, title: "Charge From Grid During E-Car Fast Charge" },
    { icon: "fa-sun", color: COLOR_DYN_OVERRIDE_PV_GREATER_LOAD, title: "Dynamic Override (PV > Load)" }
];

// Documentation base URL.
//
// The docs are published twice: the released site at the root, and the develop
// state under /develop/ (.github/workflows/pages.yml). A develop build has to link
// into the preview copy, or its help points at docs for a release that does not
// describe the feature yet.
//
// eos_connect.py resolves which one applies from src/version.py and writes it onto
// <body data-docs-base>, so no link below has to know about branches, and nothing
// here changes when develop is merged to main. The fallback covers the test
// harness in tests/web/conftest.py and any page served without the template.
const DOCS_BASE_URL = (document.body && document.body.getAttribute("data-docs-base"))
    || "https://ohand.github.io/EOS_connect/";

/**
 * Absolute URL of a documentation page for the running build.
 * @param {string} [path] - Site-relative path, e.g. "user-guide/configuration.html".
 * @returns {string} Absolute URL, ending in "/" when path is omitted.
 */
function docsUrl(path) {
    return DOCS_BASE_URL + (path || "");
}

// Global managers - will be initialized in main.js
let controlsManager;
let scheduleManager;
let chartManager;
let statisticsManager;
let evccManager;
let batteryManager;
let loggingManager;

// Global variables for application state - matching legacy code
let myChart;
let nightPeriodStart = 22;
let nightPeriodEnd = 6;
let wattPeakPower = null;
let dataChangedSinceLastVisualization = false;
let stepwidth_min = 15;
let last_data_response;
let last_data_request;
let date_german_format = new Date().toLocaleDateString('de-DE');
let date_us_format = new Date().toISOString().split('T')[0];

// Test mode configuration - only activated with ?test=1 parameter
const urlParams = new URLSearchParams(window.location.search);
let isTestMode = urlParams.get('test') === '1';

const TEST_SCENARIOS = {
    LIVE: null,
    OVERRIDE_0: 'override_0',
    OVERRIDE_1: 'override_1',
    OVERRIDE_2: 'override_2',
    SINGLE_EVCC: 'single_evcc',
    MULTI_EVCC: 'multi_evcc',
    NO_EVCC: 'no_evcc'
};

let currentTestScenario = TEST_SCENARIOS.LIVE;

// Chart configuration constants
const CHART_CONFIG = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: {
            display: true,
            position: 'top'
        },
        title: {
            display: true,
            text: 'Energy Management'
        }
    },
    scales: {
        x: {
            type: 'time',
            time: {
                unit: 'hour',
                displayFormats: {
                    hour: 'HH:mm'
                }
            }
        },
        y: {
            beginAtZero: false
        }
    }
};

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        COLOR_MODE_CHARGE_FROM_GRID,
        COLOR_MODE_AVOID_DISCHARGE,
        COLOR_MODE_DISCHARGE_ALLOWED,
        COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST,
        COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV,
        COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV,
        CHART_CONFIG
    };
}