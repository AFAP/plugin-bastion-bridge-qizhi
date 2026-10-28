#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""齐治堡垒机桥接器客户端 —— 供 AI 助手/脚本调用。

纯标准库实现，无需 pip install。自动从本机端口文件发现桥接器地址，
支持健康检查、单次执行、流式输出、交互输入。

用法：
    python bridge.py health
    python bridge.py port
    python bridge.py exec "uname -a" [--timeout 30] [--base 127.0.0.1:8766]
    python bridge.py stream "dmesg | tail -n 5" [--base 127.0.0.1:8766]
    python bridge.py input "y\\n"          [--base 127.0.0.1:8766]

退出码：
    0   请求成功（exec 时表示目标命令 exit_code == 0）
    1   目标命令执行失败；流式执行未收到明确 exit=0（服务端 error、断连、无退出码）
    2   桥接器不可达 / 端口文件缺失 / HTTP 错误
    3   本地参数错误
"""

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

PORT_FILE_WIN = Path(os.environ.get("LOCALAPPDATA", "")) / "sshbridge" / "QizhiSSHBridge" / "active_port.txt"
PORT_FILE_POSIX = Path.home() / ".sshbridge" / "qizhisshbridge" / "active_port.txt"


def ensure_utf8_console() -> None:
    """Windows 下把 stdout/stderr 切到 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def discover_port() -> Optional[str]:
    """从端口文件读取桥接器实际监听地址（127.0.0.1:port）。"""
    for candidate in (PORT_FILE_WIN, PORT_FILE_POSIX):
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def http_json(url: str, payload: Optional[dict] = None, timeout: int = 30):
    """发送 JSON 请求并解析 JSON 响应；返回 (status, body_dict)。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=max(timeout + 10, 15)) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"raw": raw}
    return status, body


def fail(message: str, code: int = 2) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False))
    return code


def cmd_port(args: argparse.Namespace) -> int:
    base = args.base or discover_port()
    if not base:
        return fail("端口文件不存在：桥接器可能未被唤起。请到齐治堡垒机控制台点击 SSH 资源后再试。", 2)
    print(base)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    base = args.base or discover_port()
    if not base:
        return fail("端口文件不存在：桥接器可能未被唤起。请到齐治堡垒机控制台点击 SSH 资源后再试。", 2)
    url = f"http://{base}/health"
    try:
        status, body = http_json(url, timeout=10)
    except urllib.error.URLError as exc:
        return fail(f"桥接器不可达 {url}: {exc}", 2)
    print(json.dumps(body, ensure_ascii=False))
    return 0 if status < 400 and body.get("status") == "ok" else 1


def cmd_exec(args: argparse.Namespace) -> int:
    base = args.base or discover_port()
    if not base:
        return fail("端口文件不存在：桥接器可能未被唤起。请到齐治堡垒机控制台点击 SSH 资源后再试。", 2)
    url = f"http://{base}/execute"
    try:
        status, body = http_json(url, {"command": args.command, "timeout": args.timeout}, timeout=args.timeout)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        return fail(f"桥接器不可达 {url}: {exc}", 2)
    print(json.dumps(body, ensure_ascii=False))
    if status >= 400:
        return 2
    exit_code = body.get("exit_code")
    if exit_code in (None, -1):
        # 目标命令未完成（SSH 未连接、执行异常或超时），不是桥接器不可达。
        return 1
    return 0 if exit_code == 0 else 1


def cmd_stream(args: argparse.Namespace) -> int:
    base = args.base or discover_port()
    if not base:
        return fail("端口文件不存在：桥接器可能未被唤起。请到齐治堡垒机控制台点击 SSH 资源后再试。", 2)
    url = f"http://{base}/stream?command={urllib.parse.quote(args.command)}"
    exit_code = None
    stream_error = False
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as response:
            buffer = b""
            # HTTPResponse.read(n) waits for n bytes (or EOF), which would
            # buffer small SSE events such as an interactive "Password:"
            # prompt.  read1 returns as soon as any data is available.
            read_chunk = getattr(response, "read1", None) or response.read
            for chunk in iter(lambda: read_chunk(4096), b""):
                buffer += chunk
                while b"\n\n" in buffer:
                    raw_event, buffer = buffer.split(b"\n\n", 1)
                    text = raw_event.decode("utf-8", errors="replace")
                    event_type, data = "", ""
                    for line in text.splitlines():
                        if line.startswith("event:"):
                            event_type = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data = line[len("data:"):].strip()
                    if event_type in ("stdout", "stderr"):
                        try:
                            payload = json.loads(data or "{}")
                            content = payload.get("data", "")
                        except json.JSONDecodeError:
                            content = data
                        stream = sys.stdout if event_type == "stdout" else sys.stderr
                        stream.write(content)
                        stream.flush()
                        if not content.endswith("\n"):
                            stream.write("\n")
                    elif event_type == "exit":
                        try:
                            payload = json.loads(data or "{}")
                            value = payload.get("exit_code")
                            exit_code = int(value) if value is not None else None
                        except (json.JSONDecodeError, TypeError, ValueError):
                            exit_code = None
                    elif event_type == "error":
                        stream_error = True
                        print(f"\n[stream error] {data}", file=sys.stderr)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        return fail(f"桥接器不可达 {url}: {exc}", 2)
    except KeyboardInterrupt:
        return 130

    if stream_error:
        print("[stream error] 服务端报告执行失败，未收到正常退出码", file=sys.stderr)
        return 1
    if exit_code is None:
        print("[stream error] 未收到退出码，流式连接可能中断", file=sys.stderr)
        return 1
    return 0 if exit_code == 0 else 1


def cmd_input(args: argparse.Namespace) -> int:
    base = args.base or discover_port()
    if not base:
        return fail("端口文件不存在：桥接器可能未被唤起。请到齐治堡垒机控制台点击 SSH 资源后再试。", 2)
    url = f"http://{base}/input"
    try:
        status, body = http_json(url, {"data": args.data}, timeout=15)
    except urllib.error.URLError as exc:
        return fail(f"桥接器不可达 {url}: {exc}", 2)
    print(json.dumps(body, ensure_ascii=False))
    return 0 if status < 400 and body.get("status") == "ok" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description="齐治堡垒机桥接器客户端（自动发现本机端口，纯标准库）",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_port = sub.add_parser("port", help="打印桥接器实际监听地址")
    p_port.set_defaults(func=cmd_port)

    p_health = sub.add_parser("health", help="健康检查")
    p_health.set_defaults(func=cmd_health)

    p_exec = sub.add_parser("exec", help="执行单条命令")
    p_exec.add_argument("command", help="要执行的命令")
    p_exec.add_argument("--timeout", type=int, default=30, help="超时秒数（默认 30）")
    p_exec.set_defaults(func=cmd_exec)

    p_stream = sub.add_parser("stream", help="流式执行命令（SSE）")
    p_stream.add_argument("command", help="要执行的命令")
    p_stream.add_argument("--timeout", type=int, default=300, help="最长等待秒数（默认 300）")
    p_stream.set_defaults(func=cmd_stream)

    p_input = sub.add_parser("input", help="向交互 shell 发送输入")
    p_input.add_argument("data", help="要发送的数据（如 'y\\n'）")
    p_input.set_defaults(func=cmd_input)

    for subparser in (p_port, p_health, p_exec, p_stream, p_input):
        subparser.add_argument("--base", default=None, help="桥接器地址，如 127.0.0.1:8766（默认自动发现）")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_console()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
