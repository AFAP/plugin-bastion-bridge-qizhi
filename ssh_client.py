"""
ssh_client.py

Paramiko-based SSH bridge for the Qizhi (齐治) SSH Bridge tool.

Implements the SSHBridge interface contract used by api_server.py/main.py:
  - connect(timeout=15) -> bool
  - execute(command, timeout=30) -> {"stdout": str, "stderr": str, "exit_code": int}
  - stream_execute(command) -> generator of {"type": "stdout"|"stderr", "data": str}
  - is_connected() -> bool
  - close() -> None

This module intentionally does not depend on any specific web framework so it can
be unit-tested in isolation and swapped out if the project moves away from
Flask/FastAPI later.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Optional

import paramiko

# 内部危险操作兜底（纯标准库）。缺失时应直接报错，而不是静默降级为「不拦截」。
from command_guard import BLOCKED_EXIT_CODE, CommandGuard, GuardDecision, build_blocked_result

# Sibling imports with fallbacks so this file can be syntax-checked standalone.
try:
    from connection import ConnectionParams
except ImportError:  # pragma: no cover - stub for isolated testing
    @dataclass
    class ConnectionParams:  # type: ignore[no-redef]
        host: str
        port: int
        username: str
        password: Optional[str] = None
        protocol: str = "ssh2"
        key_path: Optional[str] = None
        bastion_host: Optional[str] = None
        extra: Dict[str, str] = field(default_factory=dict)

try:
    from logger import get_logger
except ImportError:  # pragma: no cover - stub for isolated testing
    def get_logger(name: str) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


def _apply_paramiko_compat() -> None:
    """Widen Paramiko's algorithm lists to support older SSH peers.

    Bastion gateways and legacy Linux/network devices often only support older
    key-exchange, host-key or cipher algorithms.  Without this, newer Paramiko
    versions raise ``IncompatiblePeer: no acceptable kex algorithm``.

    Only algorithms actually registered in Paramiko's internal info dicts are
    included, to avoid ``KeyError`` at handshake time.
    """

    def _intersect(preferred: tuple, available: dict) -> tuple:
        """Return ``preferred`` filtered to algorithms present in ``available``."""
        return tuple(a for a in preferred if a in available)

    # KEX algorithms
    paramiko.Transport._preferred_kex = _intersect(
        (
            "curve25519-sha256",
            "curve25519-sha256@libssh.org",
            "ecdh-sha2-nistp256",
            "ecdh-sha2-nistp384",
            "ecdh-sha2-nistp521",
            "diffie-hellman-group16-sha512",
            "diffie-hellman-group14-sha256",
            "diffie-hellman-group14-sha1",
            "diffie-hellman-group1-sha1",
            "diffie-hellman-group-exchange-sha256",
            "diffie-hellman-group-exchange-sha1",
        ),
        paramiko.Transport._kex_info,
    )

    # Host key algorithms
    paramiko.Transport._preferred_keys = _intersect(
        (
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "rsa-sha2-512",
            "rsa-sha2-256",
            "ssh-rsa",
            "ssh-dss",
        ),
        paramiko.Transport._key_info,
    )

    # Ciphers
    paramiko.Transport._preferred_ciphers = _intersect(
        (
            "aes128-gcm@openssh.com",
            "aes256-gcm@openssh.com",
            "aes128-ctr",
            "aes192-ctr",
            "aes256-ctr",
            "aes128-cbc",
            "aes192-cbc",
            "aes256-cbc",
            "3des-cbc",
            "blowfish-cbc",
            "cast128-cbc",
            "arcfour",
            "arcfour128",
            "arcfour256",
        ),
        paramiko.Transport._cipher_info,
    )

    # MACs
    paramiko.Transport._preferred_macs = _intersect(
        (
            "hmac-sha2-256-etm@openssh.com",
            "hmac-sha2-512-etm@openssh.com",
            "hmac-sha2-256",
            "hmac-sha2-512",
            "hmac-sha1",
            "hmac-md5",
            "hmac-sha1-96",
            "hmac-md5-96",
        ),
        paramiko.Transport._mac_info,
    )

    # Some Paramiko versions use _preferred_pub_keys for signature algorithms.
    if hasattr(paramiko.Transport, "_preferred_pub_keys"):
        paramiko.Transport._preferred_pub_keys = paramiko.Transport._preferred_keys


# Apply compatibility settings once at module import time.
_apply_paramiko_compat()


class SSHBridge:
    """A thin wrapper around Paramiko for non-interactive and streaming execution."""

    def __init__(
        self,
        params: ConnectionParams,
        keepalive_enabled: bool = True,
        keepalive_interval_seconds: int = 180,
        keepalive_command: str = "ls",
        command_guard: Optional[CommandGuard] = None,
    ):
        self.params = params
        self._client: Optional[paramiko.SSHClient] = None
        self._transport: Optional[paramiko.Transport] = None
        self._shell: Optional[paramiko.Channel] = None
        self._lock = threading.RLock()
        self._logger = get_logger(__name__)

        # 危险操作兜底：默认启用；这里是 api_server 之外的兜底，保活命令同样受约束。
        self._command_guard: CommandGuard = command_guard if command_guard is not None else CommandGuard.default()

        # Set to True once the server proves it cannot honour exec_command
        # (replies with the login banner only).  From then on commands go
        # straight through the interactive shell channel, skipping the
        # wasted exec attempt and its warning noise.
        self._prefer_shell = False

        # Keep-alive configuration: if no command is sent for the configured
        # interval, an innocuous command is executed to keep the bastion/tunnel
        # session from timing out.
        self._keepalive_enabled = keepalive_enabled
        self._keepalive_interval_seconds = max(10, int(keepalive_interval_seconds))
        self._keepalive_command = keepalive_command or "ls"
        self._last_activity_time = time.time()
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop_event = threading.Event()
        # 正在执行中的指令数（>0 时保活顺延，避免打扰长任务或与在飞指令争抢通道）。
        self._inflight = 0
        self._inflight_lock = threading.Lock()

        self._logger.info(
            "SSHBridge initialized: target=%s@%s:%s protocol=%s bastion=%s "
            "keepalive=%s interval=%ss",
            params.username,
            params.host,
            params.port,
            params.protocol,
            params.bastion_host,
            self._keepalive_enabled,
            self._keepalive_interval_seconds,
        )

    def connect(self, timeout: int = 15) -> bool:
        """Open an SSH connection using the configured ConnectionParams."""
        with self._lock:
            if self.is_connected():
                self._logger.info("Already connected to %s:%s", self.params.host, self.params.port)
                return True

            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                # Load system host keys if available, but do not fail if absent.
                try:
                    client.load_system_host_keys()
                except Exception as exc:  # noqa: BLE001
                    self._logger.debug("Could not load system host keys: %s", exc)

                connect_kwargs: Dict[str, Any] = {
                    "hostname": self.params.host,
                    "port": self.params.port,
                    "username": self.params.username,
                    "timeout": timeout,
                    "look_for_keys": True,
                    "allow_agent": True,
                }

                if self.params.password:
                    connect_kwargs["password"] = self.params.password
                    self._logger.debug("Using password authentication")
                else:
                    self._logger.debug("No password provided; relying on key/agent auth")

                if self.params.key_path:
                    expanded_key = os.path.expandvars(os.path.expanduser(self.params.key_path))
                    if os.path.isfile(expanded_key):
                        connect_kwargs["key_filename"] = expanded_key
                        self._logger.info("Using private key: %s", expanded_key)
                    else:
                        self._logger.warning("Configured key_path not found: %s", expanded_key)

                # Telnet/ssh1 are legacy protocols in the contract; only ssh2 is supported.
                protocol = (self.params.protocol or "ssh2").lower()
                if protocol not in ("ssh", "ssh2"):
                    self._logger.error("Unsupported protocol requested: %s", self.params.protocol)
                    return False

                self._logger.info(
                    "Connecting to %s:%s as %s (timeout=%s)...",
                    self.params.host,
                    self.params.port,
                    self.params.username,
                    timeout,
                )
                client.connect(**connect_kwargs)
                self._client = client
                self._transport = client.get_transport()

                # Also keep an interactive shell channel open so that /input can
                # send data to a long-running process if the API chooses to use it.
                self._open_shell()

                # Start the background keep-alive thread.
                self._start_keepalive()

                self._logger.info("SSH connection established to %s:%s", self.params.host, self.params.port)
                return True

            except paramiko.AuthenticationException as exc:
                self._logger.error("Authentication failed for %s@%s:%s: %s", self.params.username, self.params.host, self.params.port, exc)
                self.close()
                return False
            except socket.error as exc:
                self._logger.error("Network error connecting to %s:%s: %s", self.params.host, self.params.port, exc)
                self.close()
                return False
            except Exception as exc:  # noqa: BLE001
                self._logger.error("SSH connection failed: %s", exc, exc_info=True)
                self.close()
                return False

    def _open_shell(self) -> None:
        """Open a persistent interactive shell channel (used by send_input)."""
        if not self._transport or not self._transport.is_active():
            return
        try:
            self._shell = self._transport.open_session()
            self._shell.get_pty(term="xterm", width=160, height=48)
            self._shell.invoke_shell()
            self._logger.info("Interactive shell channel opened")
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Could not open interactive shell channel: %s", exc)
            self._shell = None

    def _start_keepalive(self) -> None:
        """Start the background thread that sends keep-alive commands."""
        if not self._keepalive_enabled:
            return
        self._keepalive_stop_event.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="SSHKeepAlive",
            daemon=True,
        )
        self._keepalive_thread.start()
        self._logger.info(
            "Keep-alive thread started (interval=%ss, command_length=%d)",
            self._keepalive_interval_seconds,
            len(self._keepalive_command),
        )

    def _stop_keepalive(self) -> None:
        """Signal and join the keep-alive thread."""
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            self._keepalive_stop_event.set()
            self._keepalive_thread.join(timeout=2)
            if self._keepalive_thread.is_alive():
                self._logger.warning("Keep-alive thread did not stop cleanly")
        self._keepalive_thread = None

    def _keepalive_loop(self) -> None:
        """空闲满间隔时发无害命令保活；有指令在执行（含已下发未返回的长任务）则顺延。"""
        while not self._keepalive_stop_event.is_set():
            # Wake up every second to check for shutdown.
            self._keepalive_stop_event.wait(1)
            if self._keepalive_stop_event.is_set():
                break

            if not self._keepalive_due():
                if self._is_busy():
                    self._logger.debug("保活顺延：有指令正在执行")
                elif not self.is_connected():
                    self._logger.debug("Keep-alive skipped: not connected")
                continue

            elapsed = time.time() - self._last_activity_time
            self._logger.info(
                "No activity for %.0fs; sending keep-alive command (length=%d)",
                elapsed,
                len(self._keepalive_command),
            )
            try:
                # Use the same execution path as normal commands so that the
                # activity timestamp is updated automatically.
                self.execute(self._keepalive_command, timeout=10)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("Keep-alive command failed: %s", exc)

    def _bump_activity(self) -> None:
        """Record that a command was just executed, resetting keep-alive timer."""
        self._last_activity_time = time.time()

    def _mark_busy(self) -> None:
        """记录一条指令开始执行。"""
        with self._inflight_lock:
            self._inflight += 1

    def _mark_idle(self) -> None:
        """记录一条指令执行完毕，并把空闲计时重置到此刻。"""
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)
        self._bump_activity()

    def _is_busy(self) -> bool:
        """是否有指令正在执行。"""
        with self._inflight_lock:
            return self._inflight > 0

    def _keepalive_due(self) -> bool:
        """是否该发保活指令：空闲满间隔、已连接、且没有指令在执行。"""
        return (
            self._keepalive_enabled
            and self.is_connected()
            and not self._is_busy()
            and (time.time() - self._last_activity_time) >= self._keepalive_interval_seconds
        )

    def _guard_decision(self, command: str) -> Optional[GuardDecision]:
        """Return the guard decision for ``command`` (``None`` means allowed)."""
        decision = self._command_guard.check(command)
        if decision is not None:
            self._logger.warning(
                "危险操作已拒绝（未下发到服务器）| 命令长度=%d",
                len(command),
            )
        return decision

    @staticmethod
    def _drain_channel(channel: paramiko.Channel, max_wait: float = 2.0) -> None:
        """Read and discard any data currently waiting on ``channel``."""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if channel.recv_ready():
                channel.recv(4096)
            else:
                time.sleep(0.05)

    def _execute_via_shell(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """Execute a command through an interactive shell channel.

        Some bastion gateways or restricted shells do not honour
        ``exec_command`` and only allow interactive sessions.  In those cases
        we use the persistent PTY shell (opened at connect time) to run the
        command.  This avoids opening a second concurrent channel, which some
        servers reject with ``channelOpen too offen``.

        If the persistent shell is not available, we fall back to opening a
        fresh channel as a last resort.
        """
        channel: Optional[paramiko.Channel] = None
        close_when_done = False
        try:
            # Prefer the persistent shell channel opened during connect().
            if self._shell and not self._shell.closed:
                channel = self._shell
                self._logger.debug("Using persistent shell channel for command")
            else:
                transport = self._client.get_transport()
                if transport is None:
                    raise RuntimeError("No active transport")
                channel = transport.open_session()
                channel.get_pty(term="xterm", width=160, height=48)
                channel.invoke_shell()
                close_when_done = True
                self._logger.debug("Opened temporary shell channel for command")

            # Drain any stale output before running the command.
            self._drain_channel(channel, max_wait=1.0)

            # Disable command echo so the command does not appear in output.
            channel.send("stty -echo\n")
            time.sleep(0.2)
            self._drain_channel(channel, max_wait=0.5)

            # Unique markers so command output cannot accidentally match them.
            uid = uuid.uuid4().hex[:12]
            end_marker = f"---BRIDGE_END_{uid}---"
            exit_marker = f"---BRIDGE_EXIT_{uid}---"

            channel.send(f"{command}\necho {end_marker}\necho {exit_marker}:$?\n")

            output = b""
            found_end = False
            found_exit = False
            exit_code = -1
            deadline = time.time() + timeout

            while time.time() < deadline and not (found_end and found_exit):
                if channel.recv_ready():
                    chunk = channel.recv(4096)
                    output += chunk
                    text = output.decode("utf-8", errors="replace")
                    if end_marker in text:
                        found_end = True
                    if exit_marker in text:
                        found_exit = True
                else:
                    time.sleep(0.1)

            text = output.decode("utf-8", errors="replace")
            end_idx = text.find(end_marker)
            exit_idx = text.find(exit_marker)

            stdout = text[:end_idx] if end_idx != -1 else text
            if exit_idx != -1:
                exit_line = text[exit_idx:].splitlines()[0]
                try:
                    exit_code = int(exit_line.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass

            self._logger.info(
                "指令执行完毕（shell 通道），退出码=%s（stdout %d 字符）",
                exit_code,
                len(stdout),
            )
            return {"stdout": stdout.strip("\r\n"), "stderr": "", "exit_code": exit_code}

        except Exception as exc:  # noqa: BLE001
            self._logger.error("Interactive shell execution failed: %s", exc, exc_info=True)
            return {"stdout": "", "stderr": str(exc), "exit_code": -1, "error": str(exc)}
        finally:
            if close_when_done and channel:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass

    def _looks_like_login_only(self, output: str) -> bool:
        """Heuristic: detect when exec_command only returned login banner.

        We require the output to be relatively short and contain classic
        login-banner text.  This avoids false positives when a legitimate
        command happens to contain words like "connection" in its output.
        """
        if not output:
            return True
        if len(output) > 400:
            return False
        lowered = output.lower()
        login_indicators = [
            "connection closed",
            "last login",
            "authorized users only",
        ]
        return any(ind in lowered for ind in login_indicators)

    def execute(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        Execute a single command and return stdout, stderr and exit code.

        Returns a dict with keys:
            stdout: str
            stderr: str
            exit_code: int
            error: str   (only present when something went wrong; additive field)

        A refused command is never sent: the result is a normal shape with
        ``exit_code`` 126 and the prompt in ``stderr``.
        """
        with self._lock:
            decision = self._guard_decision(command)
            if decision is not None:
                return build_blocked_result(decision)

            if not self.is_connected():
                self._logger.error("execute() called while not connected")
                return {"stdout": "", "stderr": "SSH not connected", "exit_code": -1, "error": "SSH not connected"}

            self._mark_busy()
            try:
                self._bump_activity()
                self._logger.info("Executing command (length=%d)", len(command))

                # Once the server has proven it cannot honour exec_command,
                # skip the wasted exec attempt and use the shell channel directly.
                if self._prefer_shell:
                    return self._execute_via_shell(command, timeout=timeout)

                stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
                # Ensure stdin is closed so the remote command sees EOF.
                try:
                    stdin.channel.shutdown_write()
                except Exception:  # noqa: BLE001
                    pass

                exit_code = stdout.channel.recv_exit_status()
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")

                self._logger.info(
                    "指令执行完毕，退出码=%s（stdout %d 字符，stderr %d 字符）",
                    exit_code,
                    len(out),
                    len(err),
                )
                original_result = {"stdout": out, "stderr": err, "exit_code": exit_code}

                # Some bastion shells reject exec_command and only echo login banner.
                # Fall back to an interactive shell session in that case, but if the
                # fallback fails (e.g. server limits concurrent channels) return the
                # original exec_command result instead of losing it.
                if exit_code == 0 and not err and self._looks_like_login_only(out):
                    self._logger.warning(
                        "exec 通道被服务器关闭（仅返回登录横幅），改用交互 shell 通道执行"
                    )
                    fallback = self._execute_via_shell(command, timeout=timeout)
                    if fallback.get("exit_code", -1) == -1 and "error" in fallback:
                        self._logger.warning(
                            "Interactive shell fallback failed; returning original exec_command result"
                        )
                        return original_result
                    if not self._prefer_shell:
                        self._prefer_shell = True
                        self._logger.info(
                            "已确认该服务器不支持 exec 通道，后续指令将直接使用交互 shell 通道"
                        )
                    return fallback

                return original_result

            except Exception as exc:  # noqa: BLE001
                self._logger.error("Command execution failed: %s", exc, exc_info=True)
                self._logger.warning("Retrying command via interactive shell")
                return self._execute_via_shell(command, timeout=timeout)
            finally:
                self._mark_idle()

    def stream_execute(self, command: str) -> Generator[Dict[str, Any], None, None]:
        """
        Execute a command and yield output chunks as they arrive.

        Yields dicts of the form:
            {"type": "stdout", "data": str}
            {"type": "stderr", "data": str}
        After the command finishes an additional chunk is emitted:
            {"type": "exit", "exit_code": int}
        The "exit" chunk is an extension to the base contract and can be safely
        ignored by callers that only expect stdout/stderr.

        A refused command yields the prompt as a ``stderr`` chunk plus
        ``{"type": "exit", "exit_code": 126}`` and is never sent to the server.
        """
        decision = self._guard_decision(command)
        if decision is not None:
            yield {"type": "stderr", "data": decision.message}
            yield {"type": "exit", "exit_code": BLOCKED_EXIT_CODE}
            return

        if not self.is_connected():
            self._logger.error("stream_execute() called while not connected")
            yield {"type": "stderr", "data": "SSH not connected"}
            return

        channel: Optional[paramiko.Channel] = None
        self._mark_busy()
        try:
            self._bump_activity()
            self._logger.info("Streaming command (length=%d)", len(command))
            transport = self._client.get_transport()
            if transport is None:
                raise RuntimeError("No active transport")

            channel = transport.open_session()
            # No PTY here so stdout/stderr remain separate, matching the contract.
            channel.exec_command(command)
            # Close stdin so the remote command sees EOF and can finish cleanly.
            try:
                channel.shutdown_write()
            except Exception:  # noqa: BLE001
                pass

            while True:
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break

                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        yield {"type": "stdout", "data": data}

                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        yield {"type": "stderr", "data": data}

                # Small sleep to avoid spinning the CPU while waiting for output.
                time.sleep(0.01)

            exit_code = channel.recv_exit_status()
            yield {"type": "exit", "exit_code": exit_code}
            self._logger.info("流式指令执行完毕，退出码=%s", exit_code)

        except Exception as exc:  # noqa: BLE001
            self._logger.error("Stream execution failed: %s", exc, exc_info=True)
            yield {"type": "stderr", "data": f"Stream error: {exc}"}
        finally:
            self._mark_idle()
            if channel is not None:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass

    def send_input(self, data: str) -> bool:
        """
        Send data to the persistent interactive shell channel.

        This is used by api_server POST /input to drive an interactive session.
        Returns True if the data was sent, False otherwise.  Data that would run
        a dangerous command is refused (returns False) and never sent.
        """
        with self._lock:
            if self._command_guard.check_input(data) is not None:
                self._logger.warning(
                    "交互输入命中危险操作，已拒绝发送 | 长度=%d",
                    len(data),
                )
                return False

            if not self._shell or self._shell.closed:
                self._logger.warning("send_input() called but no interactive shell is open")
                return False
            try:
                self._bump_activity()
                payload = data.encode("utf-8", errors="replace")
                self._shell.send(payload)
                self._logger.debug("Sent %d bytes to interactive shell", len(payload))
                return True
            except Exception as exc:  # noqa: BLE001
                self._logger.error("Failed to send input to shell: %s", exc)
                return False

    def is_connected(self) -> bool:
        """Return True if the underlying transport is active."""
        return (
            self._client is not None
            and self._transport is not None
            and self._transport.is_active()
        )

    def close(self) -> None:
        """Close all channels and the SSH client."""
        self._stop_keepalive()
        with self._lock:
            self._logger.info("Closing SSH connection to %s:%s", self.params.host, self.params.port)
            if self._shell:
                try:
                    self._shell.close()
                except Exception as exc:  # noqa: BLE001
                    self._logger.debug("Error closing shell channel: %s", exc)
                self._shell = None

            if self._client:
                try:
                    self._client.close()
                except Exception as exc:  # noqa: BLE001
                    self._logger.debug("Error closing SSH client: %s", exc)
                self._client = None

            self._transport = None
