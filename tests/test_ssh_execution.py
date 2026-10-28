"""Execution-channel tests: shell default, shell exit status, exec read loop."""

import re
import socket
import threading
import time
import unittest
from unittest.mock import patch

import ssh_client
from connection import ConnectionParams


def _bridge(prefer_shell=True):
    return ssh_client.SSHBridge(
        ConnectionParams(host="192.0.2.1", port=22, username="tester"),
        keepalive_enabled=False,
        prefer_shell=prefer_shell,
    )


class _FakeTransport:
    def __init__(self, active=True, session=None):
        self._active = active
        self._session = session
        self.open_calls = 0

    def is_active(self):
        return self._active

    def open_session(self):
        self.open_calls += 1
        if self._session is None:
            raise RuntimeError("no session available")
        return self._session


def _markers(script):
    """Return the end/exit markers contained in a generated shell script."""
    match = re.search(r"---BRIDGE_(STREAM_)?END_([0-9a-f]+)---", script)
    if not match:
        raise AssertionError(f"no end marker in script: {script!r}")
    prefix = "BRIDGE_STREAM_" if match.group(1) else "BRIDGE_"
    uid = match.group(2)
    return f"---{prefix}END_{uid}---", f"---{prefix}EXIT_{uid}---"


class _ScriptedShell:
    """A fake PTY shell that answers a command script with canned output."""

    def __init__(
        self,
        output=b"hello\n",
        exit_code=0,
        chunk_size=17,
        answer=True,
        echo_script=False,
        one_per_poll=False,
        endless=False,
    ):
        self.sent = []
        self.closed = False
        self._reply = b""
        self._pos = 0
        self._output = output
        self._exit_code = exit_code
        self._chunk_size = chunk_size
        self._answer = answer
        self._echo_script = echo_script
        self._one_per_poll = one_per_poll
        self._endless = endless
        self._endless_active = False
        self._ready_calls = 0
        self._timeout = None

    def send(self, data):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        self.sent.append(text)
        if "BRIDGE_" in text and "END_" in text and self._answer:
            if self._endless:
                self._endless_active = True
                return
            end_marker, exit_marker = _markers(text)
            echo = text.replace("\n", "\r\n").encode() if self._echo_script else b""
            self._pos = 0
            self._ready_calls = 0
            # \x01 is produced by printf at runtime; the echoed script only has
            # the literal \001 characters.
            self._reply = (
                echo
                + self._output
                + b"\x01"
                + end_marker.encode()
                + b"\r\n"
                + b"\x01"
                + exit_marker.encode()
                + f":{self._exit_code}\r\n".encode()
            )

    def sendall(self, data):
        self.send(data)

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value

    def recv_ready(self):
        if self._endless_active:
            return True
        if self._pos >= len(self._reply):
            return False
        self._ready_calls += 1
        if self._one_per_poll and self._ready_calls % 2 == 0:
            return False
        return True

    def recv(self, size):
        if self._endless_active:
            time.sleep(0.001)  # keep the endless case from eating memory
            return b"x" * min(size, 1024)
        end = min(len(self._reply), self._pos + min(size, self._chunk_size))
        chunk = self._reply[self._pos:end]
        self._pos = end
        return chunk

    def get_pty(self, **kwargs):
        pass

    def invoke_shell(self):
        pass

    def close(self):
        self.closed = True


class _NoExecClient:
    """Client that fails the test if the exec channel is used at all."""

    def __init__(self, transport):
        self._transport = transport

    def exec_command(self, command, timeout=None):
        raise AssertionError("exec_command must not be used when shell is preferred")

    def get_transport(self):
        return self._transport


class ShellChannelTests(unittest.TestCase):
    def _connected_bridge(self, shell, prefer_shell=True):
        bridge = _bridge(prefer_shell=prefer_shell)
        transport = _FakeTransport(session=shell)
        bridge._transport = transport
        bridge._client = _NoExecClient(transport)
        bridge._shell = shell
        return bridge

    def test_shell_is_the_default_channel(self):
        self.assertTrue(_bridge()._prefer_shell)

    def test_execute_uses_shell_and_returns_real_exit_code(self):
        shell = _ScriptedShell(output=b"before\n", exit_code=3)
        bridge = self._connected_bridge(shell)

        result = bridge.execute("false")

        self.assertEqual(3, result["exit_code"])
        self.assertEqual("before", result["stdout"])
        self.assertNotIn("error", result)

    def test_shell_script_saves_status_before_printing_marker(self):
        shell = _ScriptedShell()
        bridge = self._connected_bridge(shell)

        bridge.execute("false")

        script = next(sent for sent in shell.sent if "BRIDGE_END_" in sent)
        rc_index = script.index("=$?")
        marker_index = script.index("printf '\\001---BRIDGE_END_")
        self.assertLess(rc_index, marker_index, "$? 必须在 printf 标记之前保存")
        self.assertIn("printf '\\001---BRIDGE_EXIT_", script)
        # The command is written to a temp file and sourced from one epilogue
        # line, so `read` cannot consume the wrapper as its answer.
        self.assertIn("(umask 077; cat > \"$", script)
        self.assertIn('. "$__BRIDGE_TMP_', script)

    def test_shell_timeout_returns_error_instead_of_blocking(self):
        shell = _ScriptedShell(answer=False)  # never answers: command never ends
        bridge = self._connected_bridge(shell)

        started = time.monotonic()
        result = bridge.execute("sleep 300", timeout=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5, "execute 必须在 timeout 附近返回")
        self.assertEqual(-1, result["exit_code"])
        self.assertIn("超时", result["error"])

    def test_marker_split_across_packets(self):
        # chunk_size=3 forces end/exit markers to span several recv() calls.
        shell = _ScriptedShell(output="中文输出\n".encode("utf-8"), exit_code=0, chunk_size=3)
        bridge = self._connected_bridge(shell)

        result = bridge.execute("echo 中文输出")

        self.assertEqual(0, result["exit_code"])
        self.assertIn("中文输出", result["stdout"])

    def test_echoed_script_is_not_mistaken_for_the_markers(self):
        # stty -echo may be unavailable; the terminal then echoes the whole
        # script, including the marker lines and the literal $__BRIDGE_RC_*.
        shell = _ScriptedShell(output=b"real-output\n", exit_code=4, echo_script=True)
        bridge = self._connected_bridge(shell)

        result = bridge.execute("false")

        self.assertEqual(4, result["exit_code"])
        self.assertIn("real-output", result["stdout"])

    def test_output_without_trailing_newline_still_completes(self):
        shell = _ScriptedShell(output=b"hello", exit_code=0)
        bridge = self._connected_bridge(shell)

        result = bridge.execute("printf hello")

        self.assertEqual(0, result["exit_code"])
        self.assertIn("hello", result["stdout"])
        self.assertNotIn("error", result)

    def test_exit_code_split_across_packets_is_not_parsed_early(self):
        # one_per_poll forces a pause between recv() calls; chunk_size=2 means
        # ":123\r\n" arrives as ":1", "23", "\r\n" over several polls.
        shell = _ScriptedShell(output=b"x", exit_code=123, chunk_size=2, one_per_poll=True)
        bridge = self._connected_bridge(shell)

        result = bridge.execute("false")

        self.assertEqual(123, result["exit_code"])
        self.assertNotIn("error", result)

    def test_endless_output_still_hits_the_deadline(self):
        shell = _ScriptedShell(endless=True)
        bridge = self._connected_bridge(shell)

        started = time.monotonic()
        result = bridge.execute("yes", timeout=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5)
        self.assertEqual(-1, result["exit_code"])
        self.assertIn("超时", result["error"])

    def test_send_input_reaches_a_running_command(self):
        shell = _ScriptedShell(answer=False)  # command never finishes
        bridge = self._connected_bridge(shell)
        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.setdefault("result", bridge.execute("read answer", timeout=1)),
            daemon=True,
        )
        worker.start()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any("BRIDGE_END" in sent for sent in shell.sent):
                break
            time.sleep(0.01)

        self.assertTrue(any("BRIDGE_END" in sent for sent in shell.sent), "命令脚本未下发")
        self.assertTrue(bridge.send_input("yes\n"), "命令执行期间 input 应可发送")
        self.assertIn("yes\n", shell.sent)
        self.assertTrue(worker.is_alive(), "execute 不应因 input 而提前结束")
        worker.join(3)

    def test_command_lock_serializes_execute_and_stream(self):
        shell = _ScriptedShell()
        bridge = self._connected_bridge(shell)

        bridge._command_lock.acquire()
        try:
            worker = threading.Thread(target=lambda: bridge.execute("id", timeout=1), daemon=True)
            worker.start()
            time.sleep(0.3)
            self.assertFalse(
                any("BRIDGE_END" in sent for sent in shell.sent),
                "execute 在锁被占用时不应下发脚本",
            )
        finally:
            bridge._command_lock.release()
        worker.join(3)
        self.assertTrue(any("BRIDGE_END" in sent for sent in shell.sent))

        shell.sent.clear()
        bridge._command_lock.acquire()
        try:
            collected = []
            stream_worker = threading.Thread(
                target=lambda: collected.extend(bridge.stream_execute("id")), daemon=True
            )
            stream_worker.start()
            time.sleep(0.3)
            self.assertFalse(
                any("BRIDGE_STREAM_END" in sent for sent in shell.sent),
                "stream_execute 在锁被占用时不应下发脚本",
            )
        finally:
            bridge._command_lock.release()
        stream_worker.join(3)
        self.assertTrue(any("BRIDGE_STREAM_END" in sent for sent in shell.sent))
        self.assertEqual("exit", collected[-1]["type"])


class _ReadChannel:
    def __init__(self, data=b"", ready_after_read=True):
        self._data = data
        self.ready_after_read = ready_after_read
        self.exit_status_calls = 0
        self.closed = False

    def recv_ready(self):
        return bool(self._data)

    def recv_stderr_ready(self):
        return False

    def recv(self, size):
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def recv_stderr(self, size):
        return b""

    def exit_status_ready(self):
        return self.ready_after_read and not self._data

    def recv_exit_status(self):
        self.exit_status_calls += 1
        return 0

    def close(self):
        self.closed = True


class ExecReadTests(unittest.TestCase):
    def test_output_is_read_before_waiting_for_exit_status(self):
        channel = _ReadChannel(data=b"x" * 200)
        out, err, code, error = ssh_client.SSHBridge._read_exec_output(channel, timeout=2)

        self.assertEqual("x" * 200, out)
        self.assertEqual(0, code)
        self.assertIsNone(error)

    def test_timeout_returns_error_without_blocking(self):
        channel = _ReadChannel(data=b"", ready_after_read=False)
        started = time.monotonic()
        out, err, code, error = ssh_client.SSHBridge._read_exec_output(channel, timeout=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5)
        self.assertEqual(-1, code)
        self.assertIn("超时", error)

    def test_endless_exec_output_still_hits_the_deadline(self):
        class _EndlessChannel:
            def recv_ready(self):
                return True

            def recv(self, size):
                time.sleep(0.001)
                return b"x" * min(size, 1024)

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, size):
                return b""

            def exit_status_ready(self):
                return False

            def recv_exit_status(self):
                return 0

            def close(self):
                pass

        started = time.monotonic()
        out, err, code, error = ssh_client.SSHBridge._read_exec_output(
            _EndlessChannel(), timeout=1
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5)
        self.assertEqual(-1, code)
        self.assertIn("超时", error)

    def test_exit_ready_at_deadline_drains_all_output(self):
        class _BulkChannel:
            def __init__(self, total):
                self.data = b"x" * total

            def recv_ready(self):
                return bool(self.data)

            def recv(self, size):
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, size):
                return b""

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return 0

            def close(self):
                pass

        channel = _BulkChannel(256 * 1024)
        # First monotonic() sets the deadline; the second one fires immediately
        # after the first 64 KiB chunk, so the ordinary drain hits the deadline.
        with patch("ssh_client.time.monotonic", side_effect=[0.0, 100.0]):
            out, err, code, error = ssh_client.SSHBridge._read_exec_output(
                channel, timeout=1
            )

        self.assertEqual(256 * 1024, len(out), "退出状态就绪后必须把剩余输出读全")
        self.assertEqual(0, code)
        self.assertIsNone(error)

    def test_empty_output_is_not_a_login_banner(self):
        bridge = _bridge(prefer_shell=False)
        self.assertFalse(bridge._looks_like_login_only(""))
        self.assertTrue(bridge._looks_like_login_only("Last login: today"))

    def test_empty_output_is_not_retried_through_shell(self):
        channel = _ReadChannel(data=b"")
        bridge = _bridge(prefer_shell=False)

        class _Client:
            def __init__(self):
                self.calls = 0

            def exec_command(self, command, timeout=None):
                self.calls += 1
                stdin = type("In", (), {"channel": channel})()
                stdout = type("Out", (), {"channel": channel})()
                return stdin, stdout, type("Err", (), {"read": lambda self: b""})()

        client = _Client()
        bridge._client = client
        bridge._transport = _FakeTransport()
        fallback_calls = []
        bridge._execute_via_shell = lambda command, timeout=30: fallback_calls.append(command) or {
            "stdout": "wrong",
            "stderr": "",
            "exit_code": 0,
        }

        result = bridge.execute("systemctl restart nginx")

        self.assertEqual(1, client.calls)
        self.assertEqual([], fallback_calls, "空输出不应触发 shell 重跑")
        self.assertEqual(0, result["exit_code"])

    def test_exec_banner_returns_error_without_rerunning_command(self):
        channel = _ReadChannel(data=b"Last login: today from 10.0.0.1")
        bridge = _bridge(prefer_shell=False)

        class _Client:
            def exec_command(self, command, timeout=None):
                stdin = type("In", (), {"channel": channel})()
                stdout = type("Out", (), {"channel": channel})()
                return stdin, stdout, type("Err", (), {"read": lambda self: b""})()

        bridge._client = _Client()
        bridge._transport = _FakeTransport()
        shell_calls = []
        bridge._execute_via_shell = lambda command, timeout=30: shell_calls.append(command) or {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
        }

        result = bridge.execute("id")

        self.assertEqual([], shell_calls, "横幅场景不应自动重跑命令")
        self.assertEqual(-1, result["exit_code"])
        self.assertIn("ssh_prefer_shell", result["error"])


class StreamExitTests(unittest.TestCase):
    def test_not_connected_still_yields_exit(self):
        chunks = list(_bridge().stream_execute("id"))
        self.assertEqual({"type": "exit", "exit_code": -1}, chunks[-1])

    def test_open_session_failure_still_yields_exit(self):
        bridge = _bridge(prefer_shell=False)
        transport = _FakeTransport(session=None)  # open_session raises
        bridge._transport = transport
        bridge._client = type("C", (), {"get_transport": lambda self: transport})()

        chunks = list(bridge.stream_execute("id"))

        self.assertEqual({"type": "exit", "exit_code": -1}, chunks[-1])
        self.assertTrue(any(chunk["type"] == "stderr" for chunk in chunks))

    def test_shell_stream_yields_output_and_exit(self):
        shell = _ScriptedShell(output=b"line1\nline2\n", exit_code=3, chunk_size=2)
        bridge = _bridge()
        transport = _FakeTransport(session=shell)
        bridge._transport = transport
        bridge._client = _NoExecClient(transport)
        bridge._shell = shell

        chunks = list(bridge.stream_execute("printf ..."))

        text = "".join(chunk["data"] for chunk in chunks if chunk["type"] == "stdout")
        self.assertIn("line1", text)
        self.assertIn("line2", text)
        self.assertNotIn("BRIDGE_STREAM_END", text)
        self.assertEqual({"type": "exit", "exit_code": 3}, chunks[-1])

    def _stream_with(self, shell):
        bridge = _bridge()
        transport = _FakeTransport(session=shell)
        bridge._transport = transport
        bridge._client = _NoExecClient(transport)
        bridge._shell = shell
        return bridge, list(bridge.stream_execute("printf ..."))

    def test_stream_output_without_trailing_newline_completes(self):
        chunks = self._stream_with(_ScriptedShell(output=b"hello", exit_code=0))[1]

        text = "".join(chunk["data"] for chunk in chunks if chunk["type"] == "stdout")
        self.assertIn("hello", text)
        self.assertEqual({"type": "exit", "exit_code": 0}, chunks[-1])

    def test_stream_exit_code_split_is_not_parsed_early(self):
        chunks = self._stream_with(
            _ScriptedShell(output=b"x", exit_code=123, chunk_size=2, one_per_poll=True)
        )[1]

        self.assertEqual({"type": "exit", "exit_code": 123}, chunks[-1])

    def test_stream_ignores_echoed_script_markers(self):
        chunks = self._stream_with(
            _ScriptedShell(
                output=b"real-output\n",
                exit_code=4,
                echo_script=True,
                chunk_size=3,
            )
        )[1]

        text = "".join(chunk["data"] for chunk in chunks if chunk["type"] == "stdout")
        self.assertIn("real-output", text)
        self.assertEqual({"type": "exit", "exit_code": 4}, chunks[-1])

    def test_stream_delivers_while_channel_keeps_writing(self):
        shell = _ScriptedShell(endless=True)
        bridge = _bridge()
        transport = _FakeTransport(session=shell)
        bridge._transport = transport
        bridge._client = _NoExecClient(transport)
        bridge._shell = shell

        generator = bridge.stream_execute("yes")
        first = []
        worker = threading.Thread(target=lambda: first.append(next(generator)), daemon=True)
        worker.start()
        worker.join(2)

        self.assertFalse(worker.is_alive(), "持续输出时流式读取必须交付数据")
        self.assertTrue(first and first[0]["type"] == "stdout")
        generator.close()

    def test_stream_emits_prompt_without_newline(self):
        shell = _ScriptedShell(output=b"Password:", exit_code=0, chunk_size=2, one_per_poll=True)
        bridge = _bridge()
        transport = _FakeTransport(session=shell)
        bridge._transport = transport
        bridge._client = _NoExecClient(transport)
        bridge._shell = shell

        generator = bridge.stream_execute("sudo id")
        first = []
        worker = threading.Thread(target=lambda: first.append(next(generator)), daemon=True)
        worker.start()
        worker.join(2)

        self.assertFalse(worker.is_alive(), "无换行提示必须立即交付")
        self.assertTrue(first and first[0]["data"])
        chunks = first + list(generator)
        self.assertEqual({"type": "exit", "exit_code": 0}, chunks[-1])


class ShellScriptTests(unittest.TestCase):
    """The wrapper must not leave rc/marker lines in the command's stdin."""

    def _script(self):
        return ssh_client.SSHBridge._shell_script(
            "read answer", "END", "EXIT", "RC", "TMP", "EOF_MARK", "bridge.sh"
        )

    def test_command_runs_from_a_temp_file(self):
        lines = self._script().splitlines()
        self.assertIn('(umask 077; cat > "$TMP") <<\'EOF_MARK\'', lines)

        index = lines.index("read answer")
        self.assertEqual("EOF_MARK", lines[index + 1])
        self.assertNotIn("RC=$?", lines[index + 1])

    def test_epilogue_is_one_line_with_rc_and_sentinel_markers(self):
        lines = self._script().splitlines()
        epilogue = next(line for line in lines if line.startswith('. "$TMP"'))

        self.assertIn("RC=$?", epilogue)
        self.assertIn("printf '\\001END\\n'", epilogue)
        self.assertIn("printf '\\001EXIT:%s\\n'", epilogue)
        # `unset` must ride on the same line; a separate line would be left in
        # the terminal queue and consumed by a running `read`.
        self.assertIn("unset RC", epilogue)
        self.assertFalse(
            any(line.strip() == "unset RC" for line in lines),
            "unset 不能单独成行",
        )


class _RecordingSendChannel:
    def __init__(self, fail=False):
        self.data = b""
        self.fail = fail
        self.closed = False
        self.timeout = None

    def sendall(self, data):
        if self.fail:
            raise socket.timeout("stalled")
        self.data += data

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


class SendAllTests(unittest.TestCase):
    def _bridge_with(self, channel):
        bridge = _bridge()
        bridge._shell = channel
        return bridge

    def test_send_input_uses_sendall_and_sends_everything(self):
        channel = _RecordingSendChannel()
        payload = "x" * 40000

        self.assertTrue(self._bridge_with(channel).send_input(payload))
        self.assertEqual(payload.encode(), channel.data)

    def test_send_input_reports_a_write_timeout(self):
        channel = _RecordingSendChannel(fail=True)

        self.assertFalse(self._bridge_with(channel).send_input("yes\n"))


class MarkerPatternTests(unittest.TestCase):
    """The marker patterns require the SOH sentinel and the trailing newline."""

    def test_exit_pattern_waits_for_the_whole_number(self):
        _, exit_pattern = ssh_client.SSHBridge._marker_patterns("END", "EXIT")

        self.assertIsNone(exit_pattern.search(b"\x01EXIT:1"))
        self.assertIsNone(exit_pattern.search(b"\x01EXIT:1\r"))
        # An echoed printf format has the literal characters \001, not SOH.
        self.assertIsNone(exit_pattern.search(b"echo \\001EXIT:123\r\n"))
        match = exit_pattern.search(b"\x01EXIT:123\r\n")
        self.assertIsNotNone(match)
        self.assertEqual(b"123", match.group(1))

    def test_end_pattern_requires_sentinel_and_newline(self):
        end_pattern, _ = ssh_client.SSHBridge._marker_patterns("END", "EXIT")

        self.assertIsNone(end_pattern.search(b"END\r\n"))
        self.assertIsNone(end_pattern.search(b"\x01END"))
        self.assertIsNone(end_pattern.search(b"echo \\001END\r\n"))
        self.assertIsNotNone(end_pattern.search(b"\x01END\r\n"))


if __name__ == "__main__":
    unittest.main()
