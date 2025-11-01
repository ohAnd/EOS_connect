/**
 * Bug Report Manager for EOS Connect
 * Handles automated bug report generation with system data collection
 */

class BugReportManager {
    constructor() {
        this.repoOwner = 'ohAnd';
        this.repoName = 'EOS_connect'; // Using EOS_connect repo for bug reports
        this.maxBodySize = 65536; // GitHub API body size limit (~64KB)
    }

    /**
     * Show bug report popup with form
     */
    async showBugReportPopup() {
        console.log('[BugReport] Preparing bug report popup...');

        // Get version information
        let versionInfo = 'Version unknown';
        try {
            const response = await fetch('json/current_controls.json');
            if (response.ok) {
                const status = await response.json();
                if (status.eos_connect_version) {
                    versionInfo = status.eos_connect_version;
                }
            }
        } catch (error) {
            console.warn('[BugReport] Could not fetch version info:', error);
        }

        const header = `
            <div style="display: flex; align-items: center; gap: 10px;">
                <i class="fas fa-bug" style="color: #dc3545;"></i>
                <span>Create Bug Report</span>
            </div>
        `;

        const content = `
            <div style="height: calc(100% - 20px); overflow-y: auto; overflow-x: hidden; margin-top: 10px; max-width: 100%; box-sizing: border-box; word-wrap: break-word;">
                <form id="bugReportForm" style="display: flex; flex-direction: column; gap: 20px; max-width: 100%; box-sizing: border-box; word-wrap: break-word;">
                    <!-- Title Field -->
                    <div>
                        <label for="bugTitle" style="display: block; margin-bottom: 8px; font-weight: bold; color: #ddd;">
                            <i class="fas fa-heading" style="margin-right: 8px; color: #17a2b8;"></i>Issue Title *
                        </label>
                        <input type="text" 
                               id="bugTitle" 
                               placeholder="Brief description of the issue..." 
                               required
                               style="width: 100%; max-width: 100%; padding: 12px; border: 1px solid #555; border-radius: 6px; background-color: rgba(255,255,255,0.1); color: #fff; font-size: ${isMobile() ? '1em' : '0.9em'}; box-sizing: border-box; word-wrap: break-word;">
                    </div>
                    
                    <!-- Description Field -->
                    <div>
                        <label for="bugDescription" style="display: block; margin-bottom: 8px; font-weight: bold; color: #ddd;">
                            <i class="fas fa-align-left" style="margin-right: 8px; color: #28a745;"></i>Detailed Description *
                        </label>
                        <textarea id="bugDescription" 
                                  rows="${isMobile() ? '10' : '12'}" 
                                  required
                                  style="width: 100%; max-width: 100%; padding: 12px; border: 1px solid #555; border-radius: 6px; background-color: rgba(255,255,255,0.1); color: #fff; resize: vertical; font-size: ${isMobile() ? '1em' : '0.9em'}; box-sizing: border-box; word-wrap: break-word; white-space: pre-wrap;">
## 📝 Issue Description

**What happened?**
[Describe what you were doing when the issue occurred]

**Expected Behavior**
[What did you expect to happen?]

**Actual Behavior**  
[What actually happened instead?]

## 🔄 Steps to Reproduce

1. [First step]
2. [Second step]
3. [Third step]
4. [Issue occurs]

## 🌍 Environment

**EOS Connect Version:** ${versionInfo}
**When did this occur?** [Date/Time]
**How often?** [Always / Sometimes / Once]
**Impact:** [High / Medium / Low]

## 📎 Additional Context

[Add any clipboard, other context, screenshots, or relevant information here]

</textarea>
                    </div>
                    
                    <!-- System Data Selection -->
                    <div style="background-color: rgba(0,0,0,0.3); border-radius: 8px; padding: 20px; border-left: 4px solid #ffc107;">
                        <div style="font-size: 1.1em; color: #ffc107; margin-bottom: 15px; font-weight: bold;">
                            <i class="fas fa-database" style="margin-right: 8px;"></i>System Data Selection
                        </div>
                        
                        <div id="systemDataItems" style="display: flex; flex-direction: column; gap: 12px;">
                            <!-- Error/Warning Logs -->
                            <div class="data-item" style="display: flex; align-items: center; padding: 10px; background-color: rgba(255,255,255,0.05); border-radius: 6px;">
                                <input type="checkbox" id="include_errors" checked style="margin-right: 12px; transform: scale(1.2);">
                                <div style="flex: 1;">
                                    <label for="include_errors" style="color: #ff6b6b; font-weight: bold; cursor: pointer;">
                                        <i class="fas fa-exclamation-triangle" style="margin-right: 8px;"></i>Recent Errors & Warnings (Last 10)
                                    </label>
                                    <div style="font-size: 0.8em; color: #999; margin-top: 2px;">Critical issues and warnings from recent logs</div>
                                </div>
                                <button type="button" onclick="bugReportManager.previewData('errors')" style="padding: 4px 8px; background: transparent; border: 1px solid #666; border-radius: 4px; color: #ccc; cursor: pointer; font-size: 0.8em;">
                                    <i class="fas fa-eye"></i>
                                </button>
                            </div>
                            
                            <!-- Current Controls -->
                            <div class="data-item" style="display: flex; align-items: center; padding: 10px; background-color: rgba(255,255,255,0.05); border-radius: 6px;">
                                <input type="checkbox" id="include_controls" checked style="margin-right: 12px; transform: scale(1.2);">
                                <div style="flex: 1;">
                                    <label for="include_controls" style="color: #4ecdc4; font-weight: bold; cursor: pointer;">
                                        <i class="fas fa-sliders-h" style="margin-right: 8px;"></i>Current System Controls & States
                                    </label>
                                    <div style="font-size: 0.8em; color: #999; margin-top: 2px;">Current battery, inverter, and system states</div>
                                </div>
                                <div style="display: flex; gap: 4px;">
                                    <button type="button" onclick="bugReportManager.previewData('controls')" style="padding: 4px 8px; background: transparent; border: 1px solid #666; border-radius: 4px; color: #ccc; cursor: pointer; font-size: 0.8em;">
                                        <i class="fas fa-eye"></i>
                                    </button>
                                    <button type="button" onclick="bugReportManager.copyToClipboard('controls')" style="padding: 4px 8px; background: transparent; border: 1px solid #4ecdc4; border-radius: 4px; color: #4ecdc4; cursor: pointer; font-size: 0.8em;">
                                        <i class="fas fa-copy"></i>
                                    </button>
                                </div>
                            </div>
                            
                            <!-- Optimize Request -->
                            <div class="data-item" style="display: flex; align-items: center; padding: 10px; background-color: rgba(255,255,255,0.05); border-radius: 6px;">
                                <input type="checkbox" id="include_opt_request" checked style="margin-right: 12px; transform: scale(1.2);">
                                <div style="flex: 1;">
                                    <label for="include_opt_request" style="color: #a8e6cf; font-weight: bold; cursor: pointer;">
                                        <i class="fas fa-upload" style="margin-right: 8px;"></i>Last Optimization Request
                                    </label>
                                    <div style="font-size: 0.8em; color: #999; margin-top: 2px;">Parameters sent to optimization engine</div>
                                </div>
                                <div style="display: flex; gap: 4px;">
                                    <button type="button" onclick="bugReportManager.previewData('opt_request')" style="padding: 4px 8px; background: transparent; border: 1px solid #666; border-radius: 4px; color: #ccc; cursor: pointer; font-size: 0.8em;">
                                        <i class="fas fa-eye"></i>
                                    </button>
                                    <button type="button" onclick="bugReportManager.copyToClipboard('opt_request')" style="padding: 4px 8px; background: transparent; border: 1px solid #a8e6cf; border-radius: 4px; color: #a8e6cf; cursor: pointer; font-size: 0.8em;">
                                        <i class="fas fa-copy"></i>
                                    </button>
                                </div>
                            </div>
                            
                            <!-- Optimize Response -->
                            <div class="data-item" style="display: flex; align-items: center; padding: 10px; background-color: rgba(255,255,255,0.05); border-radius: 6px;">
                                <input type="checkbox" id="include_opt_response" checked style="margin-right: 12px; transform: scale(1.2);">
                                <div style="flex: 1;">
                                    <label for="include_opt_response" style="color: #ffd93d; font-weight: bold; cursor: pointer;">
                                        <i class="fas fa-download" style="margin-right: 8px;"></i>Last Optimization Response
                                    </label>
                                    <div style="font-size: 0.8em; color: #999; margin-top: 2px;">Results received from optimization engine</div>
                                </div>
                                <div style="display: flex; gap: 4px;">
                                    <button type="button" onclick="bugReportManager.previewData('opt_response')" style="padding: 4px 8px; background: transparent; border: 1px solid #666; border-radius: 4px; color: #ccc; cursor: pointer; font-size: 0.8em;">
                                        <i class="fas fa-eye"></i>
                                    </button>
                                    <button type="button" onclick="bugReportManager.copyToClipboard('opt_response')" style="padding: 4px 8px; background: transparent; border: 1px solid #ffd93d; border-radius: 4px; color: #ffd93d; cursor: pointer; font-size: 0.8em;">
                                        <i class="fas fa-copy"></i>
                                    </button>
                                </div>
                            </div>
                            
                            <!-- Recent Logs -->
                            <div class="data-item" style="display: flex; align-items: center; padding: 10px; background-color: rgba(255,255,255,0.05); border-radius: 6px;">
                                <input type="checkbox" id="include_logs" checked style="margin-right: 12px; transform: scale(1.2);">
                                <div style="flex: 1;">
                                    <label for="include_logs" style="color: #b8860b; font-weight: bold; cursor: pointer;">
                                        <i class="fas fa-file-alt" style="margin-right: 8px;"></i>Recent Log Entries (Last 200)
                                    </label>
                                    <div style="font-size: 0.8em; color: #999; margin-top: 2px;">Complete log history for debugging</div>
                                </div>
                                <button type="button" onclick="bugReportManager.previewData('logs')" style="padding: 4px 8px; background: transparent; border: 1px solid #666; border-radius: 4px; color: #ccc; cursor: pointer; font-size: 0.8em;">
                                    <i class="fas fa-eye"></i>
                                </button>
                            </div>
                        </div>
                        
                        <div style="margin-top: 15px; padding: 10px; background-color: rgba(40,167,69,0.1); border-radius: 4px; font-size: 0.85em; border: 1px solid rgba(40,167,69,0.3);">
                            <i class="fas fa-shield-alt" style="margin-right: 8px; color: #28a745;"></i>
                            <strong>Privacy:</strong> Only system configuration and error logs are included. No personal data, credentials, or sensitive information is collected. Please review the data before submission.
                        </div>
                    </div>
                    
                    <!-- Action Buttons -->
                    <div style="margin-top: 20px;">
                        <!-- Submit Options -->
                        <div style="margin-bottom: 15px;">
                            <!--
                            <p style="margin: 0 0 10px 0; color: #ddd; font-size: 0.9em;">
                                Choose how to submit your bug report:
                            </p>
                            -->
                            <!-- Instructions -->
                            <div style="background-color: rgba(23, 162, 184, 0.1); border-radius: 6px; padding: 15px; margin-bottom: 15px; border: 1px solid rgba(23, 162, 184, 0.3);">
                                <h4 style="margin: 0 0 10px 0; color: #17a2b8; font-size: 0.9em;">
                                    <i class="fas fa-info-circle" style="margin-right: 8px;"></i>How to Create the GitHub Issue:
                                </h4>
                                <ol style="margin: 0; padding-left: 20px; color: #ccc; font-size: 0.85em; line-height: 1.4;">
                                    <li>Fill out title and description above</li>
                                    <li>Check which system data should be attached below</li>
                                    <li>Click <strong>"Copy to Clipboard"</strong> to copy formatted data</li>
                                    <li>Click <strong>"Open GitHub Form"</strong> to open pre-filled issue</li>
                                    <li>Paste the clipboard content at the end of the GitHub issue description</li>
                                </ol>
                            </div>
                            
                            <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                                <button type="button" 
                                        id="copyDataBtn"
                                        onclick="bugReportManager.copySystemDataToClipboard()" 
                                        disabled
                                        style="padding: 12px 18px; border: 1px solid #ffc107; border-radius: 6px; background-color: rgba(255, 193, 7, 0.1); color: #ffc107; cursor: not-allowed; font-size: ${isMobile() ? '0.9em' : '0.85em'}; opacity: 0.6; flex: 1; min-width: 140px;">
                                    <i class="fas fa-copy" style="margin-right: 6px;"></i>Copy to Clipboard
                                    <div style="font-size: 0.8em; margin-top: 2px; color: #999;">System data</div>
                                </button>
                                <button type="button" 
                                        id="generateUrlBtn"
                                        onclick="bugReportManager.generateGitHubURL()" 
                                        disabled
                                        style="padding: 12px 18px; border: 1px solid #28a745; border-radius: 6px; background-color: rgba(40, 167, 69, 0.1); color: #28a745; cursor: not-allowed; font-size: ${isMobile() ? '0.9em' : '0.85em'}; opacity: 0.6; flex: 1; min-width: 140px;">
                                    <i class="fas fa-external-link-alt" style="margin-right: 6px;"></i>Open GitHub Form
                                    <div style="font-size: 0.8em; margin-top: 2px; color: #999;">Pre-filled</div>
                                </button>

                            </div>
                        </div>
                        
                        <!-- Cancel Button -->
                        <div style="display: flex; justify-content: center;">
                            <button type="button" 
                                    onclick="closeFullScreenOverlay()" 
                                    style="padding: 12px 24px; border: 1px solid #666; border-radius: 6px; background-color: transparent; color: #ccc; cursor: pointer; font-size: ${isMobile() ? '1em' : '0.9em'};">
                                Cancel
                            </button>
                        </div>
                    </div>
                </form>
            </div>
        `;

        showFullScreenOverlay(header, content);

        // Enable/disable the generate buttons based on form validation
        const titleInput = document.getElementById('bugTitle');
        const descriptionInput = document.getElementById('bugDescription');
        const generateUrlBtn = document.getElementById('generateUrlBtn');

        function updateButtonState() {
            const isValid = titleInput.value.trim() !== '' && descriptionInput.value.trim() !== '';

            // Update Copy button
            const copyDataBtn = document.getElementById('copyDataBtn');
            copyDataBtn.disabled = !isValid;
            copyDataBtn.style.cursor = isValid ? 'pointer' : 'not-allowed';
            copyDataBtn.style.opacity = isValid ? '1' : '0.6';

            // Update URL button
            generateUrlBtn.disabled = !isValid;
            generateUrlBtn.style.cursor = isValid ? 'pointer' : 'not-allowed';
            generateUrlBtn.style.opacity = isValid ? '1' : '0.6';
        }

        titleInput.addEventListener('input', updateButtonState);
        descriptionInput.addEventListener('input', updateButtonState);

        // Focus on title field
        setTimeout(() => titleInput.focus(), 100);
    }



    /**
     * Generate GitHub bug report using URL method (always works, no authentication)
     */
    async generateGitHubURL() {
        const titleInput = document.getElementById('bugTitle');
        const descriptionInput = document.getElementById('bugDescription');
        const generateUrlBtn = document.getElementById('generateUrlBtn');

        if (!titleInput.value.trim() || !descriptionInput.value.trim()) {
            alert('Please fill in both title and description fields.');
            return;
        }

        // Show loading state
        generateUrlBtn.innerHTML = '<i class="fas fa-spinner fa-spin" style="margin-right: 6px;"></i>Collecting...';
        generateUrlBtn.disabled = true;

        try {
            // Clean and encode description for URL - keep it simple
            let issueBody = descriptionInput.value.trim();

            // Fix markdown formatting for URL encoding
            issueBody = issueBody
                .replace(/\*\*([^*]+)\*\*/g, '**$1**')  // Fix bold formatting
                .replace(/##\s+/g, '## ')              // Fix header spacing
                .replace(/\n\s*\n\s*\n/g, '\n\n')      // Remove excessive newlines
                .replace(/\[([^\]]+)\]/g, '$1');       // Remove placeholder brackets

            // Keep the description clean for GitHub URL

            // Update button text
            generateUrlBtn.innerHTML = '<i class="fas fa-spinner fa-spin" style="margin-right: 6px;"></i>Opening...';

            // Open GitHub with pre-filled form and bug label
            this.openGitHubIssueURL(titleInput.value.trim(), issueBody);

            // Close the popup after a brief delay
            setTimeout(() => {
                closeFullScreenOverlay();
            }, 1000);

        } catch (error) {
            console.error('[BugReport] Error generating URL bug report:', error);

            // Show user-friendly error message
            const errorMessage = error.message.includes('fetch')
                ? 'Unable to collect system data. Opening GitHub form without system data.'
                : 'Error collecting system data. Opening GitHub form with basic information.';

            alert(errorMessage);

            // Fallback: Open GitHub with just title and cleaned description
            let fallbackBody = descriptionInput.value.trim()
                .replace(/\[([^\]]+)\]/g, '$1')  // Remove placeholder brackets
                .replace(/\n\s*\n\s*\n/g, '\n\n'); // Clean excessive newlines
            this.openGitHubIssueURL(titleInput.value.trim(), fallbackBody);

            // Close popup
            setTimeout(() => {
                closeFullScreenOverlay();
            }, 1000);
        }
    }

    /**
     * Collect system data for bug report using existing managers
     */
    async collectSystemData() {
        const data = {
            timestamp: new Date().toISOString(),
            version: null,
            currentControls: null,
            optimizeRequest: null,
            optimizeResponse: null,
            recentLogs: null,
            alerts: null,
            errors: []
        };

        try {
            // Get version from global if available
            if (typeof window.eosConnectVersion !== 'undefined') {
                data.version = window.eosConnectVersion;
            }

            // Use DataManager for JSON data collection
            const promises = [];

            // Current controls using existing DataManager
            promises.push(
                dataManager.fetchCurrentControls()
                    .then(result => {
                        data.currentControls = result;
                        if (result.eos_connect_version) {
                            data.version = result.eos_connect_version;
                        }
                    })
                    .catch(error => {
                        data.errors.push(`Error fetching current_controls.json: ${error.message}`);
                    })
            );

            // Optimization request using existing DataManager
            promises.push(
                dataManager.fetchOptimizationRequest()
                    .then(result => {
                        data.optimizeRequest = result;
                    })
                    .catch(error => {
                        data.errors.push(`Error fetching optimize_request.json: ${error.message}`);
                    })
            );

            // Optimization response using existing DataManager
            promises.push(
                dataManager.fetchOptimizationResponse()
                    .then(result => {
                        data.optimizeResponse = result;
                    })
                    .catch(error => {
                        data.errors.push(`Error fetching optimize_response.json: ${error.message}`);
                    })
            );

            // Recent logs using existing LoggingManager
            promises.push(
                (async () => {
                    try {
                        // Check if loggingManager is available
                        if (typeof loggingManager !== 'undefined' && loggingManager.fetchLogs) {
                            const logData = await loggingManager.fetchLogs(null, 100);
                            data.recentLogs = logData;
                        } else {
                            // Fallback to direct fetch if loggingManager not available
                            const response = await fetch('/logs?limit=100&nocache=' + Date.now());
                            if (response.ok) {
                                data.recentLogs = await response.json();
                            } else {
                                throw new Error(`HTTP ${response.status}`);
                            }
                        }
                    } catch (error) {
                        data.errors.push(`Error fetching logs: ${error.message}`);
                    }
                })()
            );

            // Alerts using existing LoggingManager
            promises.push(
                (async () => {
                    try {
                        if (typeof loggingManager !== 'undefined' && loggingManager.fetchAlerts) {
                            const alertData = await loggingManager.fetchAlerts();
                            data.alerts = alertData.alerts || loggingManager.alerts || [];
                        } else {
                            // Fallback to direct fetch
                            const response = await fetch('/logs/alerts?nocache=' + Date.now());
                            if (response.ok) {
                                const alertData = await response.json();
                                data.alerts = alertData.alerts || [];
                            } else {
                                throw new Error(`HTTP ${response.status}`);
                            }
                        }
                    } catch (error) {
                        data.errors.push(`Error fetching alerts: ${error.message}`);
                    }
                })()
            );

            // Wait for all data collection to complete
            await Promise.allSettled(promises);

            console.log(`[BugReport] Data collection completed. ${data.errors.length} errors occurred.`);
            return data;

        } catch (error) {
            console.error('[BugReport] Critical error in collectSystemData:', error);
            data.errors.push(`Critical error: ${error.message}`);
            return data; // Return partial data instead of throwing
        }
    }

    /**
     * Generate GitHub issue body with system data (optimized for larger size limits)
     */
    generateIssueBody(description, systemData) {
        let body = '';

        // Add user description
        body += '## Description\\n\\n';
        body += description + '\\n\\n';

        // Add system information
        body += '## System Information\\n\\n';
        if (systemData.version) {
            body += `**EOS Connect Version:** ${systemData.version}\\n`;
        }
        body += `**Report Generated:** ${systemData.timestamp}\\n`;
        if (systemData.errors.length > 0) {
            body += `**Data Collection Errors:** ${systemData.errors.length} error(s) occurred\\n`;
        }
        body += '\\n';

        // Add system data as collapsed sections with size monitoring
        body += '## System Data\\n\\n';
        let currentSize = body.length;
        const sizeLimit = this.maxBodySize - 1000; // Reserve space for footer and safety margin

        // Helper function to add section if it fits
        const addSectionIfFits = (sectionTitle, sectionData, formatAsJson = true) => {
            let sectionContent = `<details>\\n<summary>${sectionTitle}</summary>\\n\\n`;
            if (formatAsJson) {
                sectionContent += '```json\\n' + JSON.stringify(sectionData, null, 2) + '\\n```\\n\\n';
            } else {
                sectionContent += '```\\n' + sectionData + '\\n```\\n\\n';
            }
            sectionContent += '</details>\\n\\n';

            if (currentSize + sectionContent.length < sizeLimit) {
                body += sectionContent;
                currentSize += sectionContent.length;
                return true;
            }
            return false;
        };

        // Current Controls (highest priority)
        if (systemData.currentControls) {
            if (!addSectionIfFits('Current Controls & States', systemData.currentControls, true)) {
                // Try with just essential data
                const essentialControls = {
                    current_states: systemData.currentControls.current_states || {},
                    battery: systemData.currentControls.battery || {},
                    timestamp: systemData.currentControls.timestamp,
                    eos_connect_version: systemData.currentControls.eos_connect_version
                };
                addSectionIfFits('Current Controls & States (Essential)', essentialControls, true);
            }
        }

        // Recent Error/Warning Alerts (high priority) - use LoggingManager alerts
        if (systemData.alerts) {
            const errorAlerts = systemData.alerts.filter(alert =>
                alert.level === 'ERROR' || alert.level === 'WARNING'
            );

            if (errorAlerts.length > 0) {
                let alertText = '';
                errorAlerts.slice(-50).forEach(alert => { // Get last 50 error/warning alerts
                    alertText += `${alert.timestamp} [${alert.level}] ${alert.message}\\n`;
                });
                addSectionIfFits('Recent Error/Warning Alerts', alertText, false);
            }
        }

        // Recent Logs (lower priority - general logs)
        if (systemData.recentLogs && systemData.recentLogs.logs && currentSize < sizeLimit * 0.7) {
            let allLogText = '';
            systemData.recentLogs.logs.slice(0, 30).forEach(log => { // Reduced to 30 entries
                allLogText += `${log.timestamp} [${log.level}] ${log.message}\\n`;
            });
            addSectionIfFits('Recent Logs (Last 30 entries)', allLogText, false);
        }

        // Optimization data (medium priority)
        if (systemData.optimizeResponse) {
            addSectionIfFits('Last Optimization Response', systemData.optimizeResponse, true);
        }

        if (systemData.optimizeRequest) {
            addSectionIfFits('Last Optimization Request', systemData.optimizeRequest, true);
        }

        // Data collection errors (if any)
        if (systemData.errors.length > 0) {
            const errorText = systemData.errors.join('\\n');
            addSectionIfFits('Data Collection Errors', errorText, false);
        }

        // Add size information
        body += `\\n**Data Size Info:** ${Math.round(currentSize / 1024 * 10) / 10}KB / ${Math.round(sizeLimit / 1024)}KB limit\\n\\n`;

        // Add footer
        body += '---\\n';
        body += '*This bug report was generated automatically by EOS Connect\'s built-in reporting feature.*';

        return body;
    }

    /**
     * Generate truncated GitHub issue body for cases where full data is too large
     */
    generateTruncatedIssueBody(description, systemData, isUrlMode = false) {
        let body = '';

        // Add user description
        body += '## Description\\n\\n';
        body += description + '\\n\\n';

        // Add system information
        body += '## System Information\\n\\n';
        if (systemData.version) {
            body += `**EOS Connect Version:** ${systemData.version}\\n`;
        }
        body += `**Report Generated:** ${systemData.timestamp}\\n`;
        if (systemData.errors.length > 0) {
            body += `**Data Collection Errors:** ${systemData.errors.length} error(s) occurred\\n`;
        }
        body += '\\n';

        // Add truncated system data
        body += '## System Data (Truncated)\\n\\n';
        const sizeNote = isUrlMode
            ? '_Note: System data was truncated for URL length limitations. For complete data, please use the "Auto-Create Issue" option._\\n\\n'
            : '_Note: System data was truncated due to size limitations. Please check the application logs for full details._\\n\\n';
        body += sizeNote;

        // Add basic system info only
        if (systemData.currentControls) {
            const basicInfo = {
                current_states: systemData.currentControls.current_states || {},
                battery: systemData.currentControls.battery || {},
                timestamp: systemData.currentControls.timestamp,
                eos_connect_version: systemData.currentControls.eos_connect_version
            };
            body += '<details>\\n<summary>Basic System States</summary>\\n\\n';
            body += '```json\\n' + JSON.stringify(basicInfo, null, 2) + '\\n```\\n\\n';
            body += '</details>\\n\\n';
        }

        // Add only recent error alerts (using LoggingManager alerts)
        if (systemData.alerts && !isUrlMode) {
            const errorAlerts = systemData.alerts.filter(alert =>
                alert.level === 'ERROR' || alert.level === 'WARNING'
            ).slice(-20); // Get last 20 error/warning alerts

            if (errorAlerts.length > 0) {
                body += '<details>\\n<summary>Recent Error/Warning Alerts (Last 20)</summary>\\n\\n';
                body += '```\\n';
                errorAlerts.forEach(alert => {
                    body += `${alert.timestamp} [${alert.level}] ${alert.message}\\n`;
                });
                body += '```\\n\\n';
                body += '</details>\\n\\n';
            }
        } else if (systemData.alerts && isUrlMode) {
            // For URL mode, only show count of errors
            const errorAlerts = systemData.alerts.filter(alert =>
                alert.level === 'ERROR' || alert.level === 'WARNING'
            );

            if (errorAlerts.length > 0) {
                body += `**Recent Errors:** ${errorAlerts.length} error/warning entries in alerts\\n\\n`;
            }
        }

        // Add data collection errors if any
        if (systemData.errors.length > 0) {
            body += '<details>\\n<summary>Data Collection Errors</summary>\\n\\n';
            body += '```\\n';
            systemData.errors.forEach(error => {
                body += error + '\\n';
            });
            body += '```\\n\\n';
            body += '</details>\\n\\n';
        }

        // Add footer
        body += '---\\n';
        body += '*This bug report was generated automatically by EOS Connect\'s built-in reporting feature.*\\n';
        body += '*Full system data was truncated due to GitHub URL length limitations.*';

        return body;
    }

    /**
     * Create GitHub issue using OAuth authentication or fallback methods
     */
    async createGitHubIssue(title, body) {
        try {
            console.log('[BugReport] Attempting to create GitHub issue...');

            // Check if we have a stored OAuth token
            let accessToken = this.getStoredGitHubToken();

            // Try to create issue with current token (if any)
            let response = await this.attemptIssueCreation(title, body, accessToken);

            if (response && response.ok) {
                const result = await response.json();
                console.log('[BugReport] GitHub issue created successfully');
                console.log('[BugReport] Issue URL:', result.html_url);

                // Open the created issue
                window.open(result.html_url, '_blank');
                return true;
            }

            // Handle authentication required
            if (response && response.status === 401) {
                const result = await response.json();

                if (result.auth_required) {
                    console.log('[BugReport] Authentication required, starting OAuth flow...');

                    // Try OAuth authentication
                    const newToken = await this.authenticateWithGitHub();
                    if (newToken) {
                        // Retry issue creation with new token
                        const retryResponse = await this.attemptIssueCreation(title, body, newToken);
                        if (retryResponse && retryResponse.ok) {
                            const retryResult = await retryResponse.json();
                            console.log('[BugReport] GitHub issue created successfully after authentication');
                            window.open(retryResult.html_url, '_blank');
                            return true;
                        }
                    }
                }
            }

            // Server proxy not configured - try URL method
            if (response && response.status === 503) {
                const result = await response.json();
                console.log('[BugReport] Server proxy not configured:', result.message);
                console.log('[BugReport] Falling back to URL method...');
                this.openGitHubIssueURL(title, body);
                return true;
            }

            // If we get here, API methods failed - try URL method
            console.log('[BugReport] API methods failed, trying URL method...');
            this.openGitHubIssueURL(title, body);
            return true;

        } catch (error) {
            console.error('[BugReport] Error in createGitHubIssue:', error);

            // Final fallback to issues overview page
            console.log('[BugReport] All methods failed, redirecting to issues overview...');
            this.openGitHubIssuesOverview();
            return false;
        }
    }

    /**
     * Attempt to create GitHub issue with given token
     */
    async attemptIssueCreation(title, body, accessToken = null) {
        const payload = {
            title: title,
            body: body,
            repo: `${this.repoOwner}/${this.repoName}`
        };

        if (accessToken) {
            payload.access_token = accessToken;
        }

        return await fetch('/api/github/issues', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload)
        });
    }

    /**
     * Authenticate user with GitHub using Device Flow (zero setup required)
     */
    async authenticateWithGitHub() {
        try {
            console.log('[BugReport] Starting GitHub Device Flow authentication...');

            // Start GitHub Device Flow
            const authResponse = await fetch('/api/github/auth/start');
            if (!authResponse.ok) {
                throw new Error('Failed to start GitHub authentication');
            }

            const deviceData = await authResponse.json();

            // Show device code to user
            const authPromise = new Promise((resolve) => {
                // Create modal with device code instructions
                const authModal = `
                    <div style="text-align: center; padding: 20px;">
                        <h2 style="color: #ffc107; margin-bottom: 20px;">
                            <i class="fab fa-github" style="margin-right: 10px;"></i>
                            GitHub Authentication Required
                        </h2>
                        <div style="background: rgba(0,0,0,0.3); padding: 20px; border-radius: 8px; margin: 20px 0;">
                            <p style="font-size: 1.1em; margin-bottom: 15px;">To create a bug report, please authorize EOS Connect:</p>
                            <div style="margin: 15px 0;">
                                <strong style="font-size: 1.2em; color: #17a2b8;">Verification Code:</strong>
                                <div style="font-family: monospace; font-size: 1.5em; color: #ffc107; margin: 10px 0; padding: 10px; background: rgba(255,255,255,0.1); border-radius: 4px;">
                                    ${deviceData.user_code}
                                </div>
                            </div>
                        </div>
                        <div style="margin: 20px 0;">
                            <button onclick="window.open('${deviceData.verification_uri}', '_blank')" 
                                    style="padding: 15px 30px; font-size: 1.1em; background: #28a745; color: white; border: none; border-radius: 6px; cursor: pointer; margin: 10px;">
                                <i class="fab fa-github" style="margin-right: 8px;"></i>Open GitHub Authorization
                            </button>
                        </div>
                        <div style="color: #ccc; font-size: 0.9em;">
                            <p>1. Click "Open GitHub Authorization"</p>
                            <p>2. Sign in to GitHub if needed</p>
                            <p>3. Enter the verification code: <strong>${deviceData.user_code}</strong></p>
                            <p>4. Authorize EOS Connect</p>
                            <p><em>This window will close automatically once authorized</em></p>
                        </div>
                        <div style="margin-top: 20px;">
                            <button onclick="bugReportManager.cancelGitHubAuth()" 
                                    style="padding: 10px 20px; background: transparent; color: #ccc; border: 1px solid #666; border-radius: 4px; cursor: pointer;">
                                Cancel
                            </button>
                        </div>
                        <div id="authStatus" style="margin-top: 15px; font-style: italic; color: #17a2b8;">
                            Waiting for authorization...
                        </div>
                    </div>
                `;

                showFullScreenOverlay(
                    '<div style="display: flex; align-items: center; gap: 10px;"><i class="fab fa-github" style="color: #ffc107;"></i><span>GitHub Authentication</span></div>',
                    authModal
                );

                // Start polling for authentication
                this.pollGitHubAuth(deviceData.device_code, resolve);
            });

            return await authPromise;

        } catch (error) {
            console.error('[BugReport] GitHub authentication error:', error);
            return null;
        }
    }

    /**
     * Poll GitHub for device flow completion
     */
    async pollGitHubAuth(deviceCode, resolve) {
        const maxAttempts = 60; // 5 minutes with 5-second intervals
        let attempts = 0;
        let pollInterval = 5000; // Start with 5 seconds

        const poll = async () => {
            if (attempts >= maxAttempts) {
                document.getElementById('authStatus').textContent = 'Authentication timeout. Please try again.';
                setTimeout(() => resolve(null), 2000);
                return;
            }

            try {
                const response = await fetch('/api/github/auth/poll', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_code: deviceCode })
                });

                const result = await response.json();

                if (result.status === 'success') {
                    document.getElementById('authStatus').innerHTML = '<span style="color: #28a745;"><i class="fas fa-check"></i> Authentication successful!</span>';
                    this.storeGitHubToken(result.access_token);
                    setTimeout(() => {
                        closeFullScreenOverlay();
                        resolve(result.access_token);
                    }, 1000);
                    return;
                }

                if (result.status === 'slow_down') {
                    pollInterval += 2000; // Slow down polling
                }

                attempts++;
                setTimeout(poll, pollInterval);

            } catch (error) {
                console.error('[BugReport] Polling error:', error);
                attempts++;
                setTimeout(poll, pollInterval);
            }
        };

        // Start polling
        setTimeout(poll, 1000);
    }

    /**
     * Cancel GitHub authentication
     */
    cancelGitHubAuth() {
        closeFullScreenOverlay();
    }

    /**
     * Store GitHub token in session storage
     */
    storeGitHubToken(token) {
        try {
            sessionStorage.setItem('github_access_token', token);
        } catch (error) {
            console.warn('[BugReport] Could not store GitHub token:', error);
        }
    }

    /**
     * Get stored GitHub token from session storage
     */
    getStoredGitHubToken() {
        try {
            return sessionStorage.getItem('github_access_token');
        } catch (error) {
            console.warn('[BugReport] Could not retrieve GitHub token:', error);
            return null;
        }
    }

    /**
     * Open GitHub issues overview page when URL method fails
     */
    openGitHubIssuesOverview() {
        const issuesUrl = `https://github.com/${this.repoOwner}/${this.repoName}/issues`;

        console.log('[BugReport] Opening GitHub issues overview due to technical difficulties...');

        // Show user-friendly message
        alert('There was a technical issue creating the pre-filled bug report.\n\n' +
            'You will be redirected to the GitHub issues page where you can:\n' +
            '1. Click "New issue" to create a manual report\n' +
            '2. Include the system data from your EOS Connect web interface\n' +
            '3. Check the Logs section and JSON endpoints for debugging data');

        window.open(issuesUrl, '_blank');
    }

    /**
     * Open GitHub issue URL with pre-filled data (fallback method)
     */
    openGitHubIssueURL(title, body) {
        const baseUrl = `https://github.com/${this.repoOwner}/${this.repoName}/issues/new`;

        // For very large bodies, we'll create a more structured approach
        if (body.length > 8000) {
            // Create a truncated version for URL and provide instructions
            const truncatedBody = this.generateUrlSafeBody(title, body);
            const params = new URLSearchParams({
                title: title,
                body: truncatedBody,
                labels: 'bug'
            });

            const githubUrl = `${baseUrl}?${params.toString()}`;
            console.log('[BugReport] Opening GitHub with truncated data due to URL limits...');
            window.open(githubUrl, '_blank');
        } else {
            const params = new URLSearchParams({
                title: title,
                body: body,
                labels: 'bug'
            });

            const githubUrl = `${baseUrl}?${params.toString()}`;
            console.log('[BugReport] Opening GitHub issue URL...');
            window.open(githubUrl, '_blank');
        }
    }

    /**
     * Generate a URL-safe body that fits within GitHub's URL limits
     */
    generateUrlSafeBody(title, fullBody) {
        const maxUrlBodyLength = 6000; // Conservative limit for URL

        if (fullBody.length <= maxUrlBodyLength) {
            return fullBody;
        }

        // Create a summary version that fits in URL
        const lines = fullBody.split('\\n');
        let safebody = '';
        let currentLength = 0;

        // Always include description section
        const descriptionEndIndex = lines.findIndex(line => line.startsWith('## System Information'));
        if (descriptionEndIndex > 0) {
            const descriptionLines = lines.slice(0, descriptionEndIndex);
            safebody = descriptionLines.join('\\n') + '\\n\\n';
            currentLength = safebody.length;
        }

        // Add system info
        safebody += '## System Information\\n\\n';
        safebody += '_System data was truncated due to URL length limitations._\\n';
        safebody += '_Full system data is available in EOS Connect logs and web interface._\\n\\n';

        // Add instructions for full data
        safebody += '## Full System Data\\n\\n';
        safebody += 'To provide complete system data for debugging:\\n';
        safebody += '1. Access your EOS Connect web interface\\n';
        safebody += '2. Go to Logs section and export recent logs\\n';
        safebody += '3. Check JSON endpoints: `/json/current_controls.json`, `/json/optimize_request.json`, `/json/optimize_response.json`\\n';
        safebody += '4. Attach the relevant files to this issue\\n\\n';

        safebody += '---\\n';
        safebody += '_This bug report was generated automatically by EOS Connect. Full data truncated due to URL limitations._';

        return safebody;
    }

    /**
     * Show preview in a smaller modal that doesn't close the main form
     */
    showPreviewModal(title, content) {
        // Remove existing preview modal if any
        const existingModal = document.getElementById('bugReportPreviewModal');
        if (existingModal) {
            existingModal.remove();
        }

        // Create modal HTML
        const modal = document.createElement('div');
        modal.id = 'bugReportPreviewModal';
        modal.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 10000;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            box-sizing: border-box;
        `;

        modal.innerHTML = `
            <div style="
                background: #2d2d30;
                border-radius: 8px;
                max-width: 90vw;
                max-height: 90vh;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                color: #fff;
                box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            ">
                <div style="
                    padding: 20px;
                    border-bottom: 1px solid #404040;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                ">
                    <h3 style="margin: 0; color: #fff; font-size: 1.1em;">${title}</h3>
                    <button onclick="this.closest('#bugReportPreviewModal').remove()" style="
                        background: transparent;
                        border: none;
                        color: #ccc;
                        font-size: 1.5em;
                        cursor: pointer;
                        padding: 5px;
                    ">×</button>
                </div>
                <div style="
                    padding: 20px;
                    overflow-y: auto;
                    flex: 1;
                    max-height: calc(90vh - 80px);
                ">
                    ${content}
                </div>
            </div>
        `;

        // Add to document
        document.body.appendChild(modal);

        // Close on background click
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.remove();
            }
        });
    }

    /**
     * Create GitHub issue URL with pre-filled data (legacy support)
     */
    createGitHubIssueURL(title, body) {
        const baseUrl = `https://github.com/${this.repoOwner}/${this.repoName}/issues/new`;
        const params = new URLSearchParams({
            title: title,
            body: body
        });

        return `${baseUrl}?${params.toString()}`;
    }

    /**
     * Copy all selected system data to clipboard
     */
    async copySystemDataToClipboard() {
        const copyBtn = document.getElementById('copyDataBtn');

        try {
            // Show loading state
            copyBtn.innerHTML = '<i class="fas fa-spinner fa-spin" style="margin-right: 6px;"></i>Copying...';
            copyBtn.disabled = true;

            // Collect system data
            console.log('[BugReport] Collecting system data for clipboard...');
            const systemData = await this.collectSystemData();

            // Generate markdown content based on selections
            const markdownContent = this.generateMarkdownFromSelections(systemData);

            // Copy to clipboard with fallback
            let success = false;
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(markdownContent);
                    success = true;
                } else {
                    // Fallback for older browsers
                    const textArea = document.createElement('textarea');
                    textArea.value = markdownContent;
                    document.body.appendChild(textArea);
                    textArea.select();
                    success = document.execCommand('copy');
                    document.body.removeChild(textArea);
                }
            } catch (error) {
                console.warn('[BugReport] Clipboard API failed, trying fallback:', error);
                // Fallback method
                const textArea = document.createElement('textarea');
                textArea.value = markdownContent;
                document.body.appendChild(textArea);
                textArea.select();
                success = document.execCommand('copy');
                document.body.removeChild(textArea);
            }

            if (!success) {
                throw new Error('All clipboard methods failed');
            }

            // Show success state
            copyBtn.innerHTML = '<i class="fas fa-check" style="margin-right: 6px;"></i>Copied!';
            copyBtn.style.background = 'rgba(40, 167, 69, 0.2)';
            copyBtn.style.borderColor = '#28a745';
            copyBtn.style.color = '#28a745';

            // Reset button after 2 seconds
            setTimeout(() => {
                copyBtn.innerHTML = '<i class="fas fa-copy" style="margin-right: 6px;"></i>Copy to Clipboard';
                copyBtn.style.background = 'rgba(255, 193, 7, 0.1)';
                copyBtn.style.borderColor = '#ffc107';
                copyBtn.style.color = '#ffc107';
                copyBtn.disabled = false;
            }, 2000);

        } catch (error) {
            console.error('[BugReport] Error copying to clipboard:', error);
            alert('Failed to copy to clipboard. Please try again or copy manually.');

            // Reset button
            copyBtn.innerHTML = '<i class="fas fa-copy" style="margin-right: 6px;"></i>Copy to Clipboard';
            copyBtn.disabled = false;
        }
    }

    /**
     * Generate markdown content from selected data items
     */
    generateMarkdownFromSelections(systemData) {
        let markdown = '\n\n---\n\n## 🔧 System Data\n\n';

        // Check which items are selected
        const includeErrors = document.getElementById('include_errors').checked;
        const includeControls = document.getElementById('include_controls').checked;
        const includeOptRequest = document.getElementById('include_opt_request').checked;
        const includeOptResponse = document.getElementById('include_opt_response').checked;
        const includeLogs = document.getElementById('include_logs').checked;

        // Add errors/warnings first (most important) - use alerts like in preview
        if (includeErrors && systemData.alerts) {
            // Use same logic as preview - filter alerts for ERROR and WARNING only
            const errorAlerts = systemData.alerts.filter(alert =>
                alert.level === 'ERROR' || alert.level === 'WARNING'
            ).slice(-10); // Get last 10 error/warning alerts

            if (errorAlerts.length > 0) {
                markdown += `### ⚠️ Recent Errors & Warnings (${errorAlerts.length} found)\n\n`;
                markdown += '```\n';
                errorAlerts.forEach(alert => {
                    markdown += `${alert.timestamp} [${alert.level}] ${alert.message}\n`;
                });
                markdown += '```\n\n';
            } else {
                markdown += '### ✅ Recent Errors & Warnings\n\nNo recent errors or warnings found in alerts.\n\n';
            }
        } else if (includeErrors) {
            markdown += '### ❌ Recent Errors & Warnings\n\nAlerts data not available.\n\n';
        }

        // Add current controls
        if (includeControls && systemData.currentControls) {
            markdown += '### 🎛️ Current System Controls & States\n\n';
            markdown += '<details>\n<summary>Click to expand system controls</summary>\n\n';
            markdown += '```json\n';
            markdown += JSON.stringify(systemData.currentControls, null, 2);
            markdown += '\n```\n\n';
            markdown += '</details>\n\n';
        }

        // Add optimization request
        if (includeOptRequest && systemData.optimizeRequest) {
            markdown += '### 📤 Last Optimization Request\n\n';
            markdown += '<details>\n<summary>Click to expand optimization request</summary>\n\n';
            markdown += '```json\n';
            markdown += JSON.stringify(systemData.optimizeRequest, null, 2);
            markdown += '\n```\n\n';
            markdown += '</details>\n\n';
        }

        // Add optimization response
        if (includeOptResponse && systemData.optimizeResponse) {
            markdown += '### 📥 Last Optimization Response\n\n';
            markdown += '<details>\n<summary>Click to expand optimization response</summary>\n\n';
            markdown += '```json\n';
            markdown += JSON.stringify(systemData.optimizeResponse, null, 2);
            markdown += '\n```\n\n';
            markdown += '</details>\n\n';
        }

        // Add recent logs
        if (includeLogs && systemData.recentLogs && systemData.recentLogs.logs) {
            const recentLogs = systemData.recentLogs.logs.slice(0, 200);
            markdown += '### 📋 Recent Log Entries (Last 200)\n\n';
            markdown += '<details>\n<summary>Click to expand recent logs</summary>\n\n';
            markdown += '```\n';
            recentLogs.forEach(log => {
                markdown += `${log.timestamp} [${log.level}] ${log.message}\n`;
            });
            markdown += '```\n\n';
            markdown += '</details>\n\n';
        }

        // Add footer
        markdown += '---\n';
        markdown += '*This system data was generated automatically by EOS Connect bug reporting feature.*';

        return markdown;
    }

    /**
     * Preview data in a popup
     */
    async previewData(dataType) {
        try {
            console.log(`[BugReport] Previewing ${dataType} data...`);

            // Collect system data if not already available
            if (!this.cachedSystemData) {
                this.cachedSystemData = await this.collectSystemData();
            }

            let content = '';
            let title = '';

            switch (dataType) {
                case 'errors':
                    title = '⚠️ Recent Errors & Warnings (Last 10)';
                    try {
                        // Use LoggingManager alerts directly - they contain ERROR and WARNING levels
                        if (typeof loggingManager !== 'undefined' && loggingManager.fetchAlerts) {
                            await loggingManager.fetchAlerts();
                            const alerts = loggingManager.alerts || [];

                            const errorAlerts = alerts
                                .filter(alert => alert.level === 'ERROR' || alert.level === 'WARNING')
                                .slice(-10); // Get most recent 10

                            if (errorAlerts.length > 0) {
                                title = `⚠️ Recent Errors & Warnings (${errorAlerts.length} found)`;
                                content = errorAlerts.map(alert =>
                                    `<div style="margin-bottom: 8px; padding: 8px; background: rgba(255,0,0,0.1); border-radius: 4px; word-wrap: break-word;">
                                        <strong>[${alert.level}]</strong> ${alert.timestamp}<br>
                                        <span style="color: #ff6b6b; word-wrap: break-word;">${alert.message}</span>
                                    </div>`
                                ).join('');
                            } else {
                                content = '<div style="color: #28a745;">✅ No recent errors or warnings found in alerts.</div>';
                            }
                        } else {
                            content = '<div style="color: #dc3545;">❌ LoggingManager not available.</div>';
                        }
                    } catch (error) {
                        console.error('[BugReport] Error fetching alerts:', error);
                        content = '<div style="color: #dc3545;">❌ Error loading alerts data.</div>';
                    }
                    break;

                case 'controls':
                    title = '🎛️ Current System Controls & States';
                    content = `<pre style="background: #222; padding: 15px; border-radius: 6px; overflow: auto; max-height: 400px; color: #fff; word-wrap: break-word; white-space: pre-wrap;">${JSON.stringify(this.cachedSystemData.currentControls || {}, null, 2)}</pre>`;
                    break;

                case 'opt_request':
                    title = '📤 Last Optimization Request';
                    content = `<pre style="background: #222; padding: 15px; border-radius: 6px; overflow: auto; max-height: 400px; color: #fff; word-wrap: break-word; white-space: pre-wrap;">${JSON.stringify(this.cachedSystemData.optimizeRequest || {}, null, 2)}</pre>`;
                    break;

                case 'opt_response':
                    title = '📥 Last Optimization Response';
                    content = `<pre style="background: #222; padding: 15px; border-radius: 6px; overflow: auto; max-height: 400px; color: #fff; word-wrap: break-word; white-space: pre-wrap;">${JSON.stringify(this.cachedSystemData.optimizeResponse || {}, null, 2)}</pre>`;
                    break;

                case 'logs':
                    title = '📋 Recent Log Entries (Last 200)';
                    if (this.cachedSystemData.recentLogs && this.cachedSystemData.recentLogs.logs) {
                        const recentLogs = this.cachedSystemData.recentLogs.logs.slice(0, 200);
                        content = `<div style="background: #222; padding: 15px; border-radius: 6px; overflow: auto; max-height: 400px; color: #fff; font-family: monospace; font-size: 0.85em; word-wrap: break-word; white-space: pre-wrap;">
                            ${recentLogs.map(log => `${log.timestamp} [${log.level}] ${log.message}`).join('<br>')}
                        </div>`;
                    } else {
                        content = '<div style="color: #999;">No log data available.</div>';
                    }
                    break;

                default:
                    title = 'Preview';
                    content = '<div style="color: #999;">Unknown data type.</div>';
            }

            // Show preview in smaller modal
            this.showPreviewModal(title, content);

        } catch (error) {
            console.error(`[BugReport] Error previewing ${dataType}:`, error);
            alert(`Failed to preview ${dataType} data. Please try again.`);
        }
    }

    /**
     * Copy specific data type to clipboard (formatted as markdown)
     */
    async copyToClipboard(dataType) {
        try {
            console.log(`[BugReport] Copying ${dataType} to clipboard...`);

            // Collect system data if not already available
            if (!this.cachedSystemData) {
                this.cachedSystemData = await this.collectSystemData();
            }

            let markdown = '';

            switch (dataType) {
                case 'controls':
                    markdown = '### 🎛️ Current System Controls & States\n\n```json\n';
                    markdown += JSON.stringify(this.cachedSystemData.currentControls || {}, null, 2);
                    markdown += '\n```';
                    break;

                case 'opt_request':
                    markdown = '### 📤 Last Optimization Request\n\n```json\n';
                    markdown += JSON.stringify(this.cachedSystemData.optimizeRequest || {}, null, 2);
                    markdown += '\n```';
                    break;

                case 'opt_response':
                    markdown = '### 📥 Last Optimization Response\n\n```json\n';
                    markdown += JSON.stringify(this.cachedSystemData.optimizeResponse || {}, null, 2);
                    markdown += '\n```';
                    break;

                default:
                    throw new Error(`Unknown data type: ${dataType}`);
            }

            // Copy to clipboard with fallback
            let success = false;
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(markdown);
                    success = true;
                } else {
                    // Fallback for older browsers
                    const textArea = document.createElement('textarea');
                    textArea.value = markdown;
                    document.body.appendChild(textArea);
                    textArea.select();
                    success = document.execCommand('copy');
                    document.body.removeChild(textArea);
                }
            } catch (error) {
                console.warn('[BugReport] Clipboard API failed, trying fallback:', error);
                // Fallback method
                const textArea = document.createElement('textarea');
                textArea.value = markdown;
                document.body.appendChild(textArea);
                textArea.select();
                success = document.execCommand('copy');
                document.body.removeChild(textArea);
            }

            if (!success) {
                throw new Error('All clipboard methods failed');
            }

            // Show visual feedback
            const button = event.target.closest('button');
            const originalContent = button.innerHTML;
            button.innerHTML = '<i class="fas fa-check"></i>';
            button.style.borderColor = '#28a745';
            button.style.color = '#28a745';

            setTimeout(() => {
                button.innerHTML = originalContent;
                button.style.borderColor = '';
                button.style.color = '';
            }, 1500);

        } catch (error) {
            console.error(`[BugReport] Error copying ${dataType} to clipboard:`, error);
            alert(`Failed to copy ${dataType} data to clipboard. Please try again.`);
        }
    }
}

// Create global bug report manager instance
const bugReportManager = new BugReportManager();

// Global function for backward compatibility
async function sendBugReport() {
    await bugReportManager.showBugReportPopup();
}