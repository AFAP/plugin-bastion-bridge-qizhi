# 齐治堡垒机 SSH 桥接器 · Qizhi Bastion SSH Bridge

<div align="center">
  <b>中文</b> · <a href="README.en.md">English</a>
</div>

> **让 AI 通过 RESTful API 接管齐治堡垒机里的服务器。**
> 把「只能在专用 exe 客户端里点来点去」的运维操作，变成任何程序都能调用的本地接口。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/AFAP/plugin-bastion-bridge-qizhi)](https://github.com/AFAP/plugin-bastion-bridge-qizhi/releases)
[![Build](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions)](.github/workflows/release.yml)

**两种玩法，各取所需：**

| 你的身份 | 接入方式 |
|---|---|
| 🎯 **DeepSeek Harness 用户** | 一行命令安装技能插件，AI 立即获得服务器运维能力：对话中输入 `/qizhi-bastion`，或直接派任务（"看看 10.2.1.5 的磁盘"） |
| 🌐 **任何 AI / Agent / 脚本** | 桥接器本质是**本地 RESTful API**（HTTP / SSE / WebSocket），本文档就是完整 API 说明——**只要能发 HTTP 请求的 AI 都能接入**（DeepSeek、Claude Code、Cursor、自研 Agent、curl 脚本……），与语言和框架无关 |

---

## 1. 它解决了什么问题

齐治堡垒机的 Web 控制台点击 SSH 资源时，只能唤起 `putty.exe` 等**人机交互式终端**——人可以看到屏幕敲命令，但 AI 程序无法稳定地发送命令并拿到结构化返回结果，于是「让 AI 运维服务器」始终差最后一公里。

本项目把 `putty.exe` 替换为一个**专用桥接器**：

1. 齐治控制台照常唤起 `putty.exe`（兼容 PuTTY 风格传参）；
2. 桥接器解析目标主机/端口/账号/口令，用 Paramiko 建立 SSH 连接；
3. 在本机 `127.0.0.1` 启动一个 HTTP/WebSocket API；
4. AI 助手通过 REST 接口执行命令、流式查看输出、发送交互输入——**像亲手敲命令一样运维服务器**。

```
┌──────────────┐   唤起并传参   ┌───────────────────┐      SSH      ┌──────────────┐
│ 齐治堡垒机     │ ───────────▶ │  桥接器 putty.exe  │ ───────────▶ │  目标服务器    │
│ Web 控制台    │               │  (本项目构建产物)   │               │ (Linux/Unix) │
└──────────────┘               └─────────┬─────────┘               └──────────────┘
                                          │ 127.0.0.1:<端口>
                                          │ HTTP / SSE / WebSocket
                                 ┌────────▼─────────┐
                                 │   AI 助手 / 脚本   │
                                 │  (DeepSeek 等)   │
                                 └──────────────────┘
```

> 💡 **API 是唯一的接入契约**：桥接器不绑定任何特定 AI。无论 AI 跑在哪个框架里，
> 只要它能调用 `POST /execute`、`GET /stream`、`POST /input`（完整说明见第 7 节与
> [docs/通用桥接器使用说明.md](docs/通用桥接器使用说明.md)），就能立刻运维目标服务器。
> DeepSeek Harness 技能插件只是把这份 API 说明书「翻译」成了该平台的技能格式。

## 2. 功能特性

- ✅ **单一 EXE、免安装**：PyInstaller 单文件打包，直接替换原 `putty.exe` 即可，无需管理员权限运行
- ✅ **兼容齐治传参**：支持 PuTTY 显式参数、`-load` 临时会话文件（`\`、`=`、`:` 三种格式自动解析）
- ✅ **兼容老旧服务器**：内置旧版 KEX/加密/MAC 算法（`group14-sha1`、`3des-cbc`、`hmac-sha1` 等）
- ✅ **双通道执行**：优先 `exec_command`，自动降级交互 shell，适配只允许 PTY 的受限服务器
- ✅ **完整 API**：`/health`、`/execute`、`/stream`(SSE)、`/input`、`/ws`(Socket.IO)
- ✅ **端口自动发现**：实际监听地址写入 `active_port.txt`，AI 免配置直接读取
- ✅ **空闲保活**：默认 60 秒无命令自动发无害命令，防止堡垒机断连
- ✅ **安全默认**：仅监听 `127.0.0.1`、日志全量脱敏、不持久化任何凭证
- ✅ **DeepSeek Harness 技能插件**：一键安装后 AI 自动获得「运维服务器」能力

## 3. 效果预览

| 场景 | 截图 |
|---|---|
| 在 DeepSeek Harness 对话中直接派运维任务，AI 自动唤起技能并执行 | <img src="screenshot/DeepSeek%20Harness中使用方式.png" alt="DeepSeek Harness 中使用方式" width="480"> |
| AI 通过桥接器 API 在目标服务器执行命令、拿到结构化结果 | <img src="screenshot/DeepSeek%20Harness中使用效果.png" alt="DeepSeek Harness 中使用效果" width="480"> |
| 用 Postman 调试本地 REST API（`/health`、`/execute`） | <img src="screenshot/Postman调用示意.jpg" alt="Postman 调用示意" width="480"> |

> 说明：示例中的 `8766` 为默认端口，实际端口以桥接器启动时写出的 `active_port.txt` 为准。

## 4. 目录结构

```text
plugin-bastion-bridge-qizhi/
├── main.py                  # 入口：解析参数 → 建连 → 起 API
├── params.py                # 齐治/PuTTY 参数解析
├── ssh_client.py            # SSH 引擎（Paramiko）
├── api_server.py            # 本地 AI API（Flask + SocketIO）
├── connection.py            # 连接参数数据类
├── config.py                # 本地配置
├── logger.py                # 滚动日志 + 脱敏
├── requirements.txt         # 运行/构建依赖（已锁定版本，CI 可复现）
├── putty.spec               # PyInstaller 打包配置
├── skill/qizhi-bastion/     # DeepSeek Harness 技能插件（安装后位于用户目录 ~\.dsh\skills\qizhi-bastion）
│   ├── SKILL.md             #   技能指令（AI 的操作手册）
│   ├── scripts/bridge.py    #   AI 可调用的命令行客户端（纯标准库）
│   └── references/          #   API 参考 + 排错手册
├── scripts/
│   ├── install.ps1          # 一键安装（技能插件 + 可选桥接器 EXE）
│   └── uninstall.ps1        # 一键卸载/还原
├── docs/
│   └── 通用桥接器使用说明.md   # 面向使用者/AI 的通用桥接器接口使用说明
├── screenshot/              # 效果截图（README「效果预览」用）
└── .github/workflows/release.yml  # 打 tag 自动构建 putty.exe 并发布 Release
```

## 5. 快速开始

### 5.1 一行命令安装 DeepSeek Harness 技能插件（推荐）

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/AFAP/plugin-bastion-bridge-qizhi/main/scripts/install.ps1 | iex"
```

脚本会把技能包安装到 `%USERPROFILE%\.dsh\skills\qizhi-bastion\`（若设置了 `DSH_HOME` 则用 `DSH_HOME`）。
DeepSeek Harness 实时监听该目录，**安装后无需重启**：

- 在 DSH 对话中直接输入 `/qizhi-bastion` 唤起技能；
- 或直接给 AI 派任务（"帮我看看 10.2.1.5 的磁盘使用率"），AI 会自动调用该技能。

### 5.2 同时部署桥接器 EXE（从 Release 下载）

```powershell
# 本地克隆仓库后运行（可离线安装技能）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallBridge
```

脚本会：从最新 Release 下载 `putty.exe` → 备份原文件 → 替换到
`C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe`（需要管理员权限时自动弹出 UAC）。

### 5.3 源码构建（开发者）

仓库不携带本地构建脚本与产物，编译由 GitHub Actions 自动完成：

- 打 tag（如 `v1.1.0`）→ Actions 在 Windows 环境按锁定的 `requirements.txt` 用 PyInstaller 构建
  `dist\putty.exe` 并自动发布 Release（含 SHA-256 校验和）；
- 无 tag 时也可在 Actions 页面点 `Run workflow` 手动构建验证。

本地想跑一遍：`python -m pip install -r requirements.txt` 后执行
`python -m PyInstaller putty.spec --clean --noconfirm`，产物在 `dist\putty.exe`。

## 6. 部署（替换 putty.exe）

拿到 `putty.exe`（Release 下载或本地构建）后，用它**覆盖**齐治客户端目录中的原文件：

```text
C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe
```

### 6.1 ① 备份原文件（务必先做）

```powershell
Copy-Item "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe" "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe.bak" -Force
```

> 使用 `scripts\install.ps1 -InstallBridge` 时会自动备份为 `putty.exe.bak.<时间戳>`，无需手动执行本步。

### 6.2 ② 覆盖部署

```powershell
Copy-Item ".\dist\putty.exe" "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe" -Force
```

### 6.3 ③ 验证

在齐治控制台点击 SSH 资源 → 桥接器自动建立连接并启动 API；
查看实际端口：`%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\active_port.txt`。

### 6.4 想还原官方 putty.exe？

```powershell
Copy-Item "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe.bak" "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe" -Force
```

或运行 `scripts\uninstall.ps1 -RestoreBridge`（自动从最新的 `.bak` 还原，需要管理员权限时自动提权）。

## 7. REST API 速查

| 操作 | 请求 |
|---|---|
| 健康检查 | `GET /health` |
| 执行命令 | `POST /execute` `{"command": "df -h", "timeout": 30}` |
| 流式输出(SSE) | `GET /stream?command=dmesg%20%7C%20tail` |
| 交互输入 | `POST /input` `{"data": "y\n"}` |
| WebSocket | `ws://127.0.0.1:<端口>/ws`（Socket.IO，事件 `execute`/`stream`/`input`） |

```bash
# 健康检查
curl http://127.0.0.1:8766/health

# 执行命令
curl -X POST http://127.0.0.1:8766/execute -H "Content-Type: application/json" -d '{"command": "uname -a"}'
```

响应：

```json
{ "stdout": "Linux server 5.15.0 ...\n", "stderr": "", "exit_code": 0 }
```

完整接口文档见 [docs/通用桥接器使用说明.md](docs/通用桥接器使用说明.md) 与技能包内 `references/api.md`。

## 8. 配置项

配置文件：`%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\config.json`（首次运行自动生成）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `api_host` | `127.0.0.1` | **切勿改为 0.0.0.0**，否则局域网可调用 |
| `api_port` | `8766` | 被占用时自动顺延 8767、8768… |
| `ssh_connect_timeout` | `15` | SSH 握手超时（秒） |
| `retry_max_attempts` | `3` | 连接失败重试次数 |
| `keepalive_enabled` | `true` | 空闲保活开关 |
| `keepalive_interval_seconds` | `60` | 空闲多久触发保活 |
| `keepalive_command` | `ls` | 保活命令（须无害） |
| `log_level` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `log_max_bytes` | `10485760` | 单日志文件上限（10MB） |
| `log_backup_count` | `5` | 滚动保留份数 |
| `console_log` | `true` | 是否同时输出到控制台 |

## 9. 日志与排错

日志：`%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\logs\bridge.log`（滚动保留 5 份）

| 现象 | 排查方向 |
|---|---|
| 点击 SSH 没反应 | 检查 putty.exe 是否替换到正确路径 |
| API 连不通 | 看 `active_port.txt` 实际端口（可能已顺延） |
| `/health` 返回 `connected: false` | 看日志末尾：认证失败 / 网络不通 / 参数解析失败 |
| `no acceptable kex algorithm` | 已内置 Paramiko 2.12.0 兼容旧算法，若仍出现请提 Issue |

完整排错手册见 [docs/通用桥接器使用说明.md](docs/通用桥接器使用说明.md) 与技能包内 `references/troubleshooting.md`。

## 10. 安全与合规（务必阅读）

- 本项目替换 `putty.exe` 后**会接管堡垒机下发的口令**，请仅在**被授权环境**中部署。
- API 无鉴权但仅监听 `127.0.0.1`，**本机任何进程均可调用**——请勿在多人共用终端上运行。
- 日志全量脱敏（口令显示为 `***`），不持久化任何凭证。
- 详见 [SECURITY.md](SECURITY.md)。

## 11. FAQ

**Q：杀毒软件报毒？** PyInstaller 单文件 EXE 常见误报，请加白名单，或用 `python main.py <参数>` 源码方式运行。

**Q：支持哪些齐治版本？** 参数解析基于 PuTTY 兼容传参实现；若贵司版本传参不同，查看日志 `Parsing argv:` 行后在 `params.py` 增加分支即可（欢迎提 PR）。

**Q：多个桥接器实例会冲突吗？** 不会。端口自动顺延，各自写入 `active_port.txt`，以文件为准。

**Q：密码会不会泄漏？** 仅存在于进程内存；日志、`to_dict()` 诊断输出均脱敏为 `***`。

## 12. 开发与构建

模块分层：入口 `main.py` → 参数 `params.py` → SSH 引擎 `ssh_client.py` → API `api_server.py`，
对外 API 契约与 [docs/通用桥接器使用说明.md](docs/通用桥接器使用说明.md) 保持一致，
适配其他堡垒机厂商时只需替换 `params.py` 并调整 `main.py` 的应用名/端口。

**CI 发布流程**：给仓库打 tag（如 `v1.0.0`）→ GitHub Actions 在 Windows 环境
按锁定的 `requirements.txt` 构建 `putty.exe` → 自动发布到 Release（含 SHA-256 校验和）。
手动构建也可在 Actions 页面点 `Run workflow`。

## 13. 相关文档

- [docs/通用桥接器使用说明.md](docs/通用桥接器使用说明.md) —— 通用堡垒机桥接器接口使用说明（使用者/AI 视角）
- [SECURITY.md](SECURITY.md) —— 安全设计与风险告知
- [README.en.md](README.en.md) —— English version

## 14. License

[MIT](LICENSE) © plugin-bastion-bridge-qizhi contributors

> 免责声明：本工具仅供授权运维、安全测试与学习研究使用。使用者须保证对目标服务器及堡垒机环境拥有合法操作权限，因滥用造成的后果与项目维护者无关。