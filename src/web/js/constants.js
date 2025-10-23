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

const EOS_CONNECT_ICONS = [
    { icon: "fa-plug-circle-bolt", color: COLOR_MODE_CHARGE_FROM_GRID, title: "Charge From Grid" },
    { icon: "fa-lock", color: COLOR_MODE_AVOID_DISCHARGE, title: "Avoid Discharge" },
    { icon: "fa-battery-half", color: COLOR_MODE_DISCHARGE_ALLOWED, title: "Discharge Allowed" },
    { icon: "fa-charging-station", color: COLOR_MODE_AVOID_DISCHARGE_EVCC_FAST, title: "Avoid Discharge Due to E-Car Fast Charge" },
    { icon: "fa-charging-station", color: COLOR_MODE_DISCHARGE_ALLOWED_EVCC_PV, title: "Discharge Allowed During E-Car Charging in PV Mode" },
    { icon: "fa-charging-station", color: COLOR_MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV, title: "Discharge Allowed During E-Car Charging in Min+PV Mode" }
];

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

const PRICE_INFO = Object.freeze(
        typeof window !== 'undefined' ? {
            currency: "EUR",
            symbol: "€",
            minorUnit: "ct/kWh",
        } :
        {
            currency: globalWindow.SERVER_PRICE_CURRENCY,
            symbol: globalWindow.SERVER_PRICE_SYMBOL,
            minorUnit: globalWindow.SERVER_PRICE_MINOR,
        });

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
        CHART_CONFIG,
        PRICE_INFO,
    };
}
