"""Local HTTP/WebSocket API server for the Qizhi (齐治) SSH Bridge.

This module exposes a Flask + Flask-SocketIO server that lets an AI assistant
send shell commands to the bridge and receive responses.  Flask-SocketIO was
chosen over FastAPI/uvicorn because:

* It has a pure-threading fallback async mode, which avoids packaging gevent or
  eventlet binaries inside a PyInstaller one-file EXE.
* The ``flask`` dependency tree is small and well supported on Windows.
* A single-threaded Werkzeug development server is sufficient for a localhost
  AI bridge that handles one SSH session at a time.

All routes are intentionally defensive: malformed JSON, missing fields, or SSH
errors are caught, logged, and returned with helpful diagnostics.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, request
from flask_socketio import SocketIO, emit

from command_guard import (
    BLOCKED_EXIT_CODE,
    CommandGuard,
    GuardDecision,
    build_blocked_input_result,
    build_blocked_result,
)
from connection import ConnectionParams
from logger import get_logger, log_blank_line, log_console
from ssh_client import SSHBridge
from version import __version__


logger = get_logger(__name__)


def _preview_text(value: Any, limit: int) -> tuple[str, bool]:
    """Return terminal-safe preview text and whether it was truncated."""
    raw = "" if value is None else str(value)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]

    # Prevent remote output from injecting terminal control sequences while
    # preserving normal newlines and tabs for readable command output.
    raw = raw.replace("\r\n", "\n").replace("\r", "\\r")
    escaped = []
    for char in raw:
        code = ord(char)
        if char in {"\n", "\t"} or (code >= 32 and code != 127):
            escaped.append(char)
        else:
            escaped.append(f"\\x{code:02x}")
    text = "".join(escaped).rstrip("\n")
    if not text:
        text = "（无输出）"
    if truncated:
        text += f"\n…（控制台预览已截断，原始内容共 {len(str(value))} 字符）"
    return text, truncated


class BridgeAPI:
    """Flask/SocketIO wrapper around an SSHBridge instance."""

    def __init__(
        self,
        bridge: SSHBridge,
        console_output_limit: int = 12000,
        command_guard: Optional[CommandGuard] = None,
    ) -> None:
        self.bridge: SSHBridge = bridge
        self.console_output_limit = max(256, int(console_output_limit))
        # 内部兜底：未传入时默认启用（见 command_guard.py）。
        self.command_guard: CommandGuard = command_guard if command_guard is not None else CommandGuard.default()
        self.app: Flask = Flask(__name__)
        self.app.config["JSON_AS_ASCII"] = False
        self.socketio: SocketIO = SocketIO(
            self.app,
            cors_allowed_origins="*",
            async_mode="threading",
            logger=False,
            engineio_logger=False,
        )
        self._interactive_shell: Optional[Any] = None
        self._interactive_lock: threading.Lock = threading.Lock()
        self._interactive_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self._interactive_reader: Optional[threading.Thread] = None
        self._register_routes()
        self._register_socketio_events()

    def _show_command(self, source: str, command: str, timeout: Optional[int] = None) -> None:
        """Show a received command in a console-only block."""
        preview, _ = _preview_text(command, self.console_output_limit)
        timeout_text = f" | 超时 {timeout} 秒" if timeout is not None else ""
        log_blank_line()
        log_console(
            ">>> 收到服务器指令 | 来源 %s | 长度 %d%s",
            source,
            len(command),
            timeout_text,
        )
        log_console("┌─ 指令正文")
        log_console("%s", preview)
        log_console("└─ 指令结束")

    def _show_result(self, source: str, result: Dict[str, Any], elapsed: float) -> None:
        """Show a non-streaming server result without persisting its body."""
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", -1)
        stdout_preview, stdout_truncated = _preview_text(stdout, self.console_output_limit)
        stderr_preview, stderr_truncated = _preview_text(stderr, self.console_output_limit)
        log_console(
            "<<< 服务器执行结果 | 来源 %s | 退出码 %s | 耗时 %.3f 秒",
            source,
            exit_code,
            elapsed,
        )
        log_console("┌─ stdout（%d 字符）", len(str(stdout)))
        log_console("%s", stdout_preview)
        log_console("├─ stderr（%d 字符）", len(str(stderr)))
        log_console("%s", stderr_preview)
        error = result.get("error")
        if error:
            log_console("! 执行未完成：%s", error, level=30)
        if stdout_truncated or stderr_truncated:
            log_console(
                "! 输出较长，仅展示前 %d 字符；API 返回值保持完整",
                self.console_output_limit,
                level=30,
            )
        log_console("└─ 返回结束")
        log_blank_line()

    def _show_failure(self, source: str, exc: Exception, elapsed: float) -> None:
        preview, _ = _preview_text(exc, self.console_output_limit)
        log_console(
            "<<< 服务器执行异常 | 来源 %s | 耗时 %.3f 秒\n%s",
            source,
            elapsed,
            preview,
            level=40,
        )
        log_blank_line()

    def _show_blocked(self, source: str, decision: GuardDecision) -> None:
        """Show that a command was refused (fact and source only; the rule stays internal)."""
        logger.warning("危险操作已拒绝（未下发到服务器）| 来源 %s", source)
        head = decision.message.splitlines()[0] if decision.message else ""
        log_blank_line()
        log_console("⛔ 未下发到目标服务器 | 来源 %s", source, level=30)
        if head:
            log_console("    已返回调用方：%s", head, level=30)
        log_blank_line()

    def _guard_command(self, source: str, command: str) -> Optional[GuardDecision]:
        """Return a guard decision when ``command`` must not be executed."""
        decision = self.command_guard.check(command)
        if decision is not None:
            self._show_blocked(source, decision)
        return decision

    def _show_stream_chunk(self, stream_type: str, data: Any, remaining: int) -> tuple[int, bool]:
        """Show one streaming chunk within the total console preview budget."""
        raw = "" if data is None else str(data)
        if not raw:
            return 0, False
        if remaining <= 0:
            return 0, True
        visible = raw[:remaining]
        preview, _ = _preview_text(visible, remaining)
        log_console("<<< 流式 %s\n%s", stream_type, preview)
        return len(visible), len(raw) > remaining

    def _show_stream_summary(
        self,
        source: str,
        exit_code: Any,
        elapsed: float,
        stdout_chars: int,
        stderr_chars: int,
        truncated: bool,
    ) -> None:
        log_console(
            "<<< 流式执行完成 | 来源 %s | 退出码 %s | 耗时 %.3f 秒 "
            "| stdout %d 字符 | stderr %d 字符",
            source,
            exit_code if exit_code is not None else "未知",
            elapsed,
            stdout_chars,
            stderr_chars,
        )
        if truncated:
            log_console(
                "! 流式输出超过 %d 字符，后续内容不再刷屏；API/SSE 数据保持完整",
                self.console_output_limit,
                level=30,
            )
        log_blank_line()

    @staticmethod
    def _show_interactive_input(source: str, data: str) -> None:
        """Show interactive input metadata, never the potentially secret body."""
        log_blank_line()
        log_console(
            ">>> 收到交互输入 | 来源 %s | 长度 %d | 内容已安全隐藏",
            source,
            len(data),
        )

    @staticmethod
    def _show_interactive_status(ok: bool, message: str) -> None:
        level = 20 if ok else 30
        prefix = "✓" if ok else "!"
        log_console("%s 交互输入处理结果 | %s", prefix, message, level=level)
        log_blank_line()

    # ------------------------------------------------------------------
    # HTTP routes
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        @self.app.route("/health", methods=["GET"])
        def health() -> Response:
            connected = self.bridge.is_connected()
            logger.debug("/health requested, connected=%s", connected)
            return jsonify({"status": "ok", "connected": connected, "version": __version__})

        @self.app.route("/execute", methods=["POST"])
        def execute() -> Response:
            payload, error = self._read_json_body(request)
            if error:
                logger.warning("/execute bad payload: %s", error)
                return jsonify({"error": error}), 400

            command = payload.get("command")
            if not command or not isinstance(command, str):
                return jsonify({"error": "missing or invalid 'command' field"}), 400

            timeout = self._positive_int(payload.get("timeout"), 30)
            logger.info("/execute command_len=%d timeout=%d", len(command), timeout)
            self._show_command("HTTP /execute", command, timeout)
            started = time.perf_counter()

            decision = self._guard_command("HTTP /execute", command)
            if decision is not None:
                return jsonify(build_blocked_result(decision))

            try:
                result = self.bridge.execute(command, timeout=timeout)
                logger.info("/execute finished exit_code=%s", result.get("exit_code"))
                self._show_result("HTTP /execute", result, time.perf_counter() - started)
                return jsonify(result)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("/execute failed: %s", exc)
                self._show_failure("HTTP /execute", exc, time.perf_counter() - started)
                return jsonify(self._error_payload("execute failed", exc)), 500

        @self.app.route("/stream", methods=["GET"])
        def stream() -> Response:
            command = request.args.get("command", "").strip()
            if not command:
                return jsonify({"error": "missing 'command' query parameter"}), 400

            logger.info("/stream SSE command_len=%d", len(command))
            self._show_command("HTTP SSE /stream", command)

            decision = self._guard_command("HTTP SSE /stream", command)
            if decision is not None:
                return self._blocked_stream_response(command, decision)

            def event_stream():
                started = time.perf_counter()
                displayed_chars = 0
                stdout_chars = 0
                stderr_chars = 0
                exit_code: Any = None
                truncated = False
                try:
                    yield self._sse_event(
                        "start", {"command": command, "connected": self.bridge.is_connected()}
                    )
                    for chunk in self.bridge.stream_execute(command):
                        event_type = chunk.get("type", "stdout")
                        if event_type == "exit":
                            exit_code = chunk.get("exit_code", -1)
                            payload = {"exit_code": exit_code}
                        else:
                            data = chunk.get("data", "")
                            payload = {"data": data}
                            if event_type == "stderr":
                                stderr_chars += len(str(data))
                            else:
                                stdout_chars += len(str(data))
                            used, clipped = self._show_stream_chunk(
                                event_type,
                                data,
                                self.console_output_limit - displayed_chars,
                            )
                            displayed_chars += used
                            truncated = truncated or clipped
                        yield self._sse_event(event_type, payload)
                except Exception as exc:
                    logger.exception("/stream SSE failed: %s", exc)
                    self._show_failure("HTTP SSE /stream", exc, time.perf_counter() - started)
                    yield self._sse_event("error", self._error_payload("stream failed", exc))
                finally:
                    yield self._sse_event("end", {})
                    self._show_stream_summary(
                        "HTTP SSE /stream",
                        exit_code,
                        time.perf_counter() - started,
                        stdout_chars,
                        stderr_chars,
                        truncated,
                    )

            return Response(
                event_stream(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @self.app.route("/input", methods=["POST"])
        def input_route() -> Response:
            payload, error = self._read_json_body(request)
            if error:
                logger.warning("/input bad payload: %s", error)
                return jsonify({"error": error}), 400

            data = payload.get("data")
            if data is None or not isinstance(data, str):
                return jsonify({"error": "missing or invalid 'data' field"}), 400

            logger.info("/input data_len=%d", len(data))
            self._show_interactive_input("HTTP /input", data)

            decision = self.command_guard.check_input(data)
            if decision is not None:
                self._show_blocked("HTTP /input", decision)
                return jsonify(build_blocked_input_result(decision))

            ok, message = self._send_interactive_input(data)
            self._show_interactive_status(ok, message)
            if not ok:
                return jsonify({"error": message}), 503
            return jsonify({"status": "ok", "message": message})

    # ------------------------------------------------------------------
    # Socket.IO events
    # ------------------------------------------------------------------
    def _register_socketio_events(self) -> None:
        @self.socketio.on("connect")
        def on_connect() -> None:
            logger.info("WebSocket client connected sid=%s", getattr(request, "sid", "?"))
            emit(
                "status",
                {
                    "status": "connected",
                    "ssh_connected": self.bridge.is_connected(),
                },
            )

        @self.socketio.on("disconnect")
        def on_disconnect() -> None:
            logger.info("WebSocket client disconnected sid=%s", getattr(request, "sid", "?"))

        @self.socketio.on("execute")
        def on_execute(payload: Dict[str, Any]) -> None:
            command = (payload or {}).get("command")
            if not command or not isinstance(command, str):
                emit("error", {"error": "missing or invalid 'command'"})
                return
            timeout = self._positive_int((payload or {}).get("timeout"), 30)
            logger.info("Socket.IO execute command_len=%d timeout=%d", len(command), timeout)
            self._show_command("Socket.IO execute", command, timeout)
            started = time.perf_counter()

            decision = self._guard_command("Socket.IO execute", command)
            if decision is not None:
                emit("result", build_blocked_result(decision))
                return

            try:
                result = self.bridge.execute(command, timeout=timeout)
                self._show_result("Socket.IO execute", result, time.perf_counter() - started)
                emit("result", result)
            except Exception as exc:
                logger.exception("/ws execute failed: %s", exc)
                self._show_failure("Socket.IO execute", exc, time.perf_counter() - started)
                emit("error", self._error_payload("execute failed", exc))

        @self.socketio.on("stream")
        def on_stream(payload: Dict[str, Any]) -> None:
            command = (payload or {}).get("command")
            if not command or not isinstance(command, str):
                emit("error", {"error": "missing or invalid 'command'"})
                return
            logger.info("Socket.IO stream command_len=%d", len(command))
            self._show_command("Socket.IO stream", command)

            decision = self._guard_command("Socket.IO stream", command)
            if decision is not None:
                emit("start", {"command": command, "connected": self.bridge.is_connected()})
                emit("output", {"type": "stderr", "data": decision.message})
                emit("output", {"type": "exit", "exit_code": BLOCKED_EXIT_CODE})
                emit("end", {})
                return

            started = time.perf_counter()
            displayed_chars = 0
            stdout_chars = 0
            stderr_chars = 0
            exit_code: Any = None
            truncated = False
            try:
                emit(
                    "start",
                    {"command": command, "connected": self.bridge.is_connected()},
                )
                for chunk in self.bridge.stream_execute(command):
                    event_type = chunk.get("type", "stdout")
                    if event_type == "exit":
                        exit_code = chunk.get("exit_code", -1)
                    else:
                        data = chunk.get("data", "")
                        if event_type == "stderr":
                            stderr_chars += len(str(data))
                        else:
                            stdout_chars += len(str(data))
                        used, clipped = self._show_stream_chunk(
                            event_type,
                            data,
                            self.console_output_limit - displayed_chars,
                        )
                        displayed_chars += used
                        truncated = truncated or clipped
                    emit("output", chunk)
                emit("end", {})
            except Exception as exc:
                logger.exception("/ws stream failed: %s", exc)
                self._show_failure("Socket.IO stream", exc, time.perf_counter() - started)
                emit("error", self._error_payload("stream failed", exc))
            finally:
                self._show_stream_summary(
                    "Socket.IO stream",
                    exit_code,
                    time.perf_counter() - started,
                    stdout_chars,
                    stderr_chars,
                    truncated,
                )

        @self.socketio.on("input")
        def on_input(payload: Dict[str, Any]) -> None:
            data = (payload or {}).get("data")
            if data is None or not isinstance(data, str):
                emit("error", {"error": "missing or invalid 'data'"})
                return
            self._show_interactive_input("Socket.IO input", data)

            decision = self.command_guard.check_input(data)
            if decision is not None:
                self._show_blocked("Socket.IO input", decision)
                payload_data = build_blocked_input_result(decision)
                emit("input_status", {"ok": False, **payload_data})
                return

            ok, message = self._send_interactive_input(data)
            self._show_interactive_status(ok, message)
            emit("input_status", {"ok": ok, "message": message})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _read_json_body(request) -> tuple[Dict[str, Any], Optional[str]]:
        """Return (payload, error_message)."""
        if not request.is_json:
            if request.data:
                try:
                    return json.loads(request.data.decode("utf-8", errors="replace")), None
                except json.JSONDecodeError as exc:
                    return {}, f"invalid JSON body: {exc}"
            return {}, "request body is not JSON"
        try:
            return request.get_json(force=True, silent=False) or {}, None
        except Exception as exc:
            return {}, f"cannot parse JSON body: {exc}"

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            ivalue = int(value)
            return ivalue if ivalue > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _error_payload(message: str, exc: Exception) -> Dict[str, Any]:
        return {
            "error": message,
            "detail": str(exc),
            "exception_type": type(exc).__name__,
        }

    @staticmethod
    def _sse_event(event: str, data: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _blocked_stream_response(self, command: str, decision: GuardDecision) -> Response:
        """Build the SSE refusal; identical to a normal run except the prompt and exit code."""
        def blocked_stream():
            yield self._sse_event(
                "start", {"command": command, "connected": self.bridge.is_connected()}
            )
            yield self._sse_event("stderr", {"data": decision.message})
            yield self._sse_event("exit", {"exit_code": BLOCKED_EXIT_CODE})
            yield self._sse_event("end", {})

        return Response(
            blocked_stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _send_interactive_input(self, data: str) -> tuple[bool, str]:
        """Send data to an interactive shell channel if one is available.

        The SSHBridge contract does not expose an interactive channel, so this
        method first tries a bridge-level ``send_input`` helper if present, then
        falls back to maintaining its own Paramiko invoke_shell session on the
        bridge's underlying transport.  If neither is available it returns a
        graceful error.
        """
        # 1) Prefer the bridge helper if available.
        send_input = getattr(self.bridge, "send_input", None)
        if callable(send_input):
            try:
                sent = send_input(data)
                if sent is not False:
                    return True, "sent via bridge"
                logger.warning("bridge.send_input reported that no interactive shell was available")
            except Exception as exc:
                logger.warning("bridge.send_input failed: %s", exc)

        # 2) Maintain our own shell on the bridge transport.
        try:
            with self._interactive_lock:
                if self._interactive_shell is None or self._interactive_shell.closed:
                    transport = self._bridge_transport()
                    if transport is None:
                        return False, "no SSH transport available"
                    self._interactive_shell = transport.open_session()
                    self._interactive_shell.get_pty(term="xterm", width=160, height=48)
                    self._interactive_shell.invoke_shell()
                    self._interactive_queue = queue.Queue()
                    self._interactive_reader = threading.Thread(
                        target=self._read_interactive_output,
                        daemon=True,
                    )
                    self._interactive_reader.start()
                    # Give the shell a moment to settle before sending input.
                    time.sleep(0.3)

                self._interactive_shell.sendall(data.encode("utf-8", errors="replace"))
                return True, "sent to interactive shell"
        except Exception as exc:
            logger.exception("interactive input failed: %s", exc)
            return False, f"interactive input unavailable: {exc}"

    def _bridge_transport(self) -> Optional[Any]:
        """Best-effort access to the Paramiko transport through the bridge."""
        client = getattr(self.bridge, "_client", None)
        if client is not None:
            return client.get_transport()
        transport = getattr(self.bridge, "_transport", None)
        if transport is not None:
            return transport
        return None

    def _read_interactive_output(self) -> None:
        """Background reader for the interactive shell channel."""
        shell = self._interactive_shell
        if shell is None:
            return
        try:
            while not shell.closed:
                if shell.recv_ready():
                    data = shell.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        preview, _ = _preview_text(data, self.console_output_limit)
                        log_console("<<< 交互式 stdout\n%s", preview)
                        self._interactive_queue.put({"type": "stdout", "data": data})
                if shell.recv_stderr_ready():
                    data = shell.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        preview, _ = _preview_text(data, self.console_output_limit)
                        log_console("<<< 交互式 stderr\n%s", preview, level=30)
                        self._interactive_queue.put({"type": "stderr", "data": data})
                time.sleep(0.05)
        except Exception as exc:
            logger.debug("interactive shell reader exited: %s", exc)

    def run(self, host: str = "127.0.0.1", port: int = 8766) -> None:
        """Start the API server and block until shutdown."""
        logger.info("Starting Qizhi SSH Bridge API on %s:%d", host, port)
        try:
            self.socketio.run(
                self.app,
                host=host,
                port=port,
                debug=False,
                use_reloader=False,
                allow_unsafe_werkzeug=True,
            )
        except Exception as exc:
            logger.exception("API server crashed: %s", exc)
            raise
        finally:
            self._close_interactive_shell()

    def _close_interactive_shell(self) -> None:
        with self._interactive_lock:
            shell = self._interactive_shell
            if shell is not None:
                try:
                    shell.close()
                except Exception as exc:
                    logger.debug("error closing interactive shell: %s", exc)
                self._interactive_shell = None


def start_api(
    bridge: SSHBridge,
    host: str = "127.0.0.1",
    port: int = 8766,
    console_output_limit: int = 12000,
    command_guard: Optional[CommandGuard] = None,
) -> None:
    """Public entry point used by ``main.py``.

    Args:
        bridge: An already-instantiated (and preferably connected) SSHBridge.
        host: Local interface to bind to. Defaults to loopback for safety.
        port: TCP port for the API server.
        console_output_limit: Maximum command/output characters shown per block.
        command_guard: Dangerous-command guard. Defaults to the built-in rules.
    """
    api = BridgeAPI(
        bridge,
        console_output_limit=console_output_limit,
        command_guard=command_guard,
    )
    api.run(host=host, port=port)
