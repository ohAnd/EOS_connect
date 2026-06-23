"""
Startup Validator - Lightweight error registration during application initialization.
Registers startup errors directly to the logging system (MemoryLogHandler).
All errors are accessible via the /logs/alerts endpoint and web UI log viewer.
"""

import logging

logger = logging.getLogger(__name__)


class StartupValidator:
    """
    Registers startup errors directly to the logging system.
    This validator acts as a facade - it writes ERROR/WARNING logs that appear in
    /logs/alerts endpoint and the web UI's existing log viewer.
    
    No separate error storage - logging is the single source of truth.
    """

    def add_error(
        self,
        category: str,
        component: str,
        severity: str,
        title: str,
        message: str,
        action_required: bool = False,
        config_link: str = None,
        timestamp: str = None,
    ) -> None:
        """
        Register a startup error by writing to the logging system.

        Args:
            category: Error category (initialization, configuration, connectivity) - for context only
            component: Component that failed (e.g., 'battery_interface', 'load_interface')
            severity: 'error' or 'warning'
            title: User-friendly short title
            message: Detailed error message
            action_required: Whether user action is needed (informational)
            config_link: Optional link to the configuration section (informational)
            timestamp: ISO format timestamp (ignored - logging uses its own)
        """
        # Build log message with context
        log_msg = f"[{component}] {title}: {message}"
        if config_link:
            log_msg += f" | Config: {config_link}"
        if action_required:
            log_msg += " | ACTION REQUIRED"

        # Write to logging system (MemoryLogHandler captures this)
        log_level = logging.ERROR if severity == "error" else logging.WARNING
        logger.log(log_level, log_msg)
