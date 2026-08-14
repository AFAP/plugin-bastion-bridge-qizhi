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

from connection import ConnectionParams
from logger import get_logger, log_blank_line
from ssh_client import SSHBridge


logger = get_logger(__name__)


class BridgeAPI:
    """Flask/SocketIO wrapper around an SSHBridge instance."""

    def __init__(self, bridge: SSHBridge) -> None:
        self.bridge: SSHBridge = bridge
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

    # ------------------------------------------------------------------
    # HTTP routes
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        @self.app.route("/health", methods=["GET"])
        def health() -> Response:
            connected = self.bridge.is_connected()
            logger.debug("/health requested, connected=%s", connected)
            return jsonify({"status": "ok", "connected": connected})

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
            logger.info("收到请求 /execute，指令: %r（超时 %d 秒）", command, timeout)

            try:
                result = self.bridge.execute(command, timeout=timeout)
                logger.info("请求 /execute 处理完毕，退出码: %s", result.get("exit_code"))
                log_blank_line()
                return jsonify(result)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("/execute 执行失败: %s", exc)
                log_blank_line()
                return jsonify(self._error_payload("execute failed", exc)), 500

        @self.app.route("/stream", methods=["GET"])
        def stream() -> Response:
            command = request.args.get("command", "").strip()
            if not command:
                return jsonify({"error": "missing 'command' query parameter"}), 400

            logger.info("收到请求 /stream（SSE），指令: %r", command)

            def event_stream():
                try:
                    yield self._sse_event(
                        "start", {"command": command, "connected": self.bridge.is_connected()}
                    )
                    for chunk in self.bridge.stream_execute(command):
                        event_type = chunk.get("type", "stdout")
                        data = chunk.get("data", "")
                        yield self._sse_event(event_type, {"data": data})
                except Exception as exc:
                    logger.exception("/stream SSE 执行失败: %s", exc)
                    yield self._sse_event("error", self._error_payload("stream failed", exc))
                finally:
                    yield self._sse_event("end", {})
                    log_blank_line()

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

            logger.info("收到请求 /input，数据长度: %d", len(data))
            ok, message = self._send_interactive_input(data)
            log_blank_line()
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
            logger.info("收到 WebSocket 指令: %r（超时 %d 秒）", command, timeout)
            try:
                result = self.bridge.execute(command, timeout=timeout)
                emit("result", result)
            except Exception as exc:
                logger.exception("WebSocket 指令执行失败: %s", exc)
                emit("error", self._error_payload("execute failed", exc))
            finally:
                log_blank_line()

        @self.socketio.on("stream")
        def on_stream(payload: Dict[str, Any]) -> None:
            command = (payload or {}).get("command")
            if not command or not isinstance(command, str):
                emit("error", {"error": "missing or invalid 'command'"})
                return
            logger.info("收到 WebSocket 流式指令: %r", command)
            try:
                emit(
                    "start",
                    {"command": command, "connected": self.bridge.is_connected()},
                )
                for chunk in self.bridge.stream_execute(command):
                    emit("output", chunk)
                emit("end", {})
            except Exception as exc:
                logger.exception("WebSocket 流式指令执行失败: %s", exc)
                emit("error", self._error_payload("stream failed", exc))
            finally:
                log_blank_line()

        @self.socketio.on("input")
        def on_input(payload: Dict[str, Any]) -> None:
            data = (payload or {}).get("data")
            if data is None or not isinstance(data, str):
                emit("error", {"error": "missing or invalid 'data'"})
                return
            ok, message = self._send_interactive_input(data)
            emit("input_status", {"ok": ok, "message": message})
            log_blank_line()

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
                send_input(data)
                return True, "sent via bridge"
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
                        logger.info("<<< 服务器返回（交互 stdout）: %s", data.rstrip("\r\n"))
                        self._interactive_queue.put({"type": "stdout", "data": data})
                if shell.recv_stderr_ready():
                    data = shell.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        logger.info("<<< 服务器返回（交互 stderr）: %s", data.rstrip("\r\n"))
                        self._interactive_queue.put({"type": "stderr", "data": data})
                time.sleep(0.05)
        except Exception as exc:
            logger.debug("interactive shell reader exited: %s", exc)

    def run(self, host: str = "127.0.0.1", port: int = 8765) -> None:
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


def start_api(bridge: SSHBridge, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Public entry point used by ``main.py``.

    Args:
        bridge: An already-instantiated (and preferably connected) SSHBridge.
        host: Local interface to bind to. Defaults to loopback for safety.
        port: TCP port for the API server.
    """
    api = BridgeAPI(bridge)
    api.run(host=host, port=port)
