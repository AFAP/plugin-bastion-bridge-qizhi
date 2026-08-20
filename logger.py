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
from typing import Any, Optional


#: Name of the application; used for the log directory and logger names.
APP_NAME = "QizhiSSHBridge"

# Records carrying this attribute are intended for the live console only.  The
# command and response body previews use it so operational details stay out of
# the persistent rotating log file.
_CONSOLE_ONLY_ATTR = "qizhi_console_only"
_CONSOLE_HANDLER_ATTR = "qizhi_console_handler"


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
    """Enable ANSI colors when stderr is attached to a compatible terminal."""
    if not getattr(sys.stderr, "isatty", lambda: False)():
        return False
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
    except Exception:  # noqa: BLE001 - color support is best effort
        return False


class _ExcludeConsoleOnlyFilter(logging.Filter):
    """Keep console-only previews out of persistent log handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not bool(getattr(record, _CONSOLE_ONLY_ATTR, False))


class _ConsoleVisibilityFilter(logging.Filter):
    """Keep the default console focused on pretty blocks and actionable issues."""

    def __init__(self, verbose: bool = False) -> None:
        super().__init__()
        self.verbose = verbose

    def filter(self, record: logging.LogRecord) -> bool:
        return (
            self.verbose
            or bool(getattr(record, _CONSOLE_ONLY_ATTR, False))
            or record.levelno >= logging.WARNING
        )


class _ConsoleFormatter(logging.Formatter):
    """Readable console formatter with colors for request/response blocks."""

    _CYAN = "\033[96m"
    _GREEN = "\033[92m"
    _YELLOW = "\033[93m"
    _RED = "\033[91m"
    _MAGENTA = "\033[95m"
    _RESET = "\033[0m"

    def __init__(self, *args: Any, use_colors: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        # Pretty blocks do not need a timestamp and logger name on every line.
        if getattr(record, _CONSOLE_ONLY_ATTR, False):
            text = record.getMessage()
            if record.exc_info:
                text = f"{text}\n{self.formatException(record.exc_info)}"
        else:
            text = super().format(record)

        if not self.use_colors:
            return text

        message = record.getMessage()
        if record.levelno >= logging.ERROR:
            color = self._RED
        elif record.levelno >= logging.WARNING or message.startswith("!"):
            color = self._YELLOW
        elif message.startswith(">>>"):
            color = self._CYAN
        elif message.startswith("<<<") or message.startswith("✓"):
            color = self._GREEN
        elif message.startswith(("╔", "║", "╚")):
            color = self._MAGENTA
        else:
            return text
        return f"{color}{text}{self._RESET}"


def setup_logging(
    level: int = logging.DEBUG,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    console: bool = True,
    console_verbose: bool = False,
) -> logging.Logger:
    """Configure the root application logger.

    Args:
        level: Minimum log level (default ``logging.DEBUG``).
        max_bytes: Maximum size of ``bridge.log`` before rotation.
        backup_count: Number of rotated log files to keep.
        console: Also emit logs to ``stderr`` when ``True``. Useful during
            development; can be disabled for the packaged EXE to keep the
            console clean.
        console_verbose: Show routine technical INFO/DEBUG records in addition
            to the readable live blocks and warnings/errors.

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
        file_handler.addFilter(_ExcludeConsoleOnlyFilter())
        root_logger.addHandler(file_handler)
    except OSError as exc:
        # If we cannot write to the log file, at least complain on stderr.
        sys.stderr.write(f"[QizhiSSHBridge] Failed to open log file: {exc}\n")

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        setattr(console_handler, _CONSOLE_HANDLER_ATTR, True)
        console_handler.addFilter(_ConsoleVisibilityFilter(console_verbose))
        console_handler.setFormatter(
            _ConsoleFormatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
                use_colors=_enable_ansi_colors(),
            )
        )
        root_logger.addHandler(console_handler)

    root_logger.info("Logging initialized. log_path=%s", get_log_path())
    return root_logger


def log_console(message: str, *args: Any, level: int = logging.INFO) -> None:
    """Write a live-only message that is explicitly excluded from log files."""
    logging.getLogger(APP_NAME).log(
        level,
        message,
        *args,
        extra={_CONSOLE_ONLY_ATTR: True},
    )


def log_blank_line() -> None:
    """Write a bare separator line to the live console only."""
    root_logger = logging.getLogger(APP_NAME)
    for handler in root_logger.handlers:
        if not getattr(handler, _CONSOLE_HANDLER_ATTR, False):
            continue
        stream = getattr(handler, "stream", None)
        if stream is None:
            continue
        try:
            stream.write("\n")
            handler.flush()
        except Exception:  # noqa: BLE001 - cosmetic separator only
            pass


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under the application namespace."""
    full_name = APP_NAME if name is None else f"{APP_NAME}.{name}"
    return logging.getLogger(full_name)
