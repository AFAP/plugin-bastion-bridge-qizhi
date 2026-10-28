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

import codecs
import logging
import os
import re
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
        prefer_shell: bool = True,
    ):
        self.params = params
        self._client: Optional[paramiko.SSHClient] = None
        self._transport: Optional[paramiko.Transport] = None
        self._shell: Optional[paramiko.Channel] = None
        # Connection/channel lifecycle (short critical sections).
        self._lock = threading.RLock()
        # Serializes execute/stream: both may share the persistent shell, so a
        # second script must never be injected while the first is still running.
        self._command_lock = threading.Lock()
        # Serializes writes to a channel (command script vs. interactive input).
        self._send_lock = threading.Lock()
        self._logger = get_logger(__name__)

        # 危险操作兜底：默认启用；这里是 api_server 之外的兜底，保活命令同样受约束。
        self._command_guard: CommandGuard = command_guard if command_guard is not None else CommandGuard.default()

        # 默认走持久交互 shell 通道。齐治接入客户端经本地端口转发时常把
        # exec_command 变成一段登录横幅，只有交互 shell 才能真正执行命令；
        # 从 shell 起步可以避免「第一条命令先撞横幅、被回退逻辑重跑一遍」。
        self._prefer_shell = bool(prefer_shell)

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
            "keepalive=%s interval=%ss channel=%s",
            params.username,
            params.host,
            params.port,
            params.protocol,
            params.bastion_host,
            self._keepalive_enabled,
            self._keepalive_interval_seconds,
            "shell" if self._prefer_shell else "exec",
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
        with self._lock:
            transport = self._transport
        if not transport or not transport.is_active():
            return
        try:
            channel = transport.open_session()
            channel.get_pty(term="xterm", width=160, height=48)
            channel.invoke_shell()
            with self._lock:
                self._shell = channel
            self._logger.info("Interactive shell channel opened")
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Could not open interactive shell channel: %s", exc)
            with self._lock:
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
    def _drain_channel(channel: paramiko.Channel, max_wait: float = 0.5) -> None:
        """Read and discard stale data, returning once the channel goes quiet.

        The previous version always waited the full ``max_wait``; with the
        interactive shell as the default execution channel that added ~1.7 s
        of latency to every command.  Stop after 100 ms of silence instead.
        """
        deadline = time.monotonic() + max(0.0, max_wait)
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            try:
                if channel.recv_ready():
                    channel.recv(4096)
                    last_data = time.monotonic()
                    continue
            except Exception:  # noqa: BLE001 - channel closed mid-drain
                return
            if time.monotonic() - last_data >= 0.1:
                return
            time.sleep(0.02)

    def _ensure_shell_channel(self) -> tuple[Optional[paramiko.Channel], bool]:
        """Return a shell channel and whether it is temporary (caller must close)."""
        with self._lock:
            if self._shell is not None and not self._shell.closed:
                return self._shell, False
            client = self._client
        if client is None:
            raise RuntimeError("No SSH client")
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("No active transport")
        channel = transport.open_session()
        channel.get_pty(term="xterm", width=160, height=48)
        channel.invoke_shell()
        self._logger.debug("Opened temporary shell channel for command")
        return channel, True

    def _prepare_shell_channel(self, channel: paramiko.Channel) -> None:
        """Discard stale output and turn off input echo before sending a command."""
        # A fresh PTY may print the bastion login banner; allow up to a second
        # while still returning as soon as the channel goes quiet.
        self._drain_channel(channel, max_wait=1.0)
        self._send_all(channel, b"stty -echo\n", timeout=10.0, lock=False)
        time.sleep(0.05)
        self._drain_channel(channel, max_wait=0.2)

    def _send_all(
        self,
        channel: paramiko.Channel,
        data: bytes,
        timeout: float = 30.0,
        lock: bool = True,
    ) -> None:
        """Send all bytes with a write timeout.

        ``Channel.send`` is allowed to send only part of the buffer (and the
        old code ignored its return value), so a long command could be
        silently truncated.  ``sendall`` loops until everything is written or
        raises ``socket.timeout``.
        """
        if lock:
            with self._send_lock:
                self._send_all(channel, data, timeout=timeout, lock=False)
            return
        previous = channel.gettimeout()
        channel.settimeout(timeout)
        try:
            channel.sendall(data)
        finally:
            channel.settimeout(previous)

    @staticmethod
    def _shell_script(
        command: str,
        end_marker: str,
        exit_marker: str,
        rc_var: str,
        tmp_var: str,
        eof_marker: str,
        tmp_name: str,
    ) -> str:
        """Run the command from a temp file, then print sentinel-prefixed markers.

        Why a temp file and a one-line epilogue?  If the command is sent as
        ``read answer`` followed by ``rc=$?`` on the next line, the terminal
        input queue already contains the wrapper, so ``read`` consumes
        ``rc=$?`` instead of waiting for the user.  Writing the command to a
        file and sourcing it from a *single* epilogue line means the shell has
        already consumed that whole line before ``read`` runs; only real user
        input is left in the terminal queue.

        The markers are prefixed with ``\\x01`` (SOH).  The echoed script only
        contains the literal text ``\\001``, never the control byte, so marker
        detection works even when ``stty -echo`` is unavailable, and no line
        anchoring is needed (a prompt without a newline can be delivered
        immediately).
        """
        return (
            f'{tmp_var}=$(mktemp 2>/dev/null) || '
            f'{tmp_var}="${{TMPDIR:-${{HOME:-/tmp}}}}/{tmp_name}"\n'
            f'(umask 077; cat > "${tmp_var}") <<\'{eof_marker}\'\n'
            f"{command}\n"
            f"{eof_marker}\n"
            f'. "${tmp_var}"; {rc_var}=$?; rm -f "${tmp_var}"; '
            f"printf '\\001{end_marker}\\n'; "
            f"printf '\\001{exit_marker}:%s\\n' \"${rc_var}\"; "
            f"unset {rc_var}\n"
        )

    @staticmethod
    def _marker_patterns(end_marker: str, exit_marker: str) -> tuple[re.Pattern, re.Pattern]:
        """Compile patterns for the sentinel-prefixed marker lines.

        ``\\x01`` is emitted by ``printf`` at runtime; an echoed script only
        shows the literal ``\\001`` characters, so the echo can never match.
        The trailing newline is required, so a marker split across SSH packets
        (or ``:1`` before ``:123``) is not parsed early.
        """
        sentinel = b"\x01"
        end_pattern = re.compile(
            re.escape(sentinel + end_marker.encode()) + rb"\r?\n"
        )
        exit_pattern = re.compile(
            re.escape(sentinel + exit_marker.encode()) + rb":(-?\d+)\r?\n"
        )
        return end_pattern, exit_pattern

    @staticmethod
    def _marker_prefix_len(buffer: bytes, markers: tuple[bytes, ...]) -> int:
        """Length of a suffix that could be the start of a marker (keep it)."""
        longest = 0
        for marker in markers:
            limit = min(len(marker) - 1, len(buffer))
            for size in range(limit, 0, -1):
                if buffer.endswith(marker[:size]):
                    longest = max(longest, size)
                    break
        return longest

    def _reset_shell_channel(self) -> None:
        """Recreate the persistent shell after a timeout left a command running."""
        with self._lock:
            shell = self._shell
            self._shell = None
        if shell is not None:
            try:
                shell.close()
            except Exception as exc:  # noqa: BLE001
                self._logger.debug("Error closing stale shell channel: %s", exc)
        try:
            self._open_shell()
            self._logger.info("交互 shell 已重建")
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Could not reopen interactive shell: %s", exc)

    def _execute_via_shell(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """Execute a command through an interactive shell channel.

        The Qizhi access client often does not honour ``exec_command`` over its
        local port forward (it returns a login banner instead), so this is the
        default channel.  The command is written to a temp file and sourced
        from a single epilogue line, so ``read``/``sudo`` see only real user
        input; sentinel-prefixed markers delimit output and carry the real
        ``$?`` exit status.
        """
        channel: Optional[paramiko.Channel] = None
        close_when_done = False
        persistent = False
        try:
            channel, close_when_done = self._ensure_shell_channel()
            persistent = not close_when_done

            # Unique markers so command output cannot accidentally match them.
            uid = uuid.uuid4().hex[:12]
            end_marker = f"---BRIDGE_END_{uid}---"
            exit_marker = f"---BRIDGE_EXIT_{uid}---"
            rc_var = f"__BRIDGE_RC_{uid}"
            tmp_var = f"__BRIDGE_TMP_{uid}"
            eof_marker = f"BRIDGE_EOF_{uid}"
            script = self._shell_script(
                command,
                end_marker,
                exit_marker,
                rc_var,
                tmp_var,
                eof_marker,
                f"bridge_{uid}.sh",
            )

            # Hold the write lock across "prepare + script" so an /input write
            # cannot slip in between stty and the command.
            with self._send_lock:
                self._prepare_shell_channel(channel)
                self._send_all(channel, script.encode("utf-8"), timeout=30.0, lock=False)

            end_pattern, exit_pattern = self._marker_patterns(end_marker, exit_marker)
            output = b""
            end_match = None
            exit_match = None
            timed_out = False
            channel_closed = False
            deadline = time.monotonic() + max(1.0, float(timeout))

            while not (end_match and exit_match):
                progressed = False
                try:
                    while channel.recv_ready():
                        chunk = channel.recv(4096)
                        if not chunk:
                            break
                        output += chunk
                        progressed = True
                        if time.monotonic() >= deadline:
                            # A command that keeps writing must not starve the
                            # deadline check in the outer loop.
                            timed_out = True
                            break
                except Exception as exc:  # noqa: BLE001 - treat as channel closure
                    self._logger.debug("Shell read failed: %s", exc)
                    channel_closed = True
                    break

                if timed_out:
                    # The deadline may have fired on the same read that carried
                    # the final markers; accept a complete result if we have one.
                    end_match = end_pattern.search(output)
                    exit_match = exit_pattern.search(output)
                    if end_match and exit_match:
                        timed_out = False
                    else:
                        break
                end_match = end_pattern.search(output)
                exit_match = exit_pattern.search(output)
                if end_match and exit_match:
                    break
                if channel.closed:
                    channel_closed = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if not progressed:
                    time.sleep(0.02)

            stdout = (
                output[:end_match.start()].decode("utf-8", errors="replace")
                if end_match
                else output.decode("utf-8", errors="replace")
            )
            exit_code = int(exit_match.group(1)) if exit_match else -1

            result: Dict[str, Any] = {
                "stdout": stdout.strip("\r\n"),
                "stderr": "",
                "exit_code": exit_code,
            }
            if timed_out:
                result["exit_code"] = -1
                result["error"] = f"命令执行超时（>{timeout} 秒）"
                self._logger.warning("指令执行超时（shell 通道），将重建交互 shell")
                if persistent:
                    self._reset_shell_channel()
            elif channel_closed and not (end_match and exit_match):
                result["exit_code"] = -1
                result["error"] = "交互 shell 通道已关闭，命令可能仍在远端运行"
                self._logger.warning(result["error"])

            self._logger.info(
                "指令执行完毕（shell 通道），退出码=%s（stdout %d 字符）",
                result["exit_code"],
                len(result["stdout"]),
            )
            return result

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
        """Heuristic: detect when exec_command only returned a login banner.

        Empty output is deliberately *not* treated as a banner: plenty of
        successful commands (``systemctl restart``, ``mkdir``, ``touch``)
        print nothing.  The old behaviour re-ran those commands through the
        shell, duplicating their side effects.
        """
        if not output:
            return False
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

        The default channel is the interactive shell (see ``prefer_shell``);
        the exec channel is kept for environments where it works.
        """
        decision = self._guard_decision(command)
        if decision is not None:
            return build_blocked_result(decision)

        if not self.is_connected():
            self._logger.error("execute() called while not connected")
            return {"stdout": "", "stderr": "SSH not connected", "exit_code": -1, "error": "SSH not connected"}

        # Serialize commands (not input): send_input() must still be able to
        # reach an interactive command while this lock is held.
        with self._command_lock:
            self._mark_busy()
            try:
                self._bump_activity()
                self._logger.info(
                    "Executing command (length=%d, channel=%s)",
                    len(command),
                    "shell" if self._prefer_shell else "exec",
                )

                if self._prefer_shell:
                    return self._execute_via_shell(command, timeout=timeout)
                return self._execute_via_exec(command, timeout=timeout)
            finally:
                self._mark_idle()

    def _execute_via_exec(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """Execute through a dedicated exec channel (non-PTY, stderr separate)."""
        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            # Ensure stdin is closed so the remote command sees EOF.
            try:
                stdin.channel.shutdown_write()
            except Exception:  # noqa: BLE001
                pass

            channel = stdout.channel
            try:
                out, err, exit_code, error = self._read_exec_output(channel, timeout)
            finally:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass
            result: Dict[str, Any] = {"stdout": out, "stderr": err, "exit_code": exit_code}

            if error:
                result["error"] = error
                self._logger.warning("exec 通道执行未完成：%s", error)
                return result

            self._logger.info(
                "指令执行完毕，退出码=%s（stdout %d 字符，stderr %d 字符）",
                exit_code,
                len(out),
                len(err),
            )

            # Some bastions reject exec_command and only echo a login banner.
            # Do NOT retry the command through the shell here: the exec attempt
            # may already have run it, and a retry would duplicate side effects.
            # Tell the caller to switch channels instead.
            if exit_code == 0 and not err and self._looks_like_login_only(out):
                self._logger.warning(
                    "exec 通道仅返回登录横幅；请将 ssh_prefer_shell 设为 true 使用交互 shell"
                )
                return {
                    "stdout": out,
                    "stderr": "",
                    "exit_code": -1,
                    "error": "exec 通道仅返回登录横幅，命令可能未执行；请使用交互 shell 通道（ssh_prefer_shell=true）",
                }

            return result

        except Exception as exc:  # noqa: BLE001
            # Do not silently retry through the shell: the command may already
            # have produced side effects, and a retry would run it twice.
            self._logger.error("Command execution failed: %s", exc, exc_info=True)
            return {"stdout": "", "stderr": str(exc), "exit_code": -1, "error": str(exc)}

    @staticmethod
    def _read_exec_output(
        channel: paramiko.Channel, timeout: int
    ) -> tuple[str, str, int, Optional[str]]:
        """Read stdout/stderr until the channel exits, honouring a hard deadline.

        ``recv_exit_status`` must never be called before the output is drained:
        a command writing more than the SSH window can hold would block the
        remote side forever.  We therefore poll both streams, then call
        ``recv_exit_status`` only once ``exit_status_ready`` is true.
        """
        deadline = time.monotonic() + max(1.0, float(timeout))
        out = bytearray()
        err = bytearray()

        def drain() -> bool:
            """Drain available data; return False when the deadline is exceeded."""
            while channel.recv_ready():
                chunk = channel.recv(65536)
                if not chunk:
                    break
                out.extend(chunk)
                if time.monotonic() >= deadline:
                    return False
            while channel.recv_stderr_ready():
                chunk = channel.recv_stderr(65536)
                if not chunk:
                    break
                err.extend(chunk)
                if time.monotonic() >= deadline:
                    return False
            return True

        def drain_all() -> None:
            """Drain everything after exit; the command ended, data is finite."""
            while channel.recv_ready():
                chunk = channel.recv(65536)
                if not chunk:
                    break
                out.extend(chunk)
            while channel.recv_stderr_ready():
                chunk = channel.recv_stderr(65536)
                if not chunk:
                    break
                err.extend(chunk)

        while True:
            try:
                if not drain():
                    if channel.exit_status_ready():
                        # Command finished right at the deadline; collect ALL
                        # remaining output (no deadline) instead of silently
                        # returning a truncated stdout with exit_code 0.
                        try:
                            drain_all()
                        except Exception as exc:  # noqa: BLE001
                            return (
                                out.decode("utf-8", errors="replace"),
                                err.decode("utf-8", errors="replace"),
                                -1,
                                f"命令已结束但输出读取失败：{exc}",
                            )
                        return (
                            out.decode("utf-8", errors="replace"),
                            err.decode("utf-8", errors="replace"),
                            channel.recv_exit_status(),
                            None,
                        )
                    try:
                        channel.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return (
                        out.decode("utf-8", errors="replace"),
                        err.decode("utf-8", errors="replace"),
                        -1,
                        f"命令执行超时（>{timeout} 秒）",
                    )
            except Exception as exc:  # noqa: BLE001
                return (
                    out.decode("utf-8", errors="replace"),
                    err.decode("utf-8", errors="replace"),
                    -1,
                    f"读取指令输出失败：{exc}",
                )

            if channel.exit_status_ready():
                try:
                    drain_all()
                except Exception as exc:  # noqa: BLE001
                    return (
                        out.decode("utf-8", errors="replace"),
                        err.decode("utf-8", errors="replace"),
                        -1,
                        f"命令已结束但输出读取失败：{exc}",
                    )
                return (
                    out.decode("utf-8", errors="replace"),
                    err.decode("utf-8", errors="replace"),
                    channel.recv_exit_status(),
                    None,
                )

            if time.monotonic() >= deadline:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass
                return (
                    out.decode("utf-8", errors="replace"),
                    err.decode("utf-8", errors="replace"),
                    -1,
                    f"命令执行超时（>{timeout} 秒）",
                )

            time.sleep(0.02)

    def _stream_via_shell(self, command: str) -> Generator[Dict[str, Any], None, None]:
        """Stream a command through the persistent PTY shell.

        A PTY merges stderr into stdout, so every data chunk is reported as
        ``stdout``.  Output is delivered as it arrives (only a possible marker
        prefix is held back), including prompts without a trailing newline; a
        bounded read batch keeps the yield points reachable while a command
        writes continuously.
        """
        channel: Optional[paramiko.Channel] = None
        close_when_done = False
        completed = False
        exit_sent = False
        try:
            channel, close_when_done = self._ensure_shell_channel()

            uid = uuid.uuid4().hex[:12]
            end_marker = f"---BRIDGE_STREAM_END_{uid}---"
            exit_marker = f"---BRIDGE_STREAM_EXIT_{uid}---"
            rc_var = f"__BRIDGE_RC_{uid}"
            tmp_var = f"__BRIDGE_TMP_{uid}"
            eof_marker = f"BRIDGE_EOF_{uid}"
            script = self._shell_script(
                command,
                end_marker,
                exit_marker,
                rc_var,
                tmp_var,
                eof_marker,
                f"bridge_{uid}.sh",
            )
            with self._send_lock:
                self._prepare_shell_channel(channel)
                self._send_all(channel, script.encode("utf-8"), timeout=30.0, lock=False)

            end_pattern, exit_pattern = self._marker_patterns(end_marker, exit_marker)
            end_token = b"\x01" + end_marker.encode()
            buffer = b""
            found_end = False
            exit_code = -1
            decoder = codecs.getincrementaldecoder("utf-8")("replace")

            while True:
                progressed = False
                # Read a bounded batch per iteration so a continuously-writing
                # command still reaches the yield points below.
                for _ in range(32):
                    if not channel.recv_ready():
                        break
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    progressed = True

                if not found_end:
                    match = end_pattern.search(buffer)
                    if match:
                        if match.start():
                            text = decoder.decode(buffer[:match.start()])
                            if text:
                                yield {"type": "stdout", "data": text}
                        buffer = buffer[match.end():]
                        found_end = True
                    else:
                        # Deliver everything that cannot be the beginning of a
                        # marker; prompts such as "Password:" are emitted now.
                        keep = self._marker_prefix_len(buffer, (end_token + b"\r\n",))
                        cut = len(buffer) - keep
                        if cut > 0:
                            text = decoder.decode(buffer[:cut])
                            buffer = buffer[cut:]
                            if text:
                                yield {"type": "stdout", "data": text}
                else:
                    match = exit_pattern.search(buffer)
                    if match:
                        exit_code = int(match.group(1))
                        completed = True
                        break
                    # Keep the whole pending exit line: it may already contain
                    # the exit marker plus a partial number (":1" of ":123").
                    # Everything here is at most the marker plus prompt noise.
                    if len(buffer) > 8192:
                        buffer = buffer[-8192:]

                if channel.closed and not progressed:
                    break
                if not progressed:
                    time.sleep(0.01)

            if not found_end and buffer:
                tail_text = decoder.decode(buffer, final=True)
            else:
                tail_text = decoder.decode(b"", final=True)
            if tail_text:
                yield {"type": "stdout", "data": tail_text}

            yield {"type": "exit", "exit_code": exit_code}
            exit_sent = True
            self._logger.info("流式指令执行完毕（shell 通道），退出码=%s", exit_code)

        except Exception as exc:  # noqa: BLE001
            self._logger.error("Shell stream failed: %s", exc, exc_info=True)
            yield {"type": "stderr", "data": f"Stream error: {exc}"}
            if not exit_sent:
                yield {"type": "exit", "exit_code": -1}
                exit_sent = True
        finally:
            if close_when_done and channel is not None:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass
            elif not completed and channel is not None:
                # Consumer stopped early (client disconnect): interrupt the
                # command so the shared shell is usable for the next request.
                try:
                    self._send_all(channel, b"\x03", timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass

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
        An exit chunk is emitted on every path, including "SSH not connected"
        and execution errors, so clients never mistake silence for success.
        """
        decision = self._guard_decision(command)
        if decision is not None:
            yield {"type": "stderr", "data": decision.message}
            yield {"type": "exit", "exit_code": BLOCKED_EXIT_CODE}
            return

        if not self.is_connected():
            self._logger.error("stream_execute() called while not connected")
            yield {"type": "stderr", "data": "SSH not connected"}
            yield {"type": "exit", "exit_code": -1}
            return

        channel: Optional[paramiko.Channel] = None
        # Serialize with execute()/other streams: the persistent shell can only
        # run one command at a time, and a second script would clobber the first.
        with self._command_lock:
            self._mark_busy()
            try:
                self._bump_activity()
                self._logger.info(
                    "Streaming command (length=%d, channel=%s)",
                    len(command),
                    "shell" if self._prefer_shell else "exec",
                )

                if self._prefer_shell:
                    yield from self._stream_via_shell(command)
                    return

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

                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                while True:
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break

                    if channel.recv_ready():
                        data = decoder.decode(channel.recv(4096))
                        if data:
                            yield {"type": "stdout", "data": data}

                    if channel.recv_stderr_ready():
                        data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                        if data:
                            yield {"type": "stderr", "data": data}

                    # Small sleep to avoid spinning the CPU while waiting for output.
                    time.sleep(0.01)

                remaining = decoder.decode(b"", final=True)
                if remaining:
                    yield {"type": "stdout", "data": remaining}

                exit_code = channel.recv_exit_status()
                yield {"type": "exit", "exit_code": exit_code}
                self._logger.info("流式指令执行完毕，退出码=%s", exit_code)

            except Exception as exc:  # noqa: BLE001
                self._logger.error("Stream execution failed: %s", exc, exc_info=True)
                yield {"type": "stderr", "data": f"Stream error: {exc}"}
                yield {"type": "exit", "exit_code": -1}
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

        Deliberately does **not** take ``_command_lock``: an interactive command
        holds that lock for its whole lifetime, and waiting for it would make
        /input useless exactly when it is needed.
        """
        if self._command_guard.check_input(data) is not None:
            self._logger.warning(
                "交互输入命中危险操作，已拒绝发送 | 长度=%d",
                len(data),
            )
            return False

        with self._lock:
            shell = self._shell
        if shell is None or shell.closed:
            self._logger.warning("send_input() called but no interactive shell is open")
            return False
        try:
            self._bump_activity()
            payload = data.encode("utf-8", errors="replace")
            self._send_all(shell, payload, timeout=5.0)
            self._logger.debug("Sent %d bytes to interactive shell", len(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            self._logger.error("Failed to send input to shell: %s", exc)
            return False

    def is_connected(self) -> bool:
        """Return True if the underlying transport is active."""
        client = self._client
        transport = self._transport
        return client is not None and transport is not None and transport.is_active()

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
