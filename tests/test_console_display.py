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
from version import __version__


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

    def test_console_formatter_colors_headlines_only(self):
        formatter = logger._ConsoleFormatter(use_colors=True)

        def render(message, level=logging.INFO):
            record = logging.LogRecord("t", level, __file__, 1, message, (), None)
            setattr(record, logger._CONSOLE_ONLY_ATTR, True)
            return formatter.format(record)

        # 标题行按类型上色
        self.assertIn(logger._ConsoleFormatter._CYAN, render(">>> 收到服务器指令"))
        self.assertIn(logger._ConsoleFormatter._GREEN, render("<<< 服务器执行结果"))
        self.assertIn(logger._ConsoleFormatter._RED, render("⛔ 未下发到目标服务器"))
        self.assertIn(logger._ConsoleFormatter._MAGENTA, render("║ 横幅 ║"))

        # 边框与正文保持终端默认色：整块上色会让长输出更难读
        self.assertNotIn("\033[", render("┌─ 指令正文"))
        self.assertNotIn("\033[", render("rm -rf /data"))
        self.assertNotIn("\033[", render("Linux web01 5.15.0"))
        self.assertNotIn("\033[", render("└─ 返回结束"))

    def test_console_formatter_without_colors_returns_plain_text(self):
        formatter = logger._ConsoleFormatter(use_colors=False)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, ">>> x", (), None)
        setattr(record, logger._CONSOLE_ONLY_ATTR, True)
        self.assertEqual(">>> x", formatter.format(record))

    @patch("main.log_blank_line")
    @patch("main.log_console")
    def test_startup_banner_shows_version(self, console, _blank):
        main._display_startup([])

        displayed = _render_calls(console)
        self.assertIn(f"v{__version__}", displayed)
        self.assertIn("版本", displayed)
        self.assertIn("齐治 AccessClient", displayed)

    @patch("main.get_log_path", return_value=Path("bridge.log"))
    @patch("main.get_config_path", return_value=Path("config.json"))
    @patch("main.log_blank_line")
    @patch("main.log_console")
    def test_runtime_config_shows_channel_and_paths(self, console, _blank, _cfg, _log):
        from command_guard import CommandGuard

        main._display_runtime_config(
            {
                "ssh_prefer_shell": True,
                "keepalive_enabled": True,
                "keepalive_interval_seconds": 180,
                "keepalive_command": "ls",
                "console_output_limit": 12000,
                "console_verbose": False,
            },
            CommandGuard.from_config({}),
        )

        displayed = _render_calls(console)
        self.assertIn("交互 Shell", displayed)
        self.assertIn("运行环境", displayed)
        self.assertIn("配置文件", displayed)
        self.assertIn("文件日志", displayed)
        self.assertIn("危险操作", displayed)

    def test_health_reports_version(self):
        api = api_server.BridgeAPI(FakeBridge())
        response = api.app.test_client().get("/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual(__version__, response.get_json()["version"])

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
