"""
Port management interface for EOS Connect web server.
Provides functionality to check port availability and handle port conflicts.
"""

import socket
import logging
import os
import subprocess
from contextlib import closing
import psutil
from gevent.pywsgi import WSGIServer

logger = logging.getLogger(__name__)


class PortInterface:
    """
    Interface for managing port availability and conflicts.
    """

    def __init__(self):
        pass

    @staticmethod
    def is_running_in_hassio():
        """
        Check if we're running as a Home Assistant add-on.

        Returns:
            bool: True if running in Home Assistant add-on environment
        """
        # Check for Home Assistant add-on environment variables
        return (
            os.environ.get("HASSIO") is not None
            or os.environ.get("HASSIO_TOKEN") is not None
            or os.path.exists("/data/options.json")
            or os.path.exists("/config")
        )

    @staticmethod
    def check_port_available(host, port):
        """
        Check if a port is available for binding.

        Args:
            host (str): The host address to check
            port (int): The port number to check

        Returns:
            bool: True if port is available, False if occupied
        """
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
                sock.settimeout(1)
                # In HA add-on, we should check 0.0.0.0 binding capability
                if host == "0.0.0.0":
                    # Try to actually bind to test availability
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.bind((host, port))
                        return True
                    except socket.error:
                        return False
                else:
                    # Use connect_ex for other hosts
                    result = sock.connect_ex((host, port))
                    return result != 0  # Port is available if connection fails
        except (socket.error, OSError) as e:
            logger.error("[PortInterface] Error checking port %s: %s", port, e)
            return False

    @staticmethod
    def find_available_ports(host, start_port, count=10):
        """
        Find available ports starting from start_port.

        Args:
            host (str): The host address to check
            start_port (int): The starting port number
            count (int): Number of ports to check

        Returns:
            list: List of available port numbers
        """
        available_ports = []
        for port in range(start_port, start_port + count):
            if PortInterface.check_port_available(host, port):
                available_ports.append(port)
        return available_ports

    @staticmethod
    def get_process_using_port(port):
        """
        Try to identify what process is using a specific port.
        Works in both regular environments and Home Assistant add-ons.

        Args:
            port (int): The port number to check

        Returns:
            str: Information about the process using the port, or None if not found
        """
        try:
            for conn in psutil.net_connections():
                if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    try:
                        process = psutil.Process(conn.pid)
                        return f"PID {conn.pid}: {process.name()}"
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        return f"PID {conn.pid}: <access denied>"
            return None
        except ImportError:
            # In HA add-on, try alternative methods
            if PortInterface.is_running_in_hassio():
                logger.debug(
                    "[PortInterface] Running in HA add-on - psutil not available"
                )
                # Try netstat if available
                try:
                    result = subprocess.run(
                        ["netstat", "-ln"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if f":{port} " in result.stdout:
                        return "detected via netstat"
                except (OSError, subprocess.SubprocessError):
                    pass
            else:
                logger.debug(
                    "[PortInterface] psutil not available for process detection"
                )
            return None
        except (psutil.Error, OSError, subprocess.CalledProcessError) as e:
            logger.debug("[PortInterface] Error detecting process: %s", e)
            return None

    @staticmethod
    def get_user_friendly_error_message(port, error_msg=""):
        """
        Generate a user-friendly error message for port conflicts.
        Provides different guidance for Home Assistant add-ons vs regular installations.

        Args:
            port (int): The port number that's in use
            error_msg (str): The original error message

        Returns:
            str: User-friendly error message with solutions
        """
        is_hassio = PortInterface.is_running_in_hassio()

        # Get information about what's using the port
        process_info = PortInterface.get_process_using_port(port)
        process_msg = f" (used by: {process_info})" if process_info else ""

        # Find some alternative ports for suggestion
        alternative_ports = PortInterface.find_available_ports("0.0.0.0", port + 1, 5)
        alternatives_msg = ""
        if alternative_ports:
            alternatives_msg = (
                "\n   💡 Available alternative ports: "
                f"{', '.join(map(str, alternative_ports))}"
            )

        # Detect error type
        is_windows_error = (
            "WinError 10048" in error_msg
            or "Normalerweise darf jede Socketadresse" in error_msg
        )
        is_linux_error = "Address already in use" in error_msg

        # Different error explanation based on environment
        if is_hassio:
            error_explanation = f"\n   📋 Home Assistant Add-on: Port {port} is already in use{process_msg}"

            solutions = (
                f"\n   🔧 How to fix this in Home Assistant:\n"
                f"   • Option 1: Change the port in the add-on configuration\n"
                f"   • Option 2: Check if another add-on is using port {port}\n"
                f"   • Option 3: Review Home Assistant logs for port conflicts{alternatives_msg}\n"
                f"   • Option 4: Restart the add-on after changing the port\n"
                f"\n   📖 Add-on Configuration:\n"
                f"   • Go to Settings → Add-ons → EOS Connect → Configuration\n"
                f"   • Change 'eos_connect_web_port' to an available port\n"
                f"   • Save and restart the add-on\n"
                f"\n   ⚠️  The web interface is required for EOS Connect to function."
            )
        else:
            # Regular installation guidance
            if is_windows_error:
                error_explanation = (
                    f"\n   📋 Windows Error: Port {port} is already in use{process_msg}"
                )
            elif is_linux_error:
                error_explanation = (
                    f"\n   📋 Linux Error: Port {port} is already in use{process_msg}"
                )
            else:
                error_explanation = (
                    f"\n   📋 Port {port} is already in use{process_msg}"
                )

            solutions = (
                f"\n   🔧 How to fix this:\n"
                f"   • Option 1: Stop the application using port {port}\n"
                f"   • Option 2: Change 'eos_connect_web_port' in your config.json"
                f"{alternatives_msg}\n"
                f"   • Option 3: Find what's using the port with:\n"
                f"     Windows: netstat -ano | findstr :{port}\n"
                f"     Linux/Mac: lsof -i :{port}\n"
                f"\n   ⚠️  EOS Connect requires its web interface to function properly."
            )

        return error_explanation + solutions

    @staticmethod
    def create_web_server_with_port_check(host, desired_port, app, logger_instance):
        """
        Create a web server with comprehensive port checking and error handling.
        Works in both regular environments and Home Assistant add-ons.

        Args:
            host (str): The host address to bind to
            desired_port (int): The desired port number
            app: The Flask application instance
            logger_instance: Logger instance for error reporting

        Returns:
            tuple: (WSGIServer instance, actual_port) if successful

        Raises:
            RuntimeError: If the server cannot be created with detailed error message
        """

        is_hassio = PortInterface.is_running_in_hassio()

        if is_hassio:
            logger_instance.info(
                "[PortInterface] Running in Home Assistant add-on mode"
            )

        # First check if desired port is available
        if not PortInterface.check_port_available(host, desired_port):
            error_msg = PortInterface.get_user_friendly_error_message(desired_port)
            logger_instance.error(
                f"[PortInterface] Port {desired_port} is not available{error_msg}"
            )
            raise RuntimeError(f"Port {desired_port} is not available")

        # Try to create the server
        try:
            logger_instance.info(
                f"[PortInterface] Creating web server on {host}:{desired_port}"
            )
            http_server = WSGIServer(
                (host, desired_port),
                app,
                log=None,
                error_log=logger_instance,
            )

            # Additional test binding (skip in HA add-on to avoid double binding issues)
            if not is_hassio:
                try:
                    with closing(
                        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    ) as test_sock:
                        test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        test_sock.bind((host, desired_port))
                        test_sock.listen(1)
                        logger_instance.debug(
                            f"[PortInterface] Port {desired_port} successfully bound for testing"
                        )
                except socket.error as e:
                    error_msg = PortInterface.get_user_friendly_error_message(
                        desired_port, str(e)
                    )
                    logger_instance.error(
                        f"[PortInterface] Port {desired_port} became unavailable{error_msg}"
                    )
                    raise RuntimeError(
                        f"Port {desired_port} became unavailable: {e}"
                    ) from e

            return http_server, desired_port

        except (OSError, socket.error) as e:
            # Handle server creation errors with environment-specific guidance
            if "WinError 10048" in str(e) or "Address already in use" in str(e):
                error_msg = PortInterface.get_user_friendly_error_message(
                    desired_port, str(e)
                )
                logger_instance.error(
                    f"[PortInterface] Port conflict detected{error_msg}"
                )
                raise RuntimeError(f"Port {desired_port} is in use") from e
            else:
                logger_instance.error(
                    f"[PortInterface] Failed to create web server on port {desired_port}: {e}"
                )
                raise RuntimeError(f"Failed to create web server: {e}") from e
