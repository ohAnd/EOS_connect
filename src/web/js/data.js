/**
 * Data Manager for EOS Connect
 * Handles all API communication and data fetching
 */

class DataManager {
    constructor() {
        this.baseUrl = window.location.origin;
        this.cache = new Map();
        this.cacheTimeout = 5000; // 5 second cache to avoid excessive requests
    }

    /**
     * Fetch data with basic caching to reduce server load
     */
    async fetchWithCache(url, cacheKey) {
        const now = Date.now();
        const cached = this.cache.get(cacheKey);
        
        // Return cached data if still valid
        if (cached && (now - cached.timestamp) < this.cacheTimeout) {
            return cached.data;
        }

        try {
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const data = await response.json();
            
            // Cache the successful response
            this.cache.set(cacheKey, {
                data: data,
                timestamp: now
            });
            
            return data;
        } catch (error) {
            console.error(`[DataManager] Error fetching ${url}:`, error);
            
            // Return cached data if available, even if expired
            if (cached) {
                console.warn(`[DataManager] Using expired cache for ${cacheKey}`);
                return cached.data;
            }
            
            throw error;
        }
    }

    /**
     * Fetch EOS Connect data files
     * This replaces the original fetch_EOS_Connect_Data function
     * Routes test files to the dynamic test endpoint
     */
    async fetchEOSConnectData(filename) {
        // Check if this is a test file and route to dynamic test endpoint
        // All test files now follow the .test.json naming convention
        const isTestFile = filename.endsWith('.test.json');
        
        const basePath = isTestFile ? 'json/test/' : 'json/';
        const url = `${basePath}${filename}?nocache=${new Date().getTime()}`;
        return this.fetchWithCache(url, filename);
    }

    /**
     * Fetch current controls data (battery, inverter, EVCC status)
     */
    async fetchCurrentControls(testScenario = null) {
        if (testScenario) {
            return this.fetchEOSConnectData(`current_controls_${testScenario}.test.json`);
        }
        return this.fetchEOSConnectData("current_controls.json");
    }

    /**
     * Fetch optimization request data
     */
    async fetchOptimizationRequest(isTestMode = null) {
        const filename = isTestMode ? "optimize_request.test.json" : "optimize_request.json";
        // console.log(`[DataManager] Fetching optimization request: ${filename}`);
        return this.fetchEOSConnectData(filename);
    }

    /**
     * Fetch optimization response data
     */
    async fetchOptimizationResponse(isTestMode = null) {
        const filename = isTestMode ? "optimize_response.test.json" : "optimize_response.json";
        // console.log(`[DataManager] Fetching optimization response: ${filename}`);
        return this.fetchEOSConnectData(filename);
    }

    /**
     * Send override control commands to server
     */
    async setOverrideControl(controlData) {
        try {
            const response = await fetch('controls/mode_override', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(controlData)
            });

            if (!response.ok) {
                const errorMessage = await response.text();
                throw new Error(`Error ${response.status}: ${errorMessage}`);
            }

            return await response.json();
        } catch (error) {
            console.error("[DataManager] Failed to set override control:", error);
            throw error;
        }
    }

    /**
     * Fetch all data needed for initialization
     * Returns both request and response data
     */
    async fetchAllData(isTestMode = false, testScenario = null) {
        // testScenario != 'LIVE' ? console.log("[DataManager] Fetching all data in TEST mode") : null;
        try {
            const [requestData, responseData, controlsData] = await Promise.all([
                this.fetchOptimizationRequest(isTestMode),
                this.fetchOptimizationResponse(isTestMode),
                this.fetchCurrentControls(testScenario)
            ]);

            return {
                request: requestData,
                response: responseData,
                controls: controlsData
            };
        } catch (error) {
            console.error("[DataManager] Error fetching all data:", error);
            throw error;
        }
    }

    /**
     * Check if response data contains errors
     */
    hasErrorInResponse(responseData) {
        if (!responseData || 
            !responseData["result"] || 
            !responseData["result"]["Last_Wh_pro_Stunde"] || 
            responseData["result"]["Last_Wh_pro_Stunde"].length === 0) {
            return true;
        }
        return false;
    }

    /**
     * Get error information from response data
     */
    getErrorInfo(responseData) {
        if (responseData && responseData["error"]) {
            if (responseData["error"].includes("Request timed out")) {
                return {
                    title: "No processing possible - connection to EOS server timed out",
                    message: "Error: " + responseData["error"]
                };
            } else if (responseData["error"].includes("422 Client Error: Unprocessable Entity")) {
                return {
                    title: "Check your configuration! - EOS cannot process the request...",
                    message: "Error: " + responseData["error"]
                };
            } else {
                return {
                    title: "No data available...",
                    message: "no detailed error information available - error message: " + responseData["error"]
                };
            }
        } else if (responseData && responseData["status"]) {
            return {
                title: responseData["status"],
                message: responseData["message"]
            };
        } else {
            return {
                title: "Waiting for first data...",
                message: ""
            };
        }
    }

    /**
     * Clear cache (useful for forced refresh)
     */
    clearCache() {
        this.cache.clear();
        console.log("[DataManager] Cache cleared");
    }
}

// Create global data manager instance
const dataManager = new DataManager();

// Legacy compatibility function - keep for now to avoid breaking changes
async function fetch_EOS_Connect_Data(filename) {
    return dataManager.fetchEOSConnectData(filename);
}

// Legacy compatibility function for override controls
async function setOverrideControl(data_controls) {
    return dataManager.setOverrideControl(data_controls);
}