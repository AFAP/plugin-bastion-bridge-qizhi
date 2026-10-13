"""空闲保活测试：3 分钟无操作发 ls；有指令在执行（含等返回的长任务）时顺延。"""

import threading
import time
import unittest

import ssh_client
from connection import ConnectionParams


def _make_bridge(interval=180):
    bridge = ssh_client.SSHBridge(
        ConnectionParams(host="127.0.0.1", port=22, username="tester"),
        keepalive_enabled=True,
        keepalive_interval_seconds=interval,
    )
    bridge._client = object()  # is_connected 只看非空
    bridge._transport = _FakeTransport(active=True)
    return bridge


class _FakeTransport:
    def __init__(self, active=True):
        self._active = active

    def is_active(self):
        return self._active


class _Std:
    """exec_command 返回的 stdin/stdout/stderr 桩。"""

    def __init__(self, channel, data=b""):
        self.channel = channel
        self._data = data

    def read(self):
        return self._data


class _BlockingExecChannel:
    """recv_exit_status 会一直阻塞，直到测试放行——模拟长任务在飞。"""

    def __init__(self, started, release):
        self.started = started
        self.release = release

    def shutdown_write(self):
        pass

    def recv_exit_status(self):
        self.started.set()
        self.release.wait(timeout=5)
        return 0


class _BlockingExecClient:
    def __init__(self, started, release):
        self._channel = _BlockingExecChannel(started, release)

    def exec_command(self, command, timeout=None):
        return _Std(self._channel), _Std(self._channel, b"out"), _Std(self._channel, b"")

    def get_transport(self):
        return _FakeTransport(True)


class _SlowStreamChannel:
    """exit_status_ready 在放行前一直返回 False——模拟流式长任务在飞。"""

    def __init__(self, started, release):
        self.started = started
        self.release = release

    def exec_command(self, command):
        self.started.set()

    def shutdown_write(self):
        pass

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return self.release.is_set()

    def recv_exit_status(self):
        return 0

    def close(self):
        pass


class _SlowStreamTransport(_FakeTransport):
    def __init__(self, channel):
        super().__init__(True)
        self._channel = channel

    def open_session(self):
        return self._channel


class _SlowStreamClient:
    def __init__(self, transport):
        self._transport = transport

    def get_transport(self):
        return self._transport


class KeepaliveDueTests(unittest.TestCase):
    """_keepalive_due 判定：空闲满 180s + 已连接 + 无在飞指令。"""

    def test_due_only_after_interval(self):
        bridge = _make_bridge()
        self.assertFalse(bridge._keepalive_due())  # 刚初始化，不空闲
        bridge._last_activity_time = time.time() - 181
        self.assertTrue(bridge._keepalive_due())

    def test_not_due_when_busy(self):
        bridge = _make_bridge()
        bridge._last_activity_time = time.time() - 999
        bridge._mark_busy()
        self.assertFalse(bridge._keepalive_due())
        bridge._mark_idle()  # 结束时重置空闲计时
        self.assertFalse(bridge._keepalive_due())

    def test_not_due_when_disconnected(self):
        bridge = _make_bridge()
        bridge._last_activity_time = time.time() - 999
        bridge._transport = _FakeTransport(active=False)
        self.assertFalse(bridge._keepalive_due())

    def test_not_due_when_disabled(self):
        bridge = _make_bridge()
        bridge._last_activity_time = time.time() - 999
        bridge._keepalive_enabled = False
        self.assertFalse(bridge._keepalive_due())


class BusyTrackingTests(unittest.TestCase):
    """execute / stream_execute 在执行期间把桥标记为忙。"""

    def test_execute_marks_busy_while_waiting_for_exit_code(self):
        started, release = threading.Event(), threading.Event()
        bridge = _make_bridge()
        bridge._client = _BlockingExecClient(started, release)

        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.setdefault("result", bridge.execute("sleep 300")),
            daemon=True,
        )
        worker.start()

        self.assertTrue(started.wait(timeout=2), "指令未进入执行")
        self.assertTrue(bridge._is_busy(), "长任务在飞时应标记为忙")
        self.assertFalse(bridge._keepalive_due(), "在飞时不应触发保活")

        release.set()
        worker.join(timeout=5)
        self.assertFalse(bridge._is_busy())
        self.assertEqual(0, outcome["result"]["exit_code"])

    def test_stream_execute_marks_busy_while_streaming(self):
        started, release = threading.Event(), threading.Event()
        channel = _SlowStreamChannel(started, release)
        bridge = _make_bridge()
        bridge._transport = _SlowStreamTransport(channel)
        bridge._client = _SlowStreamClient(bridge._transport)

        chunks = []
        worker = threading.Thread(
            target=lambda: chunks.extend(bridge.stream_execute("tail -f /var/log/syslog")),
            daemon=True,
        )
        worker.start()

        self.assertTrue(started.wait(timeout=2), "流式指令未启动")
        time.sleep(0.1)  # 让循环跑起来
        self.assertTrue(bridge._is_busy(), "流式长任务在飞时应标记为忙")
        self.assertFalse(bridge._keepalive_due())

        release.set()
        worker.join(timeout=5)
        self.assertFalse(bridge._is_busy())
        self.assertEqual({"type": "exit", "exit_code": 0}, chunks[-1])


class KeepaliveLoopTests(unittest.TestCase):
    """保活线程行为：空闲触发、在飞顺延、解除后立即恢复。"""

    def test_loop_fires_when_idle_and_skips_while_busy(self):
        bridge = _make_bridge()
        bridge._keepalive_interval_seconds = 0  # 立刻到点，聚焦在「忙则顺延」上
        calls = []
        bridge.execute = lambda command, timeout=10: (
            calls.append(command),
            {"stdout": "", "stderr": "", "exit_code": 0},
        )[1]

        bridge._start_keepalive()
        try:
            time.sleep(1.5)
            self.assertTrue(calls, "空闲时保活未触发")

            calls.clear()
            bridge._mark_busy()
            time.sleep(1.5)
            self.assertEqual([], calls, "在飞期间保活不应发送")

            bridge._mark_idle()
            time.sleep(1.5)
            self.assertTrue(calls, "忙完后保活应恢复")
            self.assertTrue(all(command == "ls" for command in calls))
        finally:
            bridge._stop_keepalive()


if __name__ == "__main__":
    unittest.main()
