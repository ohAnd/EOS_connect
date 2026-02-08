"""
Update Checker Interface for EOS Connect

Checks GitHub Container Registry (GHCR) for available updates.
Only active for Docker image users (not Home Assistant Add-on users).

Features:
- Automatic branch detection (stable vs develop)
- Background service with periodic checks (every 12 hours)
- HA Add-on detection (skips checking for add-on users)
- Zero configuration required
"""

import logging
import time
import threading
import requests
from packaging import version as pkg_version
from interfaces.port_interface import PortInterface

logger = logging.getLogger("__main__")


class UpdateChecker:
    """
    Background service to check for EOS Connect updates from GHCR.

    Automatically detects:
    - Current version from version string
    - Whether running as HA Add-on (skips checking)
    - Whether on stable or develop branch
    - Latest available version matching the branch
    """

    def __init__(self, current_version, check_interval=43200, on_status_change=None):
        """
        Initialize the UpdateChecker.

        Args:
            current_version: Version string (e.g., "0.2.31.236-develop")
            check_interval: Seconds between checks (default: 43200 = 12 hours)
            on_status_change: Optional callback function called when status changes
                             Receives update_status dict as parameter
        """
        self.current_version = current_version
        self.check_interval = check_interval
        self.on_status_change = on_status_change

        # Parse current version
        self.is_develop = "-develop" in current_version
        self.current_version_clean = current_version.replace("-develop", "")

        try:
            self.current_version_parsed = pkg_version.parse(self.current_version_clean)
        except pkg_version.InvalidVersion:
            logger.warning(
                "[UPDATE-CHECK] Invalid version format: %s - Update checking disabled",
                current_version,
            )
            self.current_version_parsed = None

        # Check if running in HA Add-on
        self.is_ha_addon = PortInterface.is_running_in_hassio()

        # GHCR configuration
        self.registry_url = "https://ghcr.io"
        self.package_name = "ohand/eos_connect"  # lowercase as per Docker convention

        # Update status
        self.update_available = False
        self.latest_version = None
        self.latest_version_tag = None
        self.last_check_time = None
        self.last_check_success = False
        self.last_error = None

        # Background thread
        self._update_thread = None
        self._stop_event = threading.Event()

        # Start the service if applicable
        if self.is_ha_addon:
            logger.info(
                "[UPDATE-CHECK] Running as Home Assistant Add-on - "
                "update checking disabled (HA manages updates)"
            )
        elif self.current_version_parsed is None:
            logger.warning("[UPDATE-CHECK] Invalid version - update checking disabled")
        else:
            logger.info(
                "[UPDATE-CHECK] Initialized for %s branch (version: %s)",
                "develop" if self.is_develop else "stable",
                current_version,
            )
            self.__start_update_service()

    def get_update_status(self):
        """
        Get current update status.

        Returns:
            dict: Update status information
        """
        return {
            "enabled": not self.is_ha_addon and self.current_version_parsed is not None,
            "is_ha_addon": self.is_ha_addon,
            "current_version": self.current_version,
            "is_develop_branch": self.is_develop,
            "update_available": self.update_available,
            "latest_version": self.latest_version_tag,
            "last_check_time": self.last_check_time,
            "last_check_success": self.last_check_success,
            "last_error": self.last_error,
            "next_check_in_seconds": self._get_next_check_in_seconds(),
        }

    def _get_next_check_in_seconds(self):
        """Calculate seconds until next check."""
        if self.last_check_time is None:
            return 0  # Check immediately on startup

        elapsed = time.time() - self.last_check_time
        remaining = self.check_interval - elapsed
        return max(0, int(remaining))

    def check_for_updates(self):
        """
        Check GHCR for available updates.

        Returns:
            bool: True if check was successful, False on error
        """
        if self.is_ha_addon or self.current_version_parsed is None:
            return False

        try:
            logger.debug("[UPDATE-CHECK] Checking for updates...")

            # Get authentication token
            token = self._get_ghcr_token()
            if not token:
                self.last_error = "Failed to get GHCR authentication token"
                self.last_check_success = False
                return False

            # Get available tags
            tags = self._get_ghcr_tags(token)
            if tags is None:
                self.last_error = "Failed to retrieve tags from GHCR"
                self.last_check_success = False
                return False

            # Parse and filter versions
            latest = self._find_latest_version(tags)

            if latest is None:
                branch_type = "develop" if self.is_develop else "stable"
                self.last_error = f"No {branch_type} versions found in GHCR"
                self.last_check_success = False
                logger.warning("[UPDATE-CHECK] %s", self.last_error)
                return False

            # Check if update is available
            self.latest_version = latest["parsed"]
            self.latest_version_tag = latest["tag"]
            previous_update_available = self.update_available
            self.update_available = self.latest_version > self.current_version_parsed

            # Clear error on success
            self.last_error = None
            self.last_check_success = True
            self.last_check_time = time.time()

            if self.update_available:
                logger.info(
                    "[UPDATE-CHECK] Update available! Current: %s → Latest: %s",
                    self.current_version,
                    self.latest_version_tag,
                )
            else:
                logger.info(
                    "[UPDATE-CHECK] Up to date (version: %s)", self.current_version
                )

            # Call status change callback if status changed
            if (
                self.on_status_change
                and previous_update_available != self.update_available
            ):
                try:
                    self.on_status_change(self.get_update_status())
                except Exception as e:  # pylint: disable=broad-except
                    # Catch all exceptions from external callback to prevent update check failure
                    logger.error(
                        "[UPDATE-CHECK] Error in status change callback: %s", e
                    )

            return True

        except Exception as e:  # pylint: disable=broad-except
            # Catch-all for any unexpected errors to ensure graceful failure
            self.last_error = f"Unexpected error: {str(e)}"
            self.last_check_success = False
            logger.error("[UPDATE-CHECK] Error checking for updates: %s", e)
            return False

    def _get_ghcr_token(self):
        """
        Get anonymous authentication token from GHCR.

        Returns:
            str: Authentication token or None on failure
        """
        token_url = f"https://ghcr.io/token?scope=repository:{self.package_name}:pull"

        try:
            response = requests.get(token_url, timeout=10)
            if response.status_code == 200:
                token_data = response.json()
                return token_data.get("token") or token_data.get("access_token")
            else:
                logger.warning(
                    "[UPDATE-CHECK] Failed to get GHCR token: HTTP %d",
                    response.status_code,
                )
                return None
        except requests.exceptions.RequestException as e:
            logger.warning("[UPDATE-CHECK] Error getting GHCR token: %s", e)
            return None

    def _get_ghcr_tags(self, token):
        """
        Get available tags from GHCR.

        Args:
            token: Authentication token

        Returns:
            list: Tag names or None on failure
        """
        tags_url = f"{self.registry_url}/v2/{self.package_name}/tags/list"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = requests.get(tags_url, headers=headers, timeout=10)
            if response.status_code == 200:
                tags_data = response.json()
                return tags_data.get("tags", [])
            else:
                logger.warning(
                    "[UPDATE-CHECK] Failed to get tags: HTTP %d", response.status_code
                )
                return None
        except requests.exceptions.RequestException as e:
            logger.warning("[UPDATE-CHECK] Error getting tags: %s", e)
            return None

    def _find_latest_version(self, tags):
        """
        Find the latest version matching current branch (stable or develop).

        Args:
            tags: List of tag names from GHCR

        Returns:
            dict: {'tag': str, 'parsed': Version} or None if not found
        """
        matching_versions = []

        for tag in tags:
            # Skip special tags
            if tag in ["latest", "release", "develop"]:
                continue

            # Skip feature branches
            if tag.startswith("feature"):
                continue

            # Parse version tag
            try:
                is_develop_tag = "-develop" in tag

                # Only consider tags matching our branch
                if is_develop_tag != self.is_develop:
                    continue

                clean_version = tag.replace("-develop", "")
                parsed = pkg_version.parse(clean_version)

                matching_versions.append(
                    {
                        "tag": tag,
                        "parsed": parsed,
                    }
                )
            except pkg_version.InvalidVersion:
                continue

        # Sort by version (newest first) and return latest
        if matching_versions:
            matching_versions.sort(key=lambda x: x["parsed"], reverse=True)
            return matching_versions[0]

        return None

    def __start_update_service(self):
        """Start the background update checking service."""
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self.__update_check_loop, daemon=True
            )
            self._update_thread.start()
            logger.info(
                "[UPDATE-CHECK] Background service started (checks every 12 hours)"
            )

    def __update_check_loop(self):
        """Background loop that periodically checks for updates."""
        # Perform initial check after short delay (30 seconds)
        initial_delay = 30
        logger.info(
            "[UPDATE-CHECK] First check will run in %d seconds, then every 12 hours",
            initial_delay,
        )

        if self._stop_event.wait(timeout=initial_delay):
            return  # Stopped before first check

        while not self._stop_event.is_set():
            try:
                self.check_for_updates()
            except Exception as e:  # pylint: disable=broad-except
                # Background thread must not die - catch all exceptions
                logger.error(
                    "[UPDATE-CHECK] Unexpected error in update check loop: %s", e
                )

            # Sleep until next check (in 1-second chunks for responsive shutdown)
            sleep_interval = self.check_interval
            while sleep_interval > 0:
                if self._stop_event.is_set():
                    return
                time.sleep(min(1, sleep_interval))
                sleep_interval -= 1

    def shutdown(self):
        """Stop the background update checking service."""
        if self._update_thread and self._update_thread.is_alive():
            logger.info("[UPDATE-CHECK] Shutting down background service")
            self._stop_event.set()
            self._update_thread.join(timeout=5)
            if self._update_thread.is_alive():
                logger.warning(
                    "[UPDATE-CHECK] Background thread did not stop gracefully"
                )
            else:
                logger.info("[UPDATE-CHECK] Background service stopped")
