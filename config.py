"""
Persistent JSON configuration for Qizhi PuTTY SSH Bridge.

The configuration file lives alongside the log directory under the user's
local application data so the packaged EXE can read and write settings without
needing administrator privileges or modifying its own installation directory.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from logger import get_config_path, get_logger

logger = get_logger("config")


#: Default configuration values. These are used when the config file is missing
#: or when specific keys are absent.
DEFAULT_CONFIG: Dict[str, Any] = {
    # Local API server defaults.
    "api_host": "127.0.0.1",
    "api_port": 8766,
    # SSH connection defaults.
    "ssh_connect_timeout": 15,
    "retry_connect": True,
    "retry_max_attempts": 3,
    "retry_delay_seconds": 2,
    "exit_on_connect_failure": False,
    # Logging.
    "log_level": "INFO",
    # Keep-alive: send a harmless command if idle to prevent bastion timeout.
    "keepalive_enabled": True,
    "keepalive_interval_seconds": 180,
    "keepalive_command": "ls",
    # Log rotation.
    "log_max_bytes": 10 * 1024 * 1024,
    "log_backup_count": 5,
    # 危险操作兜底（内部策略，见 command_guard.py；不写入对外文档）。
    "dangerous_command_guard_enabled": True,
    "dangerous_command_guard_message": "",
    "dangerous_command_guard_programs": [],
    "dangerous_command_guard_patterns": [],
    "dangerous_command_guard_allow_patterns": [],
    "dangerous_command_guard_disabled_rules": [],
    # Live console: show sanitized startup fields and command/result previews.
    # Preview bodies are excluded from bridge.log by logger.py.
    "console_log": True,
    "console_verbose": False,
    "console_output_limit": 12000,
}


def _expand_env(value: Any) -> Any:
    """Recursively expand ``%VAR%`` style environment variables in strings."""
    if isinstance(value, str):
        return Path(value).expandvars().as_posix() if "%" in value else value
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the JSON configuration, creating defaults if necessary.

    Args:
        config_path: Optional explicit path. Defaults to
            ``%LOCALAPPDATA%/sshbridge/QizhiSSHBridge/config.json``.

    Returns:
        A dictionary with defaults merged with user-supplied settings.
    """
    path = config_path or get_config_path()
    config = DEFAULT_CONFIG.copy()

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                user_config = json.load(handle)
            if isinstance(user_config, dict):
                config.update(user_config)
                logger.info("Loaded configuration from %s", path)
            else:
                logger.warning(
                    "Config file %s did not contain a JSON object; using defaults.", path
                )
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read config %s: %s", path, exc)
    else:
        logger.info("No config file found at %s; using defaults.", path)

    # Resolve Windows environment variables (e.g. %USERPROFILE%).
    config = _expand_env(config)

    # Normalize log level string to a valid logging level name.
    # Use logging._nameToLevel for Python 3.10 compatibility
    # (getLevelNamesMapping was added in Python 3.11).
    level_name = str(config.get("log_level", "DEBUG")).upper()
    if level_name not in logging._nameToLevel:  # type: ignore[attr-defined]
        logger.warning("Invalid log_level %r; falling back to DEBUG.", level_name)
        config["log_level"] = "DEBUG"

    return config


def save_config(config: Dict[str, Any], config_path: Optional[Path] = None) -> bool:
    """Persist the configuration dictionary to JSON.

    Args:
        config: The configuration dictionary to save.
        config_path: Optional explicit path. Defaults to the standard location.

    Returns:
        ``True`` on success, ``False`` otherwise.
    """
    path = config_path or get_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
        logger.info("Saved configuration to %s", path)
        return True
    except (TypeError, OSError) as exc:
        logger.error("Failed to save config %s: %s", path, exc)
        return False


def get_config_value(config: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Return a configuration value, falling back to ``default`` if missing."""
    return config.get(key, default)
