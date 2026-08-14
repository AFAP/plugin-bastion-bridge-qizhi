# Qizhi Bastion SSH Bridge · plugin-bastion-bridge-qizhi

> **Give your AI a RESTful API into the servers behind the Qizhi (齐治) bastion host.**
> Turn operations that used to require clicking around a proprietary Windows client into plain local HTTP calls any program can use.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/AFAP/plugin-bastion-bridge-qizhi)](https://github.com/AFAP/plugin-bastion-bridge-qizhi/releases)

**Two ways in, one contract:**

| You are | How you plug in |
|---|---|
| 🎯 **DeepSeek Harness user** | One-line install of the skill plugin — your AI gains server-ops ability right away (`/qizhi-bastion`) |
| 🌐 **Any AI / agent / script** | The bridge is just a **local RESTful API** (HTTP / SSE / WebSocket) and this README is its API reference — **any AI that can send HTTP requests can use it** (Claude Code, Cursor, custom agents, curl…), framework-agnostic |

> 💡 **The API is the only integration contract.** No AI-vendor lock-in: if a model can call
> `POST /execute`, `GET /stream`, and `POST /input` (full reference in “REST API quick reference”),
> it can operate the target servers. The DeepSeek Harness skill merely translates this API spec
> into that platform's skill format.

## What it does

The Qizhi bastion web console launches `putty.exe` when you click an SSH asset. This project replaces that `putty.exe` with a **bridge**:

1. Parses the PuTTY-style arguments (including `-load` temp session files) passed by the bastion console;
2. Opens an SSH connection to the target server via Paramiko (legacy KEX/cipher compatibility included);
3. Starts a loopback-only HTTP/WebSocket API on `127.0.0.1:8766`;
4. An AI assistant then runs commands, streams output, and sends interactive input — turning the bastion into an AI-operable control plane.

```
Bastion web console --invokes--> bridge putty.exe --SSH--> target server
                                       |
                                       +-- 127.0.0.1:<port> HTTP/SSE/WebSocket <-- AI assistant
```

## One-line install (DeepSeek Harness skill)

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/AFAP/plugin-bastion-bridge-qizhi/main/scripts/install.ps1 | iex"
```

This installs the skill bundle into `%USERPROFILE%\.dsh\skills\qizhi-bastion\`. DeepSeek Harness
hot-reloads that directory, so the skill is available immediately: type `/qizhi-bastion` in a chat,
or just assign the AI a server task and it will load the skill automatically.

To also deploy the bridge EXE (downloads `putty.exe` from the latest GitHub Release, backs up and
replaces the original at `C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe`, UAC when needed):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallBridge
```

## Build from source

```bat
build.bat            :: creates .venv, installs pinned deps, runs PyInstaller
:: output: dist\putty.exe
```

CI: push a tag (e.g. `v1.0.0`) and GitHub Actions builds `putty.exe` on a Windows runner with the
pinned `requirements.txt` and attaches it to a Release automatically.

## Deploy (replace putty.exe)

The bridge is used by **overwriting** the original client binary:

```text
C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe
```

1. **Back up the original first** (admin shell):

   ```powershell
   Copy-Item "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe" "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe.bak" -Force
   ```

   (`scripts\install.ps1 -InstallBridge` does this automatically as `putty.exe.bak.<timestamp>`.)

2. **Overwrite** with the bridge build:

   ```powershell
   Copy-Item ".\dist\putty.exe" "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe" -Force
   ```

3. **Verify**: click an SSH asset in the Qizhi console, then check
   `%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\active_port.txt` for the live API endpoint.

4. **Restore** the official client anytime (or run `scripts\uninstall.ps1 -RestoreBridge`):

   ```powershell
   Copy-Item "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe.bak" "C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe" -Force
   ```

## REST API quick reference

| Operation | Request |
|---|---|
| Health | `GET /health` |
| Execute | `POST /execute` `{"command": "df -h", "timeout": 30}` |
| Stream (SSE) | `GET /stream?command=dmesg%20%7C%20tail` |
| Interactive input | `POST /input` `{"data": "y\n"}` |
| WebSocket | `ws://127.0.0.1:<port>/ws` (Socket.IO events: `execute` / `stream` / `input`) |

The actual port is always written to `%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\active_port.txt`
(the bridge auto-increments to 8767, 8768… if 8766 is taken).

## Configuration

`%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\config.json` (created on first run):

| Key | Default | Notes |
|---|---|---|
| `api_host` | `127.0.0.1` | **Never set to 0.0.0.0** |
| `api_port` | `8766` | Auto-increments if busy |
| `ssh_connect_timeout` | `15` | SSH handshake timeout |
| `retry_max_attempts` | `3` | Connect retries |
| `keepalive_enabled` | `true` | Idle keep-alive (prevents bastion disconnects) |
| `log_level` | `INFO` | DEBUG / INFO / WARNING / ERROR |

Logs: `%LOCALAPPDATA%\sshbridge\QizhiSSHBridge\logs\bridge.log` (rotating, passwords redacted).

## Security & compliance

- The bridge receives credentials from the bastion console — **deploy only in authorized environments**.
- The API binds loopback only but has **no auth**: any local process can call it. Do not run on shared machines.
- Passwords are never persisted; all logs are redacted. See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © plugin-bastion-bridge-qizhi contributors

> Disclaimer: for authorized operations, security testing, and research only. Users are responsible for lawful use.
