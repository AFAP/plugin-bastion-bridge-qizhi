# 桥接器 API 参考（齐治堡垒机）

桥接器在本机监听 HTTP/WebSocket，默认 `127.0.0.1:8766`，实际端口以
`%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\active_port.txt` 为准。下文 `<BASE>` 代指实际地址。

## 1. 健康检查

```http
GET <BASE>/health
```

```json
{ "status": "ok", "connected": true }
```

- `connected: true` → SSH 已连接，可执行命令
- `connected: false` → SSH 未连接（失败原因见桥接器日志）

## 2. 执行单次命令

```http
POST <BASE>/execute
Content-Type: application/json

{ "command": "uname -a", "timeout": 30 }
```

响应：

```json
{ "stdout": "Linux ...", "stderr": "", "exit_code": 0 }
```

- `timeout` 可选，默认 30 秒，单位秒。
- `exit_code`：`0` 成功；非 `0` 失败（结合 `stderr`）；`-1` SSH 未连接或执行异常。
- 400：`command` 缺失或非法；500：执行异常（含 `error`/`detail`/`exception_type`）。

## 3. 流式输出（SSE）

```http
GET <BASE>/stream?command=dmesg%20%7C%20tail
```

事件序列：`start` → 若干 `stdout`/`stderr` → `exit`（附退出码）→ `end`；出错时 `error`。

事件格式：

```text
event: stdout
data: {"data": "<输出片段>"}
```

## 4. 交互式输入

```http
POST <BASE>/input
Content-Type: application/json

{ "data": "y\n" }
```

数据发送到桥接器维护的持久交互 shell 通道（PTY）。成功返回 `{"status": "ok", ...}`，
shell 不可用时返回 503。

## 5. WebSocket（Socket.IO）

连接 `ws://<BASE>/ws`（Socket.IO 协议），事件：

| 发送事件 | 载荷 | 接收事件 |
|---|---|---|
| `execute` | `{"command": "...", "timeout": 30}` | `result`（同 /execute 响应） |
| `stream` | `{"command": "..."}` | `start` → `output`（`{type, data}`）→ `end` |
| `input` | `{"data": "y\n"}` | `input_status`（`{ok, message}`） |

连接建立时服务端主动发送 `status` 事件：`{"status": "connected", "ssh_connected": bool}`。

## 6. 错误响应通用格式

```json
{
  "error": "execute failed",
  "detail": "<异常信息>",
  "exception_type": "SomeException"
}
```

## 7. 接口选择建议

| 场景 | 接口 |
|---|---|
| 简单查询（`uname -a`、`df -h`） | `POST /execute` |
| 耗时/持续输出 | `GET /stream` 或 WS `stream` |
| 命令中途需应答 | `POST /input` |
| 长会话、多命令 | WebSocket `/ws` |
