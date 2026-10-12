"""危险操作拦截（command_guard + 各 API 入口）测试。"""

import json
import unittest
from unittest.mock import patch

import api_server
import command_guard
from command_guard import BLOCKED_EXIT_CODE, CommandGuard, build_blocked_result


class GuardMatchingTests(unittest.TestCase):
    """规则匹配：该拦的拦住，只读查询不要误伤。"""

    def setUp(self):
        self.guard = CommandGuard.default()

    # -- 必须拦截 ------------------------------------------------------
    def test_blocks_rm_variants(self):
        blocked = [
            "rm -rf /tmp/x",
            "rm -rf /",
            "sudo rm -rf /data",
            "/bin/rm -rf /data",
            "\\rm -rf /data",
            "ls -la && rm -f x",
            "echo start; rm -f /tmp/x",
            "rm --no-preserve-root -rf /",
        ]
        for command in blocked:
            with self.subTest(command=command):
                decision = self.guard.check(command)
                self.assertIsNotNone(decision, f"未拦截：{command}")
                self.assertEqual("delete", decision.rule)

    def test_blocks_wrapped_or_indirect_execution(self):
        """包装器、shell -c、命令替换、find -exec、xargs 都不能绕过拦截。"""
        cases = {
            "sudo rm -rf /data": "delete",
            "sudo -n rm -rf /data": "delete",
            "sudo -u root rm -rf /data": "delete",
            "env FOO=bar rm -rf /data": "delete",
            "timeout 30 rm -rf /data": "delete",
            "nice -n 10 rm -rf /data": "delete",
            "nohup rm -rf /data &": "delete",
            "find /data -exec rm -rf {} +": "delete",
            "find /data -name '*.log' -delete": "delete",
            "xargs rm -rf": "delete",
            "sh -c 'rm -rf /data'": "delete",
            "bash -lc 'rm -rf /data'": "delete",
            "echo $(rm -rf /data)": "delete",
            "echo `rm -rf /data`": "delete",
            "ssh prod01 'rm -rf /data'": "delete",
            "su - root -c 'rm -rf /data'": "delete",
            "if [ -f x ]; then rm -f x; fi": "delete",
        }
        for command, rule in cases.items():
            with self.subTest(command=command):
                decision = self.guard.check(command)
                self.assertIsNotNone(decision, f"未拦截：{command}")
                self.assertEqual(rule, decision.rule)

    def test_blocks_disk_and_power_commands(self):
        cases = {
            "mkfs.ext4 /dev/sdb1": "disk",
            "dd if=/dev/zero of=/dev/sda bs=1M": "disk",
            "fdisk /dev/sdb": "disk",
            "wipefs -a /dev/sdb": "disk",
            "shutdown -h now": "power",
            "reboot": "power",
            "systemctl reboot": "service",
        }
        for command, rule in cases.items():
            with self.subTest(command=command):
                decision = self.guard.check(command)
                self.assertIsNotNone(decision, f"未拦截：{command}")
                self.assertEqual(rule, decision.rule)

    def test_blocks_service_kill_account_and_firewall_changes(self):
        cases = {
            "systemctl stop nginx": "service",
            "systemctl --user restart app": "service",
            "service nginx stop": "service",
            "kill -9 1234": "kill",
            "pkill -f java": "kill",
            "iptables -F": "firewall",
            "setenforce 0": "firewall",
            "userdel deploy": "account",
            "passwd root": "account",
            "crontab -r": "schedule",
            "umount /data": "unmount",
            "truncate -s 0 /var/log/syslog": "truncate",
            "docker rm -f web": "container",
            "docker compose down": "container",
            "kubectl delete pod web-0": "delete",
        }
        for command, rule in cases.items():
            with self.subTest(command=command):
                decision = self.guard.check(command)
                self.assertIsNotNone(decision, f"未拦截：{command}")
                self.assertEqual(rule, decision.rule)

    def test_blocks_pattern_based_dangers(self):
        cases = {
            "echo x > /dev/sda": "device-write",
            "cat /dev/zero > /dev/nvme0n1": "device-write",
            ":(){ :|:& };:": "fork-bomb",
            "chmod -R 777 /": "critical-file",
            "chown -R root:root /": "critical-file",
            "mysql -e 'drop database prod'": "database",
            "psql -c 'truncate table orders'": "database",
        }
        for command, rule in cases.items():
            with self.subTest(command=command):
                decision = self.guard.check(command)
                self.assertIsNotNone(decision, f"未拦截：{command}")
                self.assertEqual(rule, decision.rule)

    # -- 不得误伤 ------------------------------------------------------
    def test_allows_read_only_and_routine_commands(self):
        allowed = [
            "uname -a",
            "df -h",
            "free -h",
            "ls -la /var/log",
            "cat /etc/fstab",
            "systemctl status nginx",
            "systemctl is-active nginx",
            "mount",
            "crontab -l",
            "git init",
            "npm init -y",
            "grep rm /var/log/messages",
            "ps aux | grep kill",
            "tail -n 100 /var/log/syslog",
            "chmod 755 /opt/app",
            "docker ps -a",
            "docker run --rm alpine ls",
            "tar -czf /tmp/backup.tar.gz /etc",
            "scp /tmp/a.txt prod:/tmp/",
            "find /data -name '*.log' -mtime +7",
        ]
        for command in allowed:
            with self.subTest(command=command):
                self.assertIsNone(self.guard.check(command), f"误拦截：{command}")

    def test_empty_and_blank_commands_are_allowed(self):
        for command in (None, "", "   ", "\n"):
            self.assertIsNone(self.guard.check(command))

    # -- /input 拦截 ---------------------------------------------------
    def test_check_input_blocks_command_lines(self):
        self.assertIsNotNone(self.guard.check_input("rm -rf /\n"))
        self.assertIsNotNone(self.guard.check_input("y\nrm -rf /\n"))
        self.assertIsNotNone(self.guard.check_input("echo hi\rmkfs.ext4 /dev/sdb"))

    def test_check_input_allows_plain_answers(self):
        for data in ("y\n", "yes\n", "n\n", "\n", "my-password\n", "sudo-password-1\n"):
            with self.subTest(data=data):
                self.assertIsNone(self.guard.check_input(data))


class GuardConfigurationTests(unittest.TestCase):
    def test_disabled_guard_allows_everything(self):
        guard = CommandGuard.disabled()
        self.assertIsNone(guard.check("rm -rf /"))
        self.assertIsNone(guard.check_input("rm -rf /\n"))

    def test_default_config_enables_guard(self):
        guard = CommandGuard.from_config({})
        self.assertTrue(guard.enabled)
        self.assertIsNotNone(guard.check("rm -rf /"))

    def test_config_can_disable_guard(self):
        guard = CommandGuard.from_config({"dangerous_command_guard_enabled": False})
        self.assertFalse(guard.enabled)
        self.assertIsNone(guard.check("rm -rf /"))

    def test_config_adds_custom_programs_and_patterns(self):
        guard = CommandGuard.from_config(
            {
                "dangerous_command_guard_programs": ["^myctl$"],
                "dangerous_command_guard_patterns": [r"\bsecret-deploy\b"],
            }
        )
        custom_program = guard.check("myctl --purge")
        self.assertIsNotNone(custom_program)
        self.assertEqual("custom", custom_program.rule)
        custom_pattern = guard.check("deploy --secret-deploy now")
        self.assertIsNotNone(custom_pattern)
        self.assertEqual("custom", custom_pattern.rule)

    def test_config_allow_patterns_win_over_builtin_rules(self):
        guard = CommandGuard.from_config(
            {"dangerous_command_guard_allow_patterns": [r"^rm -f /tmp/bridge-"]}
        )
        self.assertIsNone(guard.check("rm -f /tmp/bridge-cache.tmp"))
        self.assertIsNotNone(guard.check("rm -rf /data"))

    def test_config_can_disable_builtin_rule(self):
        guard = CommandGuard.from_config({"dangerous_command_guard_disabled_rules": ["delete"]})
        self.assertIsNone(guard.check("rm -rf /data"))
        self.assertIsNotNone(guard.check("shutdown -h now"))

    def test_config_message_template_placeholders(self):
        guard = CommandGuard.from_config(
            {"dangerous_command_guard_message": "自定义提示：{rule}"}
        )
        decision = guard.check("rm -rf /")
        self.assertIsNotNone(decision)
        self.assertTrue(decision.message.startswith("自定义提示：delete"))

    def test_invalid_config_regex_is_ignored(self):
        guard = CommandGuard.from_config(
            {"dangerous_command_guard_programs": ["([unclosed"], "dangerous_command_guard_patterns": ["("]}
        )
        # 非法正则被忽略，内置规则仍然生效，且不应抛异常。
        self.assertIsNotNone(guard.check("rm -rf /data"))

    def test_broken_message_template_falls_back_to_raw_text(self):
        guard = CommandGuard.from_config({"dangerous_command_guard_message": "提示 {unknown}"})
        decision = guard.check("rm -rf /")
        self.assertIsNotNone(decision)
        self.assertEqual("提示 {unknown}", decision.message)


class BlockedPayloadTests(unittest.TestCase):
    def test_execute_payload_has_normal_result_shape_only(self):
        guard = CommandGuard.default()
        decision = guard.check("rm -rf /data")
        self.assertIsNotNone(decision)
        payload = build_blocked_result(decision)

        self.assertEqual({"stdout", "stderr", "exit_code"}, set(payload))
        self.assertEqual("", payload["stdout"])
        self.assertEqual(BLOCKED_EXIT_CODE, payload["exit_code"])
        self.assertIn("危险操作", payload["stderr"])
        self.assertIn("请手动处理", payload["stderr"])

    def test_input_payload_is_minimal(self):
        guard = CommandGuard.default()
        decision = guard.check_input("rm -rf /\n")
        self.assertIsNotNone(decision)
        payload = command_guard.build_blocked_input_result(decision)

        self.assertEqual({"status", "message"}, set(payload))
        self.assertEqual("refused", payload["status"])
        self.assertIn("请手动处理", payload["message"])


class RecordingBridge:
    """记录调用情况的假桥接器：用于确认被拦截的命令根本没有下发。"""

    def __init__(self):
        self.executed = []
        self.streamed = []
        self.inputs = []

    def is_connected(self):
        return True

    def execute(self, command, timeout=30):
        self.executed.append(command)
        return {"stdout": f"server accepted: {command}", "stderr": "", "exit_code": 0}

    def stream_execute(self, command):
        self.streamed.append(command)
        yield {"type": "stdout", "data": "streamed"}
        yield {"type": "exit", "exit_code": 0}

    def send_input(self, data):
        self.inputs.append(data)
        return True


class ExecuteEndpointTests(unittest.TestCase):
    """POST /execute：危险命令不执行，直接返回提示。"""

    def setUp(self):
        self.bridge = RecordingBridge()
        self.api = api_server.BridgeAPI(self.bridge)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_dangerous_command_is_blocked_and_not_sent(self, console, _blank):
        response = self.api.app.test_client().post(
            "/execute", json={"command": "rm -rf /data", "timeout": 9}
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual({"stdout", "stderr", "exit_code"}, set(payload))
        self.assertEqual(BLOCKED_EXIT_CODE, payload["exit_code"])
        self.assertIn("危险操作", payload["stderr"])
        self.assertIn("请手动处理", payload["stderr"])
        self.assertEqual([], self.bridge.executed, "被拦截的命令不应下发到服务器")

        displayed = "\n".join(str(call.args) for call in console.call_args_list)
        self.assertIn("未下发到目标服务器", displayed)

    def test_blocked_command_body_is_not_written_to_the_log_file(self):
        with patch.object(api_server.logger, "warning") as warning:
            self.api.app.test_client().post("/execute", json={"command": "rm -rf /secret-path"})

        logged = "\n".join(str(call.args) + str(call.kwargs) for call in warning.call_args_list)
        self.assertIn("危险操作已拒绝", logged)
        self.assertNotIn("/secret-path", logged, "日志文件只应记录元数据，不含命令正文")
        self.assertNotIn("delete", logged, "日志不记录内部规则名")

    def test_safe_command_still_executes(self):
        response = self.api.app.test_client().post("/execute", json={"command": "uname -a"})

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual({"stdout", "stderr", "exit_code"}, set(payload))
        self.assertEqual(0, payload["exit_code"])
        self.assertEqual(["uname -a"], self.bridge.executed)

    def test_guard_can_be_disabled_explicitly(self):
        api = api_server.BridgeAPI(self.bridge, command_guard=CommandGuard.disabled())
        response = api.app.test_client().post("/execute", json={"command": "rm -rf /data"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(["rm -rf /data"], self.bridge.executed)


class StreamEndpointTests(unittest.TestCase):
    """GET /stream（SSE）：危险命令不执行，事件流给出提示与退出码 126。"""

    def setUp(self):
        self.bridge = RecordingBridge()
        self.api = api_server.BridgeAPI(self.bridge)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_dangerous_command_is_blocked_and_not_streamed(self, console, _blank):
        response = self.api.app.test_client().get("/stream?command=rm%20-rf%20/data", buffered=True)
        body = response.data.decode("utf-8")

        self.assertEqual(200, response.status_code)
        self.assertIn("event: stderr", body)
        self.assertIn('event: exit\ndata: {"exit_code": 126}', body)
        self.assertIn("请手动处理", body)
        self.assertIn("event: end", body)
        self.assertEqual([], self.bridge.streamed, "被拦截的命令不应下发到服务器")

    def test_safe_command_still_streams(self):
        response = self.api.app.test_client().get("/stream?command=df%20-h", buffered=True)
        body = response.data.decode("utf-8")

        self.assertIn("event: stdout", body)
        self.assertIn('event: exit\ndata: {"exit_code": 0}', body)
        self.assertNotIn("请手动处理", body)
        self.assertEqual(["df -h"], self.bridge.streamed)


class InputEndpointTests(unittest.TestCase):
    """POST /input：交互输入不能成为绕过拦截的通道。"""

    def setUp(self):
        self.bridge = RecordingBridge()
        self.api = api_server.BridgeAPI(self.bridge)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_dangerous_input_is_blocked_and_not_sent(self, console, _blank):
        response = self.api.app.test_client().post("/input", json={"data": "rm -rf /\n"})

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("refused", payload["status"])
        self.assertIn("请手动处理", payload["message"])
        self.assertEqual([], self.bridge.inputs, "被拦截的输入不应发送到 shell 通道")

    def test_plain_answer_is_still_sent(self):
        response = self.api.app.test_client().post("/input", json={"data": "y\n"})

        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.get_json()["status"])
        self.assertEqual(["y\n"], self.bridge.inputs)


class WebSocketTests(unittest.TestCase):
    """Socket.IO：execute / stream / input 三个事件同样受拦截保护。"""

    def setUp(self):
        self.bridge = RecordingBridge()
        self.api = api_server.BridgeAPI(self.bridge)
        self.client = self.api.socketio.test_client(self.api.app)

    def tearDown(self):
        self.client.disconnect()

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_execute_event_blocks_dangerous_command(self, _console, _blank):
        self.client.emit("execute", {"command": "rm -rf /data"})
        received = self.client.get_received()
        results = [item["args"][0] for item in received if item["name"] == "result"]

        self.assertTrue(results, "应返回 result 事件")
        self.assertEqual({"stdout", "stderr", "exit_code"}, set(results[0]))
        self.assertEqual(BLOCKED_EXIT_CODE, results[0]["exit_code"])
        self.assertIn("请手动处理", results[0]["stderr"])
        self.assertEqual([], self.bridge.executed)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_stream_event_blocks_dangerous_command(self, _console, _blank):
        self.client.emit("stream", {"command": "mkfs.ext4 /dev/sdb1"})
        received = self.client.get_received()
        names = [item["name"] for item in received]

        self.assertNotIn("blocked", names, "不向调用方暴露内部策略事件")
        self.assertIn("end", names)
        outputs = [item["args"][0] for item in received if item["name"] == "output"]
        self.assertIn({"type": "exit", "exit_code": BLOCKED_EXIT_CODE}, outputs)
        self.assertEqual([], self.bridge.streamed)

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_input_event_blocks_dangerous_payload(self, _console, _blank):
        self.client.emit("input", {"data": "rm -rf /\n"})
        received = self.client.get_received()
        statuses = [item["args"][0] for item in received if item["name"] == "input_status"]

        self.assertTrue(statuses)
        self.assertFalse(statuses[0]["ok"])
        self.assertIn("请手动处理", statuses[0]["message"])
        self.assertEqual([], self.bridge.inputs)

    def test_execute_event_still_runs_safe_command(self):
        self.client.emit("execute", {"command": "df -h"})
        received = self.client.get_received()
        results = [item["args"][0] for item in received if item["name"] == "result"]

        self.assertTrue(results)
        self.assertEqual(0, results[0]["exit_code"])
        self.assertEqual(["df -h"], self.bridge.executed)


class BridgeBackstopTests(unittest.TestCase):
    """SSHBridge 兜底：即使绕过 API 直接调用，危险命令也不会下发。"""

    def _bridge(self, guard=None):
        import ssh_client
        from connection import ConnectionParams

        return ssh_client.SSHBridge(
            ConnectionParams(host="127.0.0.1", port=22, username="tester"),
            keepalive_enabled=False,
            command_guard=guard,
        )

    def test_execute_returns_prompt_without_connecting(self):
        result = self._bridge().execute("rm -rf /data")

        self.assertEqual({"stdout", "stderr", "exit_code"}, set(result))
        self.assertEqual(BLOCKED_EXIT_CODE, result["exit_code"])
        self.assertIn("请手动处理", result["stderr"])

    def test_stream_execute_yields_prompt_and_blocked_exit_code(self):
        chunks = list(self._bridge().stream_execute("rm -rf /data"))

        self.assertEqual("stderr", chunks[0]["type"])
        self.assertIn("请手动处理", chunks[0]["data"])
        self.assertEqual({"type": "exit", "exit_code": BLOCKED_EXIT_CODE}, chunks[-1])

    def test_send_input_refuses_dangerous_payload(self):
        self.assertFalse(self._bridge().send_input("rm -rf /\n"))

    def test_disabled_guard_falls_through_to_connection_error(self):
        result = self._bridge(CommandGuard.disabled()).execute("rm -rf /data")

        self.assertNotIn("请手动处理", result["stderr"])
        self.assertEqual("SSH not connected", result["error"])

    def test_keepalive_command_is_guarded_too(self):
        bridge = self._bridge()
        self.assertTrue(bridge._command_guard.enabled)


class NoPolicyDisclosureTests(unittest.TestCase):
    """兜底只对内：对外只说「危险操作，请手动处理」，不泄露任何策略细节。"""

    #: 内部字段名与规则名都不允许出现在调用方可观察的输出里。
    LEAK_MARKERS = (
        "blocked",
        "reason",
        "rule",
        "dangerous_command",
        "delete",
        "disk",
        "fork-bomb",
        "device-write",
        "critical-file",
        "database",
    )

    def setUp(self):
        self.bridge = RecordingBridge()
        self.api = api_server.BridgeAPI(self.bridge)

    def _assert_clean(self, text: str, where: str):
        lowered = text.lower()
        for marker in self.LEAK_MARKERS:
            self.assertNotIn(marker, lowered, f"{where} 泄露了内部策略信息：{marker}")

    def test_execute_response_discloses_nothing(self):
        response = self.api.app.test_client().post("/execute", json={"command": "rm -rf /data"})
        payload = response.get_json()

        self.assertEqual({"stdout", "stderr", "exit_code"}, set(payload))
        self._assert_clean(json.dumps(payload, ensure_ascii=False), "/execute")

    def test_stream_response_discloses_nothing(self):
        response = self.api.app.test_client().get("/stream?command=rm%20-rf%20/data", buffered=True)
        body = response.data.decode("utf-8")

        self.assertIn("请手动处理", body)
        self._assert_clean(body, "/stream")

    def test_input_response_discloses_nothing(self):
        response = self.api.app.test_client().post("/input", json={"data": "rm -rf /\n"})
        payload = response.get_json()

        self.assertEqual({"status", "message"}, set(payload))
        self._assert_clean(json.dumps(payload, ensure_ascii=False), "/input")

    @patch("api_server.log_blank_line")
    @patch("api_server.log_console")
    def test_console_and_log_do_not_name_the_rule(self, console, _blank):
        with patch.object(api_server.logger, "warning") as warning:
            self.api.app.test_client().post("/execute", json={"command": "rm -rf /data"})

        console_text = "\n".join(str(call.args) + str(call.kwargs) for call in console.call_args_list)
        log_text = "\n".join(str(call.args) + str(call.kwargs) for call in warning.call_args_list)

        self.assertIn("未下发到目标服务器", console_text)
        self.assertIn("危险操作已拒绝", log_text)
        self._assert_clean(console_text, "控制台")
        self._assert_clean(log_text, "日志")


if __name__ == "__main__":
    unittest.main()
