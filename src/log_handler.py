"""
Custom in-memory logging handler for thread-safe log storage and retrieval.
"""

import logging
import collections
from datetime import datetime
from threading import RLock
import sys


class MemoryLogHandler(logging.Handler):
    """
    Custom logging handler that stores log records in memory for web API access.
    Thread-safe implementation with configurable buffer size and separate alert storage.
    """

    def __init__(self, max_records=1000, max_alerts=1000):
        super().__init__()
        self.max_records = max_records
        self.max_alerts = max_alerts

        # Main log buffer (all log levels)
        self.records = collections.deque(maxlen=max_records)

        # Dedicated alert buffer (WARNING, ERROR, CRITICAL only)
        self.alert_records = collections.deque(maxlen=max_alerts)

        # Use RLock instead of Lock to prevent deadlocks in recursive calls
        self.lock = RLock()
        # Prevent infinite recursion by tracking if we're already in emit()
        self._in_emit = False
        # Track shutdown state
        self._shutdown = False

        # Define alert levels
        self.alert_levels = {"WARNING", "ERROR", "CRITICAL"}

    def emit(self, record):
        """Store the log record in memory - completely non-blocking version"""
        # Don't process logs if we're shutting down
        if self._shutdown or self._in_emit:
            return

        try:
            self._in_emit = True

            # Try to acquire lock with timeout to prevent hanging
            if not self.lock.acquire(blocking=False):
                return  # Skip this log entry if we can't get the lock immediately

            try:
                # Double-check shutdown state after acquiring lock
                if self._shutdown:
                    return

                # Create log entry with minimal processing
                # Use timezone-aware timestamp if formatter is timezone-aware
                if (
                    hasattr(self, "formatter")
                    and self.formatter
                    and hasattr(self.formatter, "tz")
                    and self.formatter.tz
                ):
                    # Use the formatter's timezone to create timezone-aware timestamp
                    timestamp = datetime.fromtimestamp(
                        record.created, self.formatter.tz
                    ).isoformat()
                else:
                    # Fallback to naive timestamp (original behavior)
                    timestamp = datetime.fromtimestamp(record.created).isoformat()

                log_entry = {
                    "timestamp": timestamp,
                    "level": record.levelname,
                    "message": (
                        str(record.msg) if hasattr(record, "msg") else "No message"
                    ),
                    "module": record.name if hasattr(record, "name") else "unknown",
                    "funcName": (
                        record.funcName if hasattr(record, "funcName") else "unknown"
                    ),
                    "lineno": record.lineno if hasattr(record, "lineno") else 0,
                    "severity": self._get_severity_level(record.levelname),
                }

                # Handle message formatting safely
                if hasattr(record, "args") and record.args:
                    try:
                        log_entry["message"] = str(record.msg) % record.args
                    except (TypeError, ValueError):
                        log_entry["message"] = f"{record.msg} {record.args}"

                # Store in main buffer (all logs)
                self.records.append(log_entry)

                # Store in dedicated alert buffer if it's an alert level
                if record.levelname in self.alert_levels:
                    self.alert_records.append(log_entry)

            finally:
                self.lock.release()

        except (OSError, IOError):
            # Absolutely no exceptions should escape from a log handler
            try:
                sys.stderr.write("MemoryLogHandler error: failed to store log entry\n")
                sys.stderr.flush()
            except (RuntimeError, ValueError):
                pass  # If even stderr fails, give up silently
        finally:
            self._in_emit = False

    def get_logs(self, level_filter=None, limit=None, since=None):
        """Retrieve logs with optional filtering from main buffer"""
        if self._shutdown:
            return []

        try:
            if not self.lock.acquire(blocking=False):
                return []  # Return empty list if can't get lock

            try:
                logs = list(self.records)
            finally:
                self.lock.release()
        except RuntimeError:
            return []

        # Apply filters safely
        try:
            if level_filter:
                logs = [
                    log for log in logs if log.get("level", "") == level_filter.upper()
                ]

            if since:
                try:
                    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                    logs = [
                        log
                        for log in logs
                        if datetime.fromisoformat(log.get("timestamp", "")) >= since_dt
                    ]
                except (ValueError, TypeError):
                    pass

            if limit and limit > 0:
                logs = logs[-limit:]
        except (RuntimeError, ValueError):
            pass

        return logs

    def get_alerts(self, levels=None, limit=None, since=None):
        """Get logs that should be treated as alerts from dedicated alert buffer"""
        if self._shutdown:
            return []

        # Use dedicated alert levels if none specified
        if levels is None:
            levels = list(self.alert_levels)

        try:
            if not self.lock.acquire(blocking=False):
                return []

            try:
                # Use dedicated alert buffer instead of main buffer
                alerts = list(self.alert_records)
            finally:
                self.lock.release()

            # Apply additional filtering if requested
            try:
                # Filter by specific levels if provided
                if levels != list(self.alert_levels):
                    alerts = [
                        alert for alert in alerts if alert.get("level", "") in levels
                    ]

                # Filter by time if provided
                if since:
                    try:
                        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                        alerts = [
                            alert
                            for alert in alerts
                            if datetime.fromisoformat(alert.get("timestamp", ""))
                            >= since_dt
                        ]
                    except (ValueError, TypeError):
                        pass

                # Apply limit if provided
                if limit and limit > 0:
                    alerts = alerts[-limit:]

            except (RuntimeError, ValueError):
                pass

            return alerts
        except (RuntimeError, ValueError):
            return []

    def clear_logs(self):
        """Clear all stored logs from both buffers"""
        if self._shutdown:
            return

        try:
            if not self.lock.acquire(blocking=False):
                return  # Skip if can't get lock

            try:
                self.records.clear()
                self.alert_records.clear()
            finally:
                self.lock.release()
        except (RuntimeError, ValueError):
            pass

    def clear_alerts_only(self):
        """Clear only the alert buffer, keeping main logs intact"""
        if self._shutdown:
            return

        try:
            if not self.lock.acquire(blocking=False):
                return  # Skip if can't get lock

            try:
                self.alert_records.clear()
            finally:
                self.lock.release()
        except (RuntimeError, ValueError):
            pass

    def get_buffer_stats(self):
        """Get statistics about buffer usage"""
        if self._shutdown:
            return {"error": "Handler is shutdown"}

        try:
            if not self.lock.acquire(blocking=False):
                return {"error": "Could not acquire lock"}

            try:
                stats = {
                    "main_buffer": {
                        "current_size": len(self.records),
                        "max_size": self.max_records,
                        "usage_percent": round(
                            (len(self.records) / self.max_records) * 100, 1
                        ),
                    },
                    "alert_buffer": {
                        "current_size": len(self.alert_records),
                        "max_size": self.max_alerts,
                        "usage_percent": round(
                            (len(self.alert_records) / self.max_alerts) * 100, 1
                        ),
                    },
                    "alert_levels": list(self.alert_levels),
                }
                return stats
            finally:
                self.lock.release()
        except (RuntimeError, ValueError):
            return {"error": "Failed to get stats"}

    def shutdown(self):
        """
        Shutdown the handler gracefully.
        Stops accepting new log entries and clears resources.
        """
        try:
            with self.lock:
                self._shutdown = True
                # Optionally clear records to free memory
                self.records.clear()
                self.alert_records.clear()
        except (RuntimeError, ValueError):
            # Even shutdown shouldn't raise exceptions
            pass

    def close(self):
        """
        Close the handler (called by logging framework).
        """
        self.shutdown()
        super().close()

    def _get_severity_level(self, level_name):
        """Convert log level to numeric severity for sorting/filtering"""
        levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        return levels.get(level_name, 0)
