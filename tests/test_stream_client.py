"""Tests for the skill's streaming client exit-code semantics."""

import argparse
import contextlib
import importlib.util
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

# The skill script lives outside this package; avoid writing __pycache__ next to it.
sys.dont_write_bytecode = True

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skill"
    / "bastion-qizhi"
    / "scripts"
    / "bridge.py"
)
_spec = importlib.util.spec_from_file_location("qizhi_skill_bridge", _SCRIPT)
bridge_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge_module)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Read1OnlyResponse:
    """Simulates HTTPResponse: only read1 delivers a small event promptly."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.read1_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size):
        self.read1_calls += 1
        return self._chunks.pop(0) if self._chunks else b""

    def read(self, size):
        raise AssertionError("cmd_stream must use read1, not read")


def _args():
    return argparse.Namespace(base="127.0.0.1:1", command="x", timeout=5)


def _run(payload=None, error=None, response=None):
    def fake_urlopen(url, timeout=None):
        if error is not None:
            raise error
        return response if response is not None else _Response(payload.encode("utf-8"))

    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(bridge_module.urllib.request, "urlopen", fake_urlopen):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = bridge_module.cmd_stream(_args())
    return code, stdout.getvalue(), stderr.getvalue()


class StreamExitCodeTests(unittest.TestCase):
    def test_explicit_zero_is_success(self):
        payload = (
            "event: stdout\ndata: {\"data\": \"ok\\n\"}\n\n"
            "event: exit\ndata: {\"exit_code\": 0}\n\n"
            "event: end\ndata: {}\n\n"
        )
        code, out, err = _run(payload)
        self.assertEqual(0, code)
        self.assertIn("ok", out)

    def test_stream_uses_read1_for_small_prompt_events(self):
        payload = (
            "event: stdout\ndata: {\"data\": \"Password:\"}\n\n"
            "event: exit\ndata: {\"exit_code\": 0}\n\n"
            "event: end\ndata: {}\n\n"
        )
        response = _Read1OnlyResponse([payload.encode("utf-8")])

        code, out, err = _run(response=response)

        self.assertEqual(0, code)
        self.assertIn("Password:", out)
        self.assertGreater(response.read1_calls, 0)

    def test_explicit_non_zero_is_failure(self):
        payload = (
            "event: exit\ndata: {\"exit_code\": 5}\n\n"
            "event: end\ndata: {}\n\n"
        )
        code, _, _ = _run(payload)
        self.assertEqual(1, code)

    def test_server_error_event_is_failure(self):
        payload = (
            "event: start\ndata: {}\n\n"
            "event: error\ndata: {\"error\": \"boom\"}\n\n"
            "event: end\ndata: {}\n\n"
        )
        code, _, err = _run(payload)
        self.assertEqual(1, code)
        self.assertIn("[stream error]", err)

    def test_missing_exit_event_is_failure(self):
        payload = (
            "event: stdout\ndata: {\"data\": \"partial\\n\"}\n\n"
            "event: end\ndata: {}\n\n"
        )
        code, _, err = _run(payload)
        self.assertEqual(1, code)
        self.assertIn("未收到退出码", err)

    def test_transport_error_returns_bridge_unreachable(self):
        code, _, _ = _run(error=urllib.error.URLError("refused"))
        self.assertEqual(2, code)


class ExecExitCodeTests(unittest.TestCase):
    def _run_exec(self, status, body):
        args = argparse.Namespace(base="127.0.0.1:1", command="x", timeout=5)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(bridge_module, "http_json", return_value=(status, body)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                return bridge_module.cmd_exec(args)

    def test_exit_zero_is_success(self):
        self.assertEqual(0, self._run_exec(200, {"stdout": "ok", "stderr": "", "exit_code": 0}))

    def test_non_zero_is_command_failure(self):
        self.assertEqual(1, self._run_exec(200, {"stdout": "", "stderr": "boom", "exit_code": 5}))

    def test_execution_error_is_not_bridge_unreachable(self):
        body = {"stdout": "", "stderr": "", "exit_code": -1, "error": "命令执行超时（>30 秒）"}
        self.assertEqual(1, self._run_exec(200, body))

    def test_http_error_is_bridge_failure(self):
        self.assertEqual(2, self._run_exec(500, {"error": "execute failed"}))


if __name__ == "__main__":
    unittest.main()
