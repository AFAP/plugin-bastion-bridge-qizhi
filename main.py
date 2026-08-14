"""Entry point for the Qizhi (齐治) PuTTY bridge executable.

Workflow:
    1. Parse the command line arguments passed by the Qizhi gateway.
    2. Load the JSON configuration (listening port, log level, etc.).
    3. Initialize the logger so every argument and decision is recorded.
    4. Connect to the target Linux server through Paramiko.
    5. Start the local Flask/SocketIO API so the AI assistant can drive the shell.
    6. Keep the process alive until the user closes it.

The module is deliberately defensive: malformed arguments from 齐治 are logged
and reported, but the process stays alive long enough for the health endpoint
to explain what happened.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import traceback
import warnings
from typing import Any, Dict, List, Optional, Tuple

# Suppress noisy deprecation warnings from older Paramiko / cryptography
# combinations (e.g. TripleDES warnings).  They do not affect functionality.
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
except ImportError:
    pass

from connection import ConnectionParams
from config import load_config
from logger import get_logger, setup_logging
from params import parse_args
from ssh_client import SSHBridge
from api_server import start_api


# Default configuration values.  These are overridden by %LOCALAPPDATA%\sshbridge\QizhiSSHBridge\config.json.
DEFAULT_CONFIG: Dict[str, Any] = {
    "api_host": "127.0.0.1",
    "api_port": 8766,
    "ssh_connect_timeout": 15,
    "retry_connect": True,
    "retry_max_attempts": 3,
    "retry_delay_seconds": 2,
    "exit_on_connect_failure": False,
    "log_level": "INFO",
}


def merge_config(user_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge user config over defaults."""
    merged = dict(DEFAULT_CONFIG)
    if user_config:
        merged.update(user_config)
    return merged


def _mask_password(value: Any) -> Any:
    """Replace password-like strings with a mask for safe logging."""
    if isinstance(value, str) and value:
        return "***"
    return value


def _mask_argv(argv: List[str]) -> List[str]:
    """Mask values that follow known password-like flags in argv."""
    masked: List[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            masked.append("***")
            skip_next = False
            continue
        lower = token.lower()
        if lower in {
            "/p", "/password", "/pass", "--password", "--passwd", "--pass",
        }:
            masked.append(token)
            skip_next = True
        else:
            masked.append(token)
    return masked


def _sanitize_diagnostics(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the diagnostics dict with credentials redacted."""
    import copy

    safe = copy.deepcopy(raw)
    ns = safe.get("parser_namespace")
    if isinstance(ns, dict):
        for key in list(ns.keys()):
            lower = key.lower()
            if "password" in lower or "pass" in lower or "secret" in lower or "token" in lower:
                ns[key] = _mask_password(ns[key])

    # Also mask tokens that appear next to known password flags in argv lists.
    for key in ("raw_argv", "rewritten_argv"):
        argv = safe.get(key) or []
        if isinstance(argv, list):
            safe[key] = _mask_argv(argv)

    if "final_params" in safe and isinstance(safe["final_params"], dict):
        fp = safe["final_params"]
        for key in list(fp.keys()):
            if "password" in key.lower():
                fp[key] = _mask_password(fp[key])

    return safe


def parse_command_line(argv: List[str]) -> Tuple[Optional[ConnectionParams], Dict[str, Any], Optional[str]]:
    """Parse the gateway arguments and return (params, raw_args, error_message).

    Any exception is caught and returned as an error string so the process can
    log diagnostics instead of crashing silently.
    """
    try:
        params, raw = parse_args(argv)
        return params, raw, None
    except Exception as exc:
        return None, {}, f"parse_args failed: {exc}\n{traceback.format_exc()}"


def _find_available_port(host: str, start_port: int, max_attempts: int = 10) -> int:
    """Return the first available port starting from ``start_port``.

    If the configured port is already in use (e.g. another bridge instance is
    running), increment the port until a free one is found.
    """
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    logger.warning(
        "Could not find a free port in range %d-%d; falling back to %d",
        start_port,
        start_port + max_attempts - 1,
        start_port,
    )
    return start_port


def connect_with_retries(
    bridge: SSHBridge,
    timeout: int,
    max_attempts: int,
    delay: int,
) -> bool:
    """Try to establish the SSH connection with a small retry loop."""
    for attempt in range(1, max_attempts + 1):
        logger.info("SSH connect attempt %d/%d (timeout=%d)", attempt, max_attempts, timeout)
        try:
            if bridge.connect(timeout=timeout):
                logger.info("SSH connected successfully")
                return True
        except Exception as exc:
            logger.exception("SSH connect attempt %d failed: %s", attempt, exc)
        if attempt < max_attempts:
            logger.info("Retrying SSH connection in %d seconds...", delay)
            time.sleep(delay)
    logger.error("SSH connection failed after %d attempts", max_attempts)
    return False


def main(argv: Optional[List[str]] = None) -> int:
    """Application entry point."""
    if argv is None:
        argv = sys.argv[1:]

    # Ensure stdout/stderr are UTF-8 on Windows so Chinese log messages render.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    # Load config before setting up logging because it may contain log_level.
    user_config = load_config()
    config = merge_config(user_config)

    # Initialize logging to %LOCALAPPDATA%\sshbridge\QizhiSSHBridge\logs\bridge.log.
    setup_logging(
        level=config.get("log_level", "INFO"),
        max_bytes=int(config.get("log_max_bytes", 10 * 1024 * 1024)),
        backup_count=int(config.get("log_backup_count", 5)),
        console=bool(config.get("console_log", True)),
    )
    global logger
    logger = get_logger(__name__)

    logger.info("=" * 60)
    logger.info("Qizhi SSH Bridge (putty.exe replacement) started")
    logger.info("Python version: %s", sys.version.replace("\n", " "))
    logger.info("Command line argument count: %d", len(argv))
    for idx, arg in enumerate(_mask_argv(argv), start=1):
        logger.info("  argv[%d] = %r", idx, arg)

    # Parse arguments passed by the Qizhi gateway.
    params, raw_args, parse_error = parse_command_line(argv)
    if parse_error:
        logger.error("Failed to parse gateway arguments:\n%s", parse_error)
        # Still start the API server so diagnostics are reachable, but mark SSH as not connected.
        params = None

    if params is not None:
        logger.info(
            "Parsed connection params: host=%s port=%s username=%s protocol=%s "
            "key_path=%s bastion=%s extra=%r",
            params.host,
            params.port,
            params.username,
            params.protocol,
            params.key_path,
            params.bastion_host,
            params.extra,
        )
        for key, value in _sanitize_diagnostics(raw_args).items():
            logger.info("Raw/unknown argument: %s = %r", key, value)

    # Bridge and connect.
    bridge: Optional[SSHBridge] = None
    if params is not None:
        try:
            bridge = SSHBridge(
                params,
                keepalive_enabled=bool(config.get("keepalive_enabled", True)),
                keepalive_interval_seconds=int(config.get("keepalive_interval_seconds", 60)),
                keepalive_command=str(config.get("keepalive_command", "ls")),
            )
            connected = connect_with_retries(
                bridge,
                timeout=int(config["ssh_connect_timeout"]),
                max_attempts=int(config["retry_max_attempts"]),
                delay=int(config["retry_delay_seconds"]),
            )
            if not connected and config.get("exit_on_connect_failure", False):
                logger.error("exit_on_connect_failure is true; shutting down")
                return 1
        except Exception as exc:
            logger.exception("Unexpected error creating SSH bridge: %s", exc)
            bridge = None

    if bridge is None:
        # Create a dummy bridge so the API can still expose /health diagnostics.
        logger.warning("Creating no-op bridge for diagnostic mode")
        bridge = SSHBridge(
            ConnectionParams(
                host="127.0.0.1",
                port=22,
                username="unknown",
            )
        )

    # Start the local API server (blocks until the process is killed).
    api_host = str(config["api_host"])
    desired_api_port = int(config["api_port"])
    api_port = _find_available_port(api_host, desired_api_port)
    if api_port != desired_api_port:
        logger.warning(
            "Configured API port %d is in use; using fallback port %d",
            desired_api_port,
            api_port,
        )
    else:
        logger.info("Using configured API port %d", api_port)

    # Write the active API endpoint to a well-known file so an AI assistant can
    # discover it even when a fallback port was chosen.
    try:
        from logger import get_app_dir
        port_file = get_app_dir() / "active_port.txt"
        port_file.write_text(f"{api_host}:{api_port}\n", encoding="utf-8")
        logger.info("Active API endpoint written to %s", port_file)
    except Exception as exc:
        logger.warning("Could not write active port file: %s", exc)

    try:
        start_api(
            bridge,
            host=api_host,
            port=api_port,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user, shutting down")
    except Exception as exc:
        logger.exception("API server exited with error: %s", exc)
        return 1
    finally:
        try:
            bridge.close()
        except Exception as exc:
            logger.debug("Error closing bridge: %s", exc)
        logger.info("Qizhi SSH Bridge (putty.exe replacement) stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
