---
name: bastion-qizhi
description: 通过齐治堡垒机桥接器的本地 REST API 在目标服务器上执行命令、流式查看输出、发送交互输入，让 AI 直接接管并运维服务器。
whenToUse: 用户要求操作齐治堡垒机已连接的服务器（查询状态、执行命令、看日志、部署、排障等），或提到堡垒机/桥接器/sshbridge/8766 端口时。
disable-model-invocation: true
---

# 齐治堡垒机 SSH 桥接器操作技能

## 背景

齐治堡垒机的 Web 控制台原本只能唤起 `putty.exe` 等交互式终端，AI 无法稳定执行命令。
「桥接器」是一个替换 `putty.exe` 的 Windows EXE：用户在堡垒机控制台点击 SSH 资源时，
它会自动建立到目标服务器的 SSH 连接，并在本机 `127.0.0.1` 启动一个 HTTP/WebSocket API。
本技能教你通过这个 API 像亲手敲命令一样运维服务器。

```
堡垒机控制台 --唤起--> 桥接器(putty.exe) --SSH--> 目标服务器
                              |
                              +-- 127.0.0.1:<端口> REST API <-- 你(AI)调用
```

## 第一步：发现桥接器端口（每次操作前必做）

桥接器启动时把实际监听地址写入本机文件：

```text
%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\active_port.txt
```

内容形如 `127.0.0.1:8766`。使用本技能目录下的命令行工具可一步完成发现+健康检查：

```powershell
python "<本技能目录>\scripts\bridge.py" health
```

- 输出含 `"connected": true` → 桥接器已就绪，可以执行命令。
- 输出 `connected: false` → SSH 未连通，进入「排错速查」，不要盲目重试命令。
- 报错"端口文件不存在 / 服务不可达" → 桥接器未被唤起。**停止操作**，请用户到齐治堡垒机控制台
  点击目标 SSH 资源（工具选本地工具/putty），等待数秒后再重试健康检查。

> 注意：端口文件是本机唯一事实来源。多台服务器/多个桥接器实例会使用不同端口（8766、8767…），
> 每次操作前重新读取，不要沿用记忆中的旧端口。

## 第二步：执行命令

### 方式 A：脚本（推荐）

```powershell
python "<本技能目录>\scripts\bridge.py" exec "uname -a"
python "<本技能目录>\scripts\bridge.py" exec "df -h" --timeout 30
```

脚本自动发现端口、发送请求、把结果以 UTF-8 JSON 打印到 stdout，并在桥接器返回的
`exit_code` 非 0 时以非零进程退出码退出（方便管道判断）。JSON 字段：

```json
{ "stdout": "...", "stderr": "...", "exit_code": 0 }
```

### 方式 B：直接 HTTP

```bash
curl -X POST http://127.0.0.1:8766/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "df -h", "timeout": 30}'
```

### 退出码语义（务必遵守）

| exit_code | 含义 | 处理 |
|---|---|---|
| 0 | 成功（即使 stdout 为空也正常） | 直接采用输出 |
| 非 0 | 命令失败 | 结合 `stderr` 分析原因，向用户解释，不要反复重试同一命令 |
| -1 | SSH 未连接或执行异常 | 先执行 `health` 排错，不要继续发命令 |

## 第三步：长任务 / 流式输出

耗时或持续输出的命令（`ping`、`tail -f`、长编译、大文件操作）用流式接口：

```powershell
python "<本技能目录>\scripts\bridge.py" stream "dmesg | tail -n 5"
```

HTTP 等价形式：`GET http://127.0.0.1:8766/stream?command=...`，返回 SSE 事件流
（`start` → `stdout`/`stderr` → `exit` → `end`）。

规则：
- 预计 30 秒内完成的命令用 `exec`；超过或不确定用 `stream`。
- 单次输出超过 ~50KB 的命令，结果只摘取关键片段，并明确告知用户"输出过长已截断"。

## 交互式输入

命令中途需要应答（`y/n` 确认、sudo 密码等）时：

```powershell
python "<本技能目录>\scripts\bridge.py" input "y`n"
```

发送给桥接器持久 shell 通道。**安全规则**：除非用户明确授权，绝不自动发送 `y`、
密码或任何确认输入。

## 安全红线（不可违反）

1. API 仅监听 `127.0.0.1`，但**本机任何进程都能调用**——等于拥有该服务器 shell。
   只在你和用户都明确授权的会话中执行操作。
2. 不得把命令输出中的口令、私钥、令牌等敏感内容写入文件或转发到本机之外；
   转述给用户时先脱敏（如 `password=***`）。
3. 危险操作（`rm -rf`、`shutdown`、`reboot`、`iptables` 变更、`mkfs`、
   生产数据库的 DDL/DML、批量删除）执行前必须逐条向用户确认并获得明确同意。
4. 所有命令经堡垒机审计通道执行，不要尝试绕过、提权或清理历史记录。

## 排错速查

| 现象 | 处理 |
|---|---|
| 端口文件不存在 | 桥接器未启动：请用户在齐治控制台点击 SSH 资源唤起它 |
| `/health` 返回 `connected: false` | SSH 连接失败：请用户查看 `%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\logs\bridge.log` 末尾的报错（认证失败/网络不通/参数解析失败），据实向用户说明 |
| `exit_code: -1` | SSH 已断开：先 `health`，必要时请用户重新点击资源 |
| 命令超时 | 增大 `--timeout`，或改用 `stream` |
| 中文乱码 | 用 `scripts/bridge.py`（已做 UTF-8 处理），不要手工拼接 curl |

详细接口与排错见本技能目录：
- `references/api.md` —— 全部 REST/SSE/WebSocket 接口参考
- `references/troubleshooting.md` —— 完整排错手册

## 典型会话示例

用户："帮我看看 10.2.1.5 的磁盘和内存。"
1. `python "<本技能目录>\scripts\bridge.py" health` → 确认 connected。
2. `python "<本技能目录>\scripts\bridge.py" exec "df -h"` → 汇总磁盘。
3. `python "<本技能目录>\scripts\bridge.py" exec "free -h"` → 汇总内存。
4. 用自然语言向用户报告结果（异常项重点说明）。
