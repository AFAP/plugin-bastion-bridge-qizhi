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

import json
import platform
import re
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
from command_guard import CommandGuard
from config import load_config
from logger import (
    get_app_dir,
    get_config_path,
    get_log_path,
    get_logger,
    log_blank_line,
    log_console,
    setup_logging,
)
from params import parse_args
from ssh_client import SSHBridge
from api_server import start_api
from version import __version__, version_lines


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
    "ssh_prefer_shell": True,
    "dangerous_command_guard_enabled": True,
    "console_log": True,
    "console_verbose": False,
    "console_output_limit": 12000,
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


def _mask_inline_secret(value: str) -> str:
    """Redact credentials embedded in an option or SSH-style URL."""
    option_match = re.match(
        r"(?i)^(-(?:pw|password)|--(?:password|passwd|pass|token|secret|api[-_]?key)"
        r"|/(?:password|pass|token|secret|p))=(.*)$",
        value,
    )
    if option_match:
        return f"{option_match.group(1)}=***"

    credential_match = re.match(
        r"(?i)^(ssh://)?([^/@:\s]+):([^@\s]*)@(.+)$",
        value,
    )
    if credential_match:
        scheme = credential_match.group(1) or ""
        return f"{scheme}{credential_match.group(2)}:***@{credential_match.group(4)}"
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
            "-pw", "-password", "/p", "/password", "/pass", "/token", "/secret",
            "--password", "--passwd", "--pass", "--token", "--secret",
            "--api-key", "--api_key", "--apikey",
        }:
            masked.append(token)
            skip_next = True
        else:
            masked.append(_mask_inline_secret(token))
    return masked


def _sanitize_diagnostics(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a recursively sanitized copy of parser diagnostics."""

    def sanitize(value: Any, key: str = "") -> Any:
        key_lower = key.lower()
        sensitive_key = key_lower in {"p", "pass", "pwd", "api_key", "apikey"} or any(
            marker in key_lower
            for marker in ("password", "passwd", "secret", "token", "credential")
        )
        if sensitive_key:
            return _mask_password(value)
        if isinstance(value, dict):
            return {
                item_key: sanitize(item_value, str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            if key_lower.endswith("argv"):
                return _mask_argv([str(item) for item in value])
            return [sanitize(item) for item in value]
        if isinstance(value, str):
            return _mask_inline_secret(value)
        return value

    return sanitize(raw)


def _display_value(value: Any) -> str:
    """Format a value for the live console without exposing credentials."""
    if value is None or value == "" or value == {} or value == []:
        return "（未提供）"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _display_startup(argv: List[str]) -> None:
    """Show the bridge banner, version block and sanitized gateway arguments."""
    log_blank_line()
    log_console("╔══════════════════════════════════════════════════════════╗")
    log_console("║              齐治堡垒机 SSH 桥接器                       ║")
    log_console("║      Qizhi Bastion SSH Bridge · PuTTY 安全桥接模式       ║")
    log_console("╚══════════════════════════════════════════════════════════╝")
    for line in version_lines():
        log_console("    %s", line)
    log_console(">>> 齐治传入参数（敏感字段已脱敏） | 共 %d 项", len(argv))
    safe_argv = _mask_argv(argv)
    if not safe_argv:
        log_console("    （未收到命令行参数）")
    else:
        for index, value in enumerate(safe_argv, start=1):
            log_console("    [%02d] %s", index, value)
    log_blank_line()


def _display_runtime_config(config: Dict[str, Any], guard: CommandGuard) -> None:
    """Show the effective runtime settings (never credentials or command bodies)."""
    keepalive_enabled = bool(config.get("keepalive_enabled", True))
    keepalive_command = str(config.get("keepalive_command", "ls"))
    prefer_shell = bool(config.get("ssh_prefer_shell", True))
    log_console(">>> 运行配置")
    log_console("    运行环境   : Python %s / %s", platform.python_version(), platform.platform())
    log_console("    指令通道   : %s", "交互 Shell（默认）" if prefer_shell else "exec 通道")
    log_console(
        "    空闲保活   : %s | 间隔 %s 秒 | 命令长度 %d",
        "开启" if keepalive_enabled else "关闭",
        config.get("keepalive_interval_seconds", 180),
        len(keepalive_command),
    )
    log_console(
        "    控制台预览 : %s 字符 | 详细日志 %s",
        config.get("console_output_limit", 12000),
        "开启" if config.get("console_verbose", False) else "关闭",
    )
    log_console("    危险操作   : 兜底%s", "开启" if guard.enabled else "关闭")
    log_console("    配置文件   : %s", get_config_path())
    log_console("    文件日志   : %s", get_log_path())
    log_blank_line()


def _display_connection_params(params: ConnectionParams) -> None:
    """Show parsed connection fields with passwords represented by status only."""
    auth_methods: List[str] = []
    if params.password:
        auth_methods.append("密码（已接收，内容已隐藏）")
    if params.key_path:
        auth_methods.append("私钥")
    if not auth_methods:
        auth_methods.append("未提供凭证")

    safe_extra = _sanitize_diagnostics({"extra": params.extra}).get("extra", {})
    log_console(">>> 已解析连接参数")
    log_console("    目标服务器 : %s", _display_value(params.host))
    log_console("    SSH 端口   : %s", _display_value(params.port))
    log_console("    登录用户   : %s", _display_value(params.username))
    log_console("    连接协议   : %s", _display_value(params.protocol))
    log_console("    认证方式   : %s", " + ".join(auth_methods))
    log_console("    私钥路径   : %s", _display_value(params.key_path))
    log_console("    堡垒地址   : %s", _display_value(params.bastion_host))
    log_console("    扩展参数   : %s", _display_value(safe_extra))
    log_blank_line()


def _display_api_endpoint(host: str, port: int) -> None:
    """Show the local endpoint and useful discovery locations."""
    endpoint = f"http://{host}:{port}"
    log_console("✓ 本地桥接 API 已配置")
    log_console("    服务地址   : %s", endpoint)
    log_console("    健康检查   : %s/health", endpoint)
    log_console("    端口文件   : %s", get_app_dir() / "active_port.txt")
    log_console("    当前状态   : 等待 AI 或脚本发送指令")
    log_blank_line()


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
        log_console(">>> 正在连接 SSH | 第 %d/%d 次 | 超时 %d 秒", attempt, max_attempts, timeout)
        try:
            if bridge.connect(timeout=timeout):
                logger.info("SSH connected successfully")
                log_console(
                    "✓ SSH 连接成功 | %s@%s:%s",
                    bridge.params.username,
                    bridge.params.host,
                    bridge.params.port,
                )
                log_blank_line()
                return True
        except Exception as exc:
            logger.exception("SSH connect attempt %d failed: %s", attempt, exc)
        if attempt < max_attempts:
            logger.info("Retrying SSH connection in %d seconds...", delay)
            time.sleep(delay)
    logger.error("SSH connection failed after %d attempts", max_attempts)
    log_console(
        "! SSH 连接失败 | 已尝试 %d 次，API 将以诊断模式继续启动",
        max_attempts,
        level=40,
    )
    log_blank_line()
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
    # A broken config must not kill the process before anything is displayed:
    # fall back to defaults and say so on stderr.
    try:
        user_config = load_config()
    except Exception as exc:  # noqa: BLE001 - startup must survive a bad config
        sys.stderr.write(f"[QizhiSSHBridge] 配置加载失败，改用默认配置：{exc}\n")
        user_config = {}
    config = merge_config(user_config)

    # Initialize logging to %LOCALAPPDATA%\sshbridge\QizhiSSHBridge\logs\bridge.log.
    setup_logging(
        level=config.get("log_level", "INFO"),
        max_bytes=int(config.get("log_max_bytes", 10 * 1024 * 1024)),
        backup_count=int(config.get("log_backup_count", 5)),
        console=bool(config.get("console_log", True)),
        console_verbose=bool(config.get("console_verbose", False)),
    )
    global logger
    logger = get_logger(__name__)

    _display_startup(argv)

    logger.info("=" * 60)
    logger.info("Qizhi SSH Bridge (putty.exe replacement) started")
    logger.info("Bridge version: v%s", __version__)
    logger.info("Python version: %s", sys.version.replace("\n", " "))
    logger.info("Command line argument count: %d", len(argv))
    logger.info(
        "Runtime config: channel=%s prefer_shell=%s keepalive=%s interval=%ss "
        "console_verbose=%s output_limit=%s",
        "shell" if config.get("ssh_prefer_shell", True) else "exec",
        config.get("ssh_prefer_shell", True),
        config.get("keepalive_enabled", True),
        config.get("keepalive_interval_seconds", 180),
        config.get("console_verbose", False),
        config.get("console_output_limit", 12000),
    )
    for idx, arg in enumerate(_mask_argv(argv), start=1):
        logger.info("  argv[%d] = %r", idx, arg)

    # Parse arguments passed by the Qizhi gateway.
    params, raw_args, parse_error = parse_command_line(argv)
    if parse_error:
        logger.error("Failed to parse gateway arguments:\n%s", parse_error)
        # Still start the API server so diagnostics are reachable, but mark SSH as not connected.
        params = None

    if params is not None:
        _display_connection_params(params)
        safe_extra = _sanitize_diagnostics({"extra": params.extra}).get("extra", {})
        logger.info(
            "Parsed connection params: host=%s port=%s username=%s protocol=%s "
            "key_path=%s bastion=%s extra=%r",
            params.host,
            params.port,
            params.username,
            params.protocol,
            params.key_path,
            params.bastion_host,
            safe_extra,
        )
        for key, value in _sanitize_diagnostics(raw_args).items():
            logger.info("Raw/unknown argument: %s = %r", key, value)

    # 内部危险操作兜底：桥接器与 API 共用同一实例（见 command_guard.py）。
    command_guard = CommandGuard.from_config(config)
    _display_runtime_config(config, command_guard)

    # Bridge and connect.
    bridge: Optional[SSHBridge] = None
    if params is not None:
        try:
            bridge = SSHBridge(
                params,
                keepalive_enabled=bool(config.get("keepalive_enabled", True)),
                keepalive_interval_seconds=int(config.get("keepalive_interval_seconds", 180)),
                keepalive_command=str(config.get("keepalive_command", "ls")),
                command_guard=command_guard,
                prefer_shell=bool(config.get("ssh_prefer_shell", True)),
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
            ),
            command_guard=command_guard,
        )
        log_console("! 当前为诊断模式：未建立目标 SSH 连接", level=30)
        log_blank_line()

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
        port_file = get_app_dir() / "active_port.txt"
        port_file.write_text(f"{api_host}:{api_port}\n", encoding="utf-8")
        logger.info("Active API endpoint written to %s", port_file)
    except Exception as exc:
        logger.warning("Could not write active port file: %s", exc)

    try:
        _display_api_endpoint(api_host, api_port)
        start_api(
            bridge,
            host=api_host,
            port=api_port,
            console_output_limit=int(config.get("console_output_limit", 12000)),
            command_guard=command_guard,
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
        log_console("! 齐治桥接器已停止", level=30)

    return 0


if __name__ == "__main__":
    sys.exit(main())
