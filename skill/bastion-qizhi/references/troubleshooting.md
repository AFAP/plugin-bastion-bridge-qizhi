# 排错手册（齐治堡垒机桥接器）

## 症状 → 处理

| 现象 | 排查方向 |
|---|---|
| 端口文件 `active_port.txt` 不存在 | 桥接器未被唤起。请用户到齐治堡垒机控制台点击 SSH 资源（工具选本地工具/putty）。 |
| API 连不通 / 连接被拒 | 端口文件与实际端口不一致（端口被占时桥接器会自动顺延 8767、8768…）。以 `active_port.txt` 为准。 |
| `/health` 返回 `connected: false` | SSH 连接失败。查看日志末尾：认证失败（用户名/密码错）、网络不通、参数解析失败。 |
| `exit_code: -1` | SSH 已断开或未连接。先 `/health`，必要时请用户重新点击资源。 |
| 命令超时 | 命令本身耗时过长或挂起。增大 `timeout`，或改用 `/stream`。 |
| `no acceptable kex algorithm` | 旧服务器算法问题。当前版本已内置 Paramiko 2.12.0 兼容旧算法，如仍出现请升级本工具。 |
| 中文乱码 | 用 `scripts/bridge.py`（已强制 UTF-8）；curl 下为终端编码问题。 |
| 端口被占用 | 桥接器自动顺延端口并写回 `active_port.txt`，无需处理。 |
| 杀毒软件报毒 | PyInstaller 单文件常见误报。加白名单或改用源码方式运行（`python main.py <参数>`）。 |

## 日志位置

```text
%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\logs\bridge.log
```

滚动保留 5 份，单份默认 10MB。命令与返回正文只显示在桥接器控制台，不写入日志文件。
控制台默认用 `>>>` / `<<<` 分块展示指令与服务器返回，交互输入正文始终隐藏。日志关键行为：

- `Parsing argv:` —— 堡垒机实际传参（密码已脱敏），适配新版本齐治时以此为准。
- `SSH connected successfully` / `SSH connect attempt N failed` —— 连接过程。
- 命令长度、退出码与 stdout/stderr 字符数 —— 每条命令的元数据。
- `Keep-alive` —— 空闲保活记录（默认空闲 3 分钟执行一次无害命令，有指令在执行时顺延）。

## 配置文件

```text
%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\config.json
```

首次运行自动生成。修改后需重新唤起桥接器生效。关键项：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `api_host` | `127.0.0.1` | **切勿改为 0.0.0.0**（会暴露到局域网） |
| `api_port` | `8766` | 被占用时自动顺延 |
| `ssh_connect_timeout` | `15` | SSH 握手超时 |
| `retry_max_attempts` | `3` | 连接失败重试次数 |
| `keepalive_enabled` | `true` | 空闲保活开关 |
| `keepalive_interval_seconds` | `180` | 空闲多久触发保活（有指令在执行时顺延） |
| `keepalive_command` | `ls` | 保活命令，须无害 |
| `log_level` | `INFO` | DEBUG/INFO/WARNING/ERROR |

## 齐治版本适配

不同版本齐治堡垒机唤起 putty.exe 的传参格式可能不同。桥接器已支持：

- PuTTY 显式参数：`-ssh user@host -P 22 -pw password`
- PuTTY 会话文件：`-load tmp:C:\...\putty-xxxx-config.txt -pw password`
  （自动解析 `HostName` / `PortNumber` / `UserName` 的 `\`、`=`、`:` 三种分隔格式）

若遇到新格式：查看日志中 `Parsing argv:` 行 → 修改 `params.py` 增加解析分支 → 重新构建。
