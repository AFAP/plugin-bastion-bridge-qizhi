"""
Centralized logging configuration for the Qizhi (齐治) SSH Bridge.

Logs are written to the user's local application data directory so the tool
works reliably when packaged as a single-file Windows EXE and launched from
arbitrary working directories.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


#: Name of the application; used for the log directory and logger names.
APP_NAME = "QizhiSSHBridge"


def get_app_dir() -> Path:
    """Return the application data directory under ``%LOCALAPPDATA%``.

    All bridge applications share the same parent directory
    ``%LOCALAPPDATA%\sshbridge`` and are grouped by ``APP_NAME``.

    Falls back to a sibling ``.sshbridge`` directory in the user's home
    folder when ``LOCALAPPDATA`` is unavailable (e.g. some CI environments).
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "sshbridge" / APP_NAME
    return Path.home() / ".sshbridge" / APP_NAME.lower()


def get_log_path() -> Path:
    """Return the full path to the rotating log file."""
    log_dir = get_app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "bridge.log"


def get_config_path() -> Path:
    """Return the full path to the JSON configuration file."""
    config_dir = get_app_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"


def _enable_ansi_colors() -> bool:
    """Try to enable ANSI escape sequence support for the console.

    Windows 10+ cmd/PowerShell supports ANSI colors once virtual terminal
    processing is switched on.  Returns False when colors are unavailable
    (e.g. output is piped to a file), in which case plain text is used.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        # (DWORD)-12 == STD_ERROR_HANDLE; the console handler writes to stderr.
        handle = kernel32.GetStdHandle(ctypes.c_ulong(0xFFFFFFF4))
        if not handle:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


class _ColorFormatter(logging.Formatter):
    """Console formatter that wraps lines in ANSI colors.

    * ``>>>`` (received commands)  -> bright cyan
    * ``<<<`` (server responses)   -> bright green
    * WARNING                      -> yellow
    * ERROR and above              -> red
    """

    _CMD = "\033[96m"
    _RESP = "\033[92m"
    _WARN = "\033[93m"
    _ERR = "\033[91m"
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        message = record.getMessage()
        if message.startswith(">>>"):
            color = self._CMD
        elif message.startswith("<<<"):
            color = self._RESP
        elif record.levelno >= logging.ERROR:
            color = self._ERR
        elif record.levelno >= logging.WARNING:
            color = self._WARN
        else:
            return text
        return f"{color}{text}{self._RESET}"


def setup_logging(
    level: int = logging.DEBUG,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    console: bool = True,
) -> logging.Logger:
    """Configure the root application logger.

    Args:
        level: Minimum log level (default ``logging.DEBUG``).
        max_bytes: Maximum size of ``bridge.log`` before rotation.
        backup_count: Number of rotated log files to keep.
        console: Also emit logs to ``stderr`` when ``True``. Useful during
            development; can be disabled for the packaged EXE to keep the
            console clean.

    Returns:
        The configured root logger for the application.
    """
    root_logger = logging.getLogger(APP_NAME)
    root_logger.setLevel(level)

    # Avoid duplicate handlers if setup_logging is called more than once.
    if root_logger.handlers:
        return root_logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler so the log directory does not grow unbounded.
    try:
        log_path = get_log_path()
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as exc:
        # If we cannot write to the log file, at least complain on stderr.
        sys.stderr.write(f"[QizhiSSHBridge] Failed to open log file: {exc}\n")

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        if _enable_ansi_colors():
            console_handler.setFormatter(_ColorFormatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
        else:
            console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    root_logger.info("Logging initialized. log_path=%s", get_log_path())
    return root_logger


def log_blank_line() -> None:
    """Write a bare blank line to every handler stream (console + log file).

    Used to visually separate request/response cycles in the output.
    """
    root_logger = logging.getLogger(APP_NAME)
    for handler in root_logger.handlers:
        stream = getattr(handler, "stream", None)
        if stream is None:
            continue
        try:
            stream.write("\n")
            handler.flush()
        except Exception:  # noqa: BLE001
            pass


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under the application namespace."""
    full_name = APP_NAME if name is None else f"{APP_NAME}.{name}"
    return logging.getLogger(full_name)
