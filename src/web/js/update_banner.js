/**
 * Update Banner Manager
 * Checks for available updates and displays a notification banner
 */

class UpdateBannerManager {
    constructor() {
        this.checkInterval = 300000; // Check every 5 minutes (300000ms)
        this.dismissalKey = 'eos_update_banner_dismissed';
        this.dismissedVersionKey = 'eos_update_banner_dismissed_version';
        this.updateCheckTimer = null;
        this.currentStatus = null;
    }

    /**
     * Initialize the update banner manager
     * Called on page load
     */
    init() {
        console.log('[UPDATE-BANNER] Initializing update banner manager');
        
        // Create banner element if it doesn't exist
        this.createBannerElement();
        
        // Check for updates immediately
        this.checkForUpdates();
        
        // Set up periodic checking
        this.updateCheckTimer = setInterval(() => {
            this.checkForUpdates();
        }, this.checkInterval);
        
        console.log('[UPDATE-BANNER] Update banner manager initialized (checking every ' + 
                    (this.checkInterval / 60000) + ' minutes)');
    }

    /**
     * Create the banner HTML element
     */
    createBannerElement() {
        // Check if banner already exists
        if (document.getElementById('update-banner')) {
            return;
        }

        const banner = document.createElement('div');
        banner.id = 'update-banner';
        banner.className = 'update-banner';
        banner.style.display = 'none'; // Hidden by default
        
        banner.innerHTML = `
            <div class="update-banner-content">
                <div class="update-banner-icon">
                    <i class="fas fa-arrow-circle-up"></i>
                </div>
                <div class="update-banner-text">
                    <strong>Update Available!</strong>
                    <span id="update-banner-version">A new version is available</span>
                </div>
                <div class="update-banner-actions">
                    <button id="update-banner-info" class="update-banner-button">
                        <i class="fas fa-info-circle"></i> View Details
                    </button>
                </div>
            </div>
        `;

        // Insert at the top of the body
        document.body.insertBefore(banner, document.body.firstChild);

        // Set up View Details button click handler
        const detailsBtn = document.getElementById('update-banner-info');
        if (detailsBtn) {
            detailsBtn.addEventListener('click', () => {
                console.log('[UPDATE-BANNER] View Details clicked, opening Info page');
                
                // Hide the banner (will reappear on next check if not dismissed)
                this.hideBanner();
                
                // Keep blue dot visible (update is still available)
                if (this.currentStatus && this.currentStatus.update_available && !this.isDismissed()) {
                    if (typeof MenuNotifications !== 'undefined') {
                        MenuNotifications.showUpdateDot();
                    }
                }
                
                // Open Info page directly without showing menu dropdown
                if (typeof showInfoMenu === 'function' && typeof data_controls !== 'undefined') {
                    showInfoMenu(
                        data_controls["eos_connect_version"],
                        data_controls["used_optimization_source"],
                        data_controls["used_time_frame_base"]
                    );
                } else {
                    console.warn('[UPDATE-BANNER] Cannot open Info page directly, data not available');
                }
            });
        }
    }

    /**
     * Check for available updates via REST API
     */
    async checkForUpdates() {
        try {
            const response = await fetch('/api/update/status');
            if (!response.ok) {
                console.warn('[UPDATE-BANNER] Failed to fetch update status:', response.status);
                return;
            }

            const data = await response.json();
            this.currentStatus = data.update_status;

            console.log('[UPDATE-BANNER] Update check result:', {
                enabled: this.currentStatus.enabled,
                available: this.currentStatus.update_available,
                current: this.currentStatus.current_version,
                latest: this.currentStatus.latest_version
            });

            // Blue dot logic: Show if update available and not dismissed
            if (this.currentStatus.enabled && this.currentStatus.update_available && !this.isDismissed()) {
                // Always show blue dot when update available and not dismissed
                if (typeof MenuNotifications !== 'undefined') {
                    MenuNotifications.showUpdateDot();
                }
            } else {
                // Hide blue dot when no update or dismissed
                if (typeof MenuNotifications !== 'undefined') {
                    MenuNotifications.hideUpdateDot();
                }
            }
            
            // Banner logic: Only show if not already visible and update available
            if (this.currentStatus.enabled && this.currentStatus.update_available && !this.isDismissed()) {
                this.showBanner();
            } else {
                this.hideBanner();
            }

        } catch (error) {
            console.error('[UPDATE-BANNER] Error checking for updates:', error);
        }
    }

    /**
     * Show the update banner
     */
    showBanner() {
        if (!this.currentStatus || !this.currentStatus.update_available) {
            return;
        }

        // Check if user dismissed this specific version
        const dismissedVersion = localStorage.getItem(this.dismissedVersionKey);
        if (dismissedVersion === this.currentStatus.latest_version) {
            console.log('[UPDATE-BANNER] Banner dismissed for version:', dismissedVersion);
            return;
        }

        const banner = document.getElementById('update-banner');
        if (!banner) {
            return;
        }

        // Update banner text
        const versionText = document.getElementById('update-banner-version');
        if (versionText) {
            versionText.textContent = `Version ${this.currentStatus.latest_version} is available (current: ${this.currentStatus.current_version})`;
        }

        // Update link to GitHub releases/packages
        const link = document.getElementById('update-banner-link');
        if (link) {
            // Link to GHCR package page
            link.href = 'https://github.com/Ohand/EOS_connect/pkgs/container/eos_connect';
        }

        // Show banner with animation
        banner.style.display = 'block';
        setTimeout(() => {
            banner.classList.add('update-banner-visible');
        }, 10);

        console.log('[UPDATE-BANNER] Showing update banner for version:', this.currentStatus.latest_version);
    }

    /**
     * Hide the update banner
     */
    hideBanner() {
        const banner = document.getElementById('update-banner');
        if (!banner) {
            return;
        }

        banner.classList.remove('update-banner-visible');
        setTimeout(() => {
            banner.style.display = 'none';
        }, 300); // Match CSS transition duration
    }

    /**
     * Dismiss the banner (called from Info page)
     * Stores the dismissed version in localStorage
     */
    dismissBanner() {
        if (!this.currentStatus) {
            return;
        }

        // Store the dismissed version
        localStorage.setItem(this.dismissedVersionKey, this.currentStatus.latest_version);
        
        console.log('[UPDATE-BANNER] Banner dismissed for version:', this.currentStatus.latest_version);
        
        // Hide the banner
        this.hideBanner();
        
        // Hide blue dot
        if (typeof MenuNotifications !== 'undefined') {
            MenuNotifications.hideUpdateDot();
        }
    }
    
    /**
     * Check if current update has been dismissed
     */
    isDismissed() {
        if (!this.currentStatus || !this.currentStatus.latest_version) {
            return false;
        }
        const dismissedVersion = localStorage.getItem(this.dismissedVersionKey);
        return dismissedVersion === this.currentStatus.latest_version;
    }

    /**
     * Get current update status
     * Used by Info page to display update information
     */
    getStatus() {
        return this.currentStatus;
    }

    /**
     * Clean up timers
     */
    destroy() {
        if (this.updateCheckTimer) {
            clearInterval(this.updateCheckTimer);
            this.updateCheckTimer = null;
        }
    }
}

// Create global instance
const updateBannerManager = new UpdateBannerManager();

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        updateBannerManager.init();
    });
} else {
    // DOM already loaded
    updateBannerManager.init();
}
