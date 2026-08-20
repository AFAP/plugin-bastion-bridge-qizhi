import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api_server
import logger
import main
import params


def _render_calls(mock) -> str:
    rendered = []
    for call in mock.call_args_list:
        args = call.args
        if not args:
            continue
        message = args[0]
        values = args[1:]
        rendered.append(message % values if values else message)
    return "\n".join(rendered)


class FakeBridge:
    def is_connected(self):
        return True

    def execute(self, command, timeout=30):
        return {
            "stdout": f"server accepted: {command}",
            "stderr": "",
            "exit_code": 0,
        }

    def stream_execute(self, command):
        yield {"type": "stdout", "data": "x" * 300}
        yield {"type": "exit", "exit_code": 0}

    def send_input(self, data):
        return True


class ConsoleDisplayTests(unittest.TestCase):
    def test_console_only_records_are_rejected_by_file_filter(self):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "body", (), None)
        setattr(record, logger._CONSOLE_ONLY_ATTR, True)
        self.assertFalse(logger._ExcludeConsoleOnlyFilter().filter(record))

        normal = logging.LogRecord("test", logging.INFO, __file__, 1, "metadata", (), None)
        self.assertTrue(logger._ExcludeConsoleOnlyFilter().filter(normal))

    def test_live_body_reaches_console_but_not_log_file(self):
        root = logging.getLogger(logger.APP_NAME)
        original_handlers = list(root.handlers)
        original_level = root.level
        for handler in original_handlers:
            root.removeHandler(handler)

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "bridge.log"
                console_stream = io.StringIO()
                with patch.object(logger, "get_log_path", return_value=log_path), patch.object(
                    logger.sys, "stderr", console_stream
                ):
                    logger.setup_logging(level=logging.INFO, console=True)
                    logger.log_console("console-visible-sample-body")
                    root.info("persistent metadata")
                    active_handlers = list(root.handlers)
                    for handler in active_handlers:
                        handler.flush()
                        handler.close()
                        root.removeHandler(handler)

                persisted = log_path.read_text(encoding="utf-8")
                self.assertIn("persistent metadata", persisted)
                self.assertNotIn("console-visible-sample-body", persisted)
                self.assertIn("console-visible-sample-body", console_stream.getvalue())
                self.assertNotIn("persistent metadata", console_stream.getvalue())
        finally:
            root.setLevel(original_level)
            for handler in original_handlers:
                root.addHandler(handler)

    def test_verbose_console_can_show_routine_metadata(self):
        normal = logging.LogRecord("test", logging.INFO, __file__, 1, "metadata", (), None)
        self.assertFalse(logger._ConsoleVisibilityFilter(verbose=False).filter(normal))
        self.assertTrue(logger._ConsoleVisibilityFilter(verbose=True).filter(normal))

    def test_preview_escapes_terminal_controls(self):
        preview, truncated = api_server._preview_text("\x1b[2Jhello\x00", 100)
        self.assertFalse(truncated)
        self.assertNotIn("\x1b", preview)
        self.assertIn("\\x1b[2Jhello\\x00", preview)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_execute_shows_command_and_server_result(self, console, _blank):
        api = api_server.BridgeAPI(FakeBridge())
        response = api.app.test_client().post(
            "/execute",
            json={"command": "uname -a", "timeout": 9},
        )

        self.assertEqual(response.status_code, 200)
        displayed = _render_calls(console)
        self.assertIn("uname -a", displayed)
        self.assertIn("server accepted: uname -a", displayed)
        self.assertIn("退出码 0", displayed)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_interactive_input_body_remains_hidden(self, console, _blank):
        api = api_server.BridgeAPI(FakeBridge())
        response = api.app.test_client().post(
            "/input",
            json={"data": "sample-interactive-secret\n"},
        )

        self.assertEqual(response.status_code, 200)
        displayed = _render_calls(console)
        self.assertNotIn("sample-interactive-secret", displayed)
        self.assertIn("内容已安全隐藏", displayed)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_stream_console_preview_is_bounded_and_exit_code_is_preserved(
        self, console, _blank
    ):
        api = api_server.BridgeAPI(FakeBridge(), console_output_limit=256)
        response = api.app.test_client().get("/stream?command=whoami", buffered=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'event: exit\ndata: {"exit_code": 0}', response.data)
        displayed = _render_calls(console)
        self.assertIn("流式执行完成", displayed)
        self.assertIn("后续内容不再刷屏", displayed)

    @patch("main.log_blank_line")
    @patch("main.log_console")
    def test_putty_startup_arguments_are_redacted(self, console, _blank):
        main._display_startup(
            [
                "-ssh",
                "operator@example.invalid",
                "-pw",
                "sample-putty-secret",
                "ssh://operator:sample-url-secret@example.invalid",
                "--token=sample-token-secret",
            ]
        )

        displayed = _render_calls(console)
        self.assertNotIn("sample-putty-secret", displayed)
        self.assertNotIn("sample-url-secret", displayed)
        self.assertNotIn("sample-token-secret", displayed)
        self.assertIn("***", displayed)

    def test_parser_diagnostic_argv_is_redacted(self):
        masked = params._mask_argv(
            ["-pw", "sample-parser-secret", "operator:sample-url-secret@example.invalid"]
        )
        rendered = " ".join(masked)
        self.assertNotIn("sample-parser-secret", rendered)
        self.assertNotIn("sample-url-secret", rendered)
        self.assertIn("***", rendered)


if __name__ == "__main__":
    unittest.main()
