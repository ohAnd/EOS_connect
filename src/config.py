"""
This module provides the ConfigManager class for managing configuration settings
of the application. The configuration settings are stored in a 'config.yaml' file.
"""

import copy
import json
import os
import logging
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

logger = logging.getLogger("__main__")
logger.info("[Config] loading module ")


# Data directory persistence (#287).
#
# A container without a volume on /app/data loses eos_connect.db on every recreate,
# while the bind-mounted config.yaml survives — so the next start re-migrates it and
# resurrects pre-database settings. Undetectable after the fact, hence this check.
# Everything below fails closed: unclassifiable means "unknown", never "ephemeral",
# because a false alarm costs more than a missed warning.

_MOUNTINFO_PATH = "/proc/self/mountinfo"  # module-level so tests can repoint it
_CONTAINER_ROOT_FSTYPES = frozenset({"overlay", "overlayfs"})
_RAM_BACKED_FSTYPES = frozenset({"tmpfs", "ramfs"})

# mountinfo octal-escapes these characters in mount points.
_MOUNTINFO_ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))


def _fstype_for_path(path):
    """Filesystem type of the mount *path* resolves onto, or None if unknowable.

    Longest-prefix match over ``/proc/self/mountinfo``. Diagnostic only — callers
    treat None as "no opinion", so a parse failure never changes a verdict.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        return None

    best_len, best_type = -1, None
    try:
        with open(_MOUNTINFO_PATH, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 8:
                    continue
                # Optional fields ("shared:1", ...) are variable length and end at a
                # lone "-"; fs type is the token after it. Searching from index 6
                # keeps a mount point from being read as the separator.
                try:
                    separator = fields.index("-", 6)
                except ValueError:
                    continue
                if separator + 1 >= len(fields):
                    continue
                mount_point = fields[4]
                for escape, literal in _MOUNTINFO_ESCAPES:
                    mount_point = mount_point.replace(escape, literal)
                if mount_point == target or target.startswith(
                    mount_point.rstrip("/") + "/"
                ):
                    # >= not >: shadowed mounts are listed in order, last one wins.
                    if len(mount_point) >= best_len:
                        best_len, best_type = len(mount_point), fields[separator + 1]
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return best_type


def _in_container():
    """True when running inside an OCI container (Docker, Podman, containerd).

    Not /proc/1/cgroup: under cgroup v2 a container's is typically just ``0::/``,
    indistinguishable from a host PID 1.
    """
    try:
        if os.path.exists("/.dockerenv"):  # Docker, including compose
            return True
        if os.environ.get("container"):  # podman, systemd-nspawn
            return True
        return _fstype_for_path("/") in _CONTAINER_ROOT_FSTYPES
    except OSError:
        return False


def check_data_dir_persistent(data_dir):
    """Report whether *data_dir* survives a container recreate.

    Returns ``(verdict, detail)``: True on a mount of its own (bind mount, named or
    anonymous volume), False on the container's writable layer, None if undetermined.

    The verdict is a device-id comparison and nothing else. overlay2 reports the
    merged mount's own device for a file living only in the upper layer, so
    "different device from /" is exactly "survives a recreate" for every mount kind.
    """
    try:
        data_dev = os.stat(data_dir).st_dev
        root_dev = os.stat("/").st_dev
    except OSError as exc:
        return None, f"cannot stat {data_dir}: {exc}"

    fstype = _fstype_for_path(data_dir)

    if data_dev == root_dev:
        return False, (
            f"{data_dir} shares a device with / ({fstype or 'unknown fs'}), "
            "so it is part of the container image layer"
        )

    detail = f"{data_dir} is on its own mount ({fstype or 'unknown fs'})"
    if fstype in _RAM_BACKED_FSTYPES:
        # Deliberately not a veto: from inside a container `--tmpfs /app/data` and a
        # bind mount of a host tmpfs path (systemd puts /tmp there on many distros)
        # are indistinguishable, and the bind mount does survive a recreate.
        detail += " - note: RAM-backed, so it is lost when the host reboots"
    return True, detail


class ConfigManager:
    """
    Manages the configuration settings for the application.

    This class handles loading, updating, and saving configuration settings from a 'config.yaml'
    file. If the configuration file does not exist, it creates one with default values and
    prompts the user to restart the server.
    """

    def __init__(self, given_dir):
        self.current_dir = given_dir
        self.config_file = os.path.join(self.current_dir, "config.yaml")
        self.yaml = YAML()
        self.yaml.default_flow_style = False
        self.yaml.indent(mapping=2, sequence=4, offset=2)
        self.yaml.preserve_quotes = True
        self.default_config = self.create_default_config()
        self.config = self.default_config.copy()
        self.load_config()

    @property
    def data_dir(self) -> str:
        """Resolve the persistent data directory for SQLite DB and other data files.

        Resolution order:
        1. HA addon environment -> /data/ (Supervisor owns it; nothing overrides)
        2. ``EOS_DATA_PATH`` -> custom path
        3. ``data_path`` in config.yaml -> custom path
        4. Default -> ./data/ relative to application directory

        Steps 2 and 3 share the ``data_path`` key: ``load_env_bootstrap`` has already
        written the environment value over the config.yaml one by the time this runs.
        """
        if self.is_ha_addon:
            return "/data"

        custom = self.config.get("data_path")
        if custom:
            return str(custom)

        return os.path.join(self.current_dir, "data")

    def data_dir_persistence(self):
        """Classify the data directory: ``"persistent"``/``"ephemeral"``/``"unknown"``.

        Only ``"ephemeral"`` is actionable. Callers must treat ``"unknown"`` exactly
        like ``"persistent"``: a wrong "unknown" costs a log line, a wrong
        "ephemeral" costs a user's settings.

        Returns:
            Tuple of (state, detail) where detail is a short human-readable reason.
        """
        if self.is_ha_addon:
            return "persistent", "/data is persisted by the Home Assistant Supervisor"

        if not _in_container():
            return "unknown", "not running in a container"

        verdict, detail = check_data_dir_persistent(self.data_dir)
        if verdict is None:
            return "unknown", detail
        return ("persistent" if verdict else "ephemeral"), detail

    @property
    def is_ha_addon(self) -> bool:
        """Return True when running inside a Home Assistant add-on."""
        return (
            os.environ.get("HASSIO") is not None
            or os.environ.get("HASSIO_TOKEN") is not None
            or os.path.exists("/data/options.json")
        )

    # HA bootstrap key mapping: options.json key -> config dict key
    _HA_BOOTSTRAP_MAP = {
        "web_port": "eos_connect_web_port",
        "eos_connect_web_port": "eos_connect_web_port",
        "time_zone": "time_zone",
        "log_level": "log_level",
    }

    # Environment variable bootstrap mapping: ENV name -> config dict key
    _ENV_BOOTSTRAP_MAP = {
        "EOS_WEB_PORT": "eos_connect_web_port",
        "EOS_TIMEZONE": "time_zone",
        "EOS_LOG_LEVEL": "log_level",
        # data_path is in BOOTSTRAP_KEYS, so it never reaches SQLite — the path must be
        # known before the store it points at can be opened.
        "EOS_DATA_PATH": "data_path",
    }

    def load_ha_bootstrap(self) -> dict:
        """Read bootstrap values from HA addon ``/data/options.json``.

        Returns:
            Dict of bootstrap key/value pairs that were applied, empty if not
            running in HA or if options.json is missing/invalid.
        """
        options_path = "/data/options.json"
        if not self.is_ha_addon or not os.path.exists(options_path):
            return {}

        try:
            with open(options_path, "r", encoding="utf-8") as f:
                options = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[Config] Failed to read %s: %s", options_path, exc)
            return {}

        applied = {}
        for opt_key, cfg_key in self._HA_BOOTSTRAP_MAP.items():
            if opt_key in options and options[opt_key] is not None:
                self.config[cfg_key] = options[opt_key]
                applied[cfg_key] = options[opt_key]

        if applied:
            logger.info("[Config] Applied HA addon bootstrap values: %s", list(applied.keys()))
        return applied

    def load_env_bootstrap(self) -> dict:
        """Read bootstrap values from environment variables.

        Supports ``EOS_WEB_PORT``, ``EOS_TIMEZONE``, ``EOS_LOG_LEVEL`` and
        ``EOS_DATA_PATH``. These take precedence over config.yaml and options.json
        values.

        Returns:
            Dict of bootstrap key/value pairs that were applied.
        """
        applied = {}
        for env_key, cfg_key in self._ENV_BOOTSTRAP_MAP.items():
            value = os.environ.get(env_key)
            if value:
                # Coerce port to int
                if cfg_key == "eos_connect_web_port":
                    try:
                        value = int(value)
                    except ValueError:
                        logger.warning("[Config] Invalid %s value: %s", env_key, value)
                        continue
                elif cfg_key == "data_path":
                    # A path, never a number — must not reach the int() branch above.
                    value = value.strip()
                    if not value:
                        continue
                    if not os.path.isabs(value):
                        logger.warning(
                            "[Config] %s=%s is relative and resolves against the "
                            "working directory - use an absolute path",
                            env_key,
                            value,
                        )
                self.config[cfg_key] = value
                applied[cfg_key] = value

        if applied:
            logger.info("[Config] Applied env bootstrap values: %s", list(applied.keys()))
        return applied

    def create_default_config(self):
        """
        Creates the default bootstrap configuration with comments.

        Only contains the three bootstrap keys managed via config.yaml.
        All other settings are managed through the web UI and stored in SQLite.
        """
        config = CommentedMap(
            {
                "eos_connect_web_port": 8081,
                "time_zone": "Europe/Berlin",
                "log_level": "info",
            }
        )
        config.yaml_add_eol_comment(
            "Port for EOS Connect web server - default: 8081",
            "eos_connect_web_port",
        )
        config.yaml_add_eol_comment(
            "Time zone for the application - default: Europe/Berlin",
            "time_zone",
        )
        config.yaml_add_eol_comment(
            "Log level: debug, info, warning, error - default: info",
            "log_level",
        )
        return config

    def load_config(self):
        """
        Reads the configuration from 'config.yaml' file located in the current directory.
        If the file exists, it loads the configuration values.
        If the file does not exist, defaults are used and the setup wizard will
        guide the user through initial configuration.

        When running as an HA addon, bootstrap values from ``/data/options.json``
        override the corresponding config.yaml values. Environment variables
        (``EOS_WEB_PORT``, ``EOS_TIMEZONE``, ``EOS_LOG_LEVEL``) take highest
        precedence.
        """
        # isfile, not exists: the compose file used to bind-mount the gitignored
        # ./src/config.yaml, so a clean clone made Docker create a *directory* here and
        # open() raised IsADirectoryError at import time. Nothing in a bootstrap file is
        # worth a hard crash — every key has a default and the database is authoritative.
        if os.path.isfile(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = self.yaml.load(f)
                if loaded:
                    self.config.update(loaded)
            except (OSError, YAMLError) as exc:
                logger.warning(
                    "[Config] Could not read %s (%s) - using defaults, "
                    "settings from the database still apply",
                    self.config_file,
                    exc,
                )
        else:
            if os.path.exists(self.config_file):
                logger.warning(
                    "[Config] %s exists but is not a file - ignoring it. If this is a "
                    "directory, a docker volume mount created it; remove the "
                    "config.yaml bind mount and use EOS_WEB_PORT / EOS_TIMEZONE / "
                    "EOS_LOG_LEVEL instead.",
                    self.config_file,
                )
            if self.is_ha_addon:
                logger.info(
                    "[Config] No config.yaml found (HA addon mode) - using defaults"
                )
            else:
                logger.info(
                    "[Config] No config.yaml found - using defaults, "
                    "setup wizard will guide initial configuration"
                )

        # In HA addon mode, bootstrap values from options.json override config.yaml
        self.load_ha_bootstrap()
        # Environment variables take highest precedence
        self.load_env_bootstrap()

        # If config.yaml doesn't exist, create it with defaults
        # (for fresh install only, not for HA addon mode).
        # exists(), not isfile(): if a directory occupies the path there is nothing
        # useful to write and open(..., "w") would raise.
        if not os.path.exists(self.config_file) and not self.is_ha_addon:
            logger.info(
                "[Config] Creating new config.yaml with bootstrap defaults at %s",
                self.config_file,
            )
            self.write_config()

    def write_config(self):
        """
        Writes the configuration to 'config.yaml' file located in the current directory.

        Never fatal: config.yaml holds bootstrap values only, so a read-only bind mount
        must not stop startup.

        A ``data_path`` from ``EOS_DATA_PATH`` is not written out — it describes how the
        container was started, and persisting it would outlive the variable. One put in
        config.yaml by hand is preserved.
        """
        logger.info("[Config] writing config file")
        to_dump = self.config
        if os.environ.get("EOS_DATA_PATH") and "data_path" in to_dump:
            to_dump = copy.deepcopy(self.config)  # deepcopy keeps CommentedMap comments
            to_dump.pop("data_path", None)
        try:
            with open(self.config_file, "w", encoding="utf-8") as config_file_handle:
                self.yaml.dump(to_dump, config_file_handle)
        except OSError as exc:
            logger.warning(
                "[Config] Could not write %s (%s) - continuing with the values "
                "already loaded",
                self.config_file,
                exc,
            )
