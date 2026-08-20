# 一键安装「齐治堡垒机桥接器」DeepSeek Harness 技能插件（可选：同时部署桥接器 putty.exe）。
#
# 1. 把仓库中的技能包 skill/bastion-qizhi 安装到 %USERPROFILE%\.dsh\skills\bastion-qizhi
#    （若设置了 DSH_HOME 环境变量则使用 DSH_HOME）。DeepSeek Harness 实时监听该目录，
#    安装后无需重启即可在会话中使用 /bastion-qizhi 或让 AI 自动调用。
# 2. 使用 -InstallBridge 时，从 GitHub Release 下载 putty.exe，备份并替换齐治客户端
#    目录中的原 putty.exe（需要管理员权限时自动弹出 UAC 提权）。
#
# 示例：
#   从 GitHub 一行安装：
#     powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/AFAP/plugin-bastion-bridge-qizhi/main/scripts/install.ps1 | iex"
#   本地克隆目录内安装（离线）：
#     powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   同时部署桥接器 EXE：
#     powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallBridge
#
# 参数：Owner（GitHub 用户名/组织名）、Repo（仓库名）、Ref（分支）、
#      InstallBridge（部署 EXE）、BridgeTag（Release 标签）、
#      QizhiDir（齐治客户端目录）、WhatIf（预演）。
# 参数通过 $args 手工解析：兼容 irm|iex 一行安装 与 -File 两种调用方式
# （iex 执行字符串时不允许 param() 块，故不用正式参数声明）。
$Owner = ""
$Repo = "plugin-bastion-bridge-qizhi"
$Ref = "main"
$InstallBridge = $false
$BridgeTag = "latest"
$QizhiDir = "C:\Program Files (x86)\QizhiTech\AccessClient"
$WhatIf = $false

for ($i = 0; $i -lt $args.Count; $i++) {
    switch -Regex ($args[$i]) {
        '^(-|/)InstallBridge$' { $InstallBridge = $true }
        '^(-|/)Owner$'         { $i++; if ($i -lt $args.Count) { $Owner = $args[$i] } }
        '^(-|/)Repo$'          { $i++; if ($i -lt $args.Count) { $Repo = $args[$i] } }
        '^(-|/)Ref$'           { $i++; if ($i -lt $args.Count) { $Ref = $args[$i] } }
        '^(-|/)BridgeTag$'     { $i++; if ($i -lt $args.Count) { $BridgeTag = $args[$i] } }
        '^(-|/)QizhiDir$'      { $i++; if ($i -lt $args.Count) { $QizhiDir = $args[$i] } }
        '^(-|/)WhatIf$'        { $WhatIf = $true }
    }
}

# >>> 默认发布账号（已设置）。换账号/组织时改这里即可，一行命令保持零参数。 <<<
$DefaultOwner = "AFAP"

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message){ Write-Host "    [注意] $Message" -ForegroundColor Yellow }

function Resolve-SkillsRoot {
    if ($env:DSH_HOME -and $env:DSH_HOME.Trim().Length -gt 0) {
        return Join-Path $env:DSH_HOME "skills"
    }
    return Join-Path $HOME ".dsh\skills"
}

# ---------- 1. 定位技能包（本地克隆优先，否则从 GitHub 下载仓库 zip） ----------
$SkillsRoot = Resolve-SkillsRoot
$SkillTarget = Join-Path $SkillsRoot "bastion-qizhi"
$LegacySkillTarget = Join-Path $SkillsRoot "qizhi-bastion"
$ScriptDir = $null
if ($PSScriptRoot) { $ScriptDir = $PSScriptRoot }
$BundleSource = $null
$LocalBundle = $null
if ($ScriptDir) { $LocalBundle = Join-Path $ScriptDir "..\skill\bastion-qizhi" }

if ($LocalBundle -and (Test-Path (Join-Path $LocalBundle "SKILL.md"))) {
    $BundleSource = $LocalBundle
    Write-Step "使用本地技能包：$LocalBundle"
} else {
    if (-not $Owner) { $Owner = $DefaultOwner }
    if (-not $Owner) {
        Write-Host "错误：无法确定 GitHub 仓库 Owner。" -ForegroundColor Red
        Write-Host "  本地未找到技能包，且未提供 -Owner 参数；请："
        Write-Host "  1) 在本地克隆目录内运行本脚本（离线安装），或"
        Write-Host "  2) 运行：irm ...install.ps1 | iex 之前，在脚本顶部把 `$DefaultOwner 改成你的 GitHub 用户名，或"
        Write-Host "  3) 显式传入：-Owner <你的GitHub用户名>"
        exit 1
    }
    $ZipUrl = "https://codeload.github.com/$Owner/$Repo/zip/refs/heads/$Ref"
    $ZipPath = Join-Path $env:TEMP "plugin-bastion-bridge-qizhi-$Ref.zip"
    $ExtractDir = Join-Path $env:TEMP "plugin-bastion-bridge-qizhi-$Ref"
    Write-Step "从 GitHub 下载技能包：$ZipUrl"
    if (-not $WhatIf) {
        Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing
        if (Test-Path $ExtractDir) { Remove-Item $ExtractDir -Recurse -Force }
        Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
        $RefFolder = $Ref -replace '/', '-'
        $BundleSource = Join-Path $ExtractDir "$Repo-$RefFolder\skill\bastion-qizhi"
    }
}

if (-not (Test-Path (Join-Path $BundleSource "SKILL.md"))) {
    Write-Host "错误：未找到技能包 SKILL.md：$BundleSource" -ForegroundColor Red
    exit 1
}

# ---------- 2. 安装技能插件 ----------
Write-Step "安装 DSH 技能插件到：$SkillTarget"
if (-not $WhatIf) {
    if (Test-Path $SkillTarget) { Remove-Item $SkillTarget -Recurse -Force }
    if (Test-Path $LegacySkillTarget) {
        Remove-Item $LegacySkillTarget -Recurse -Force
        Write-Ok "已删除旧技能 qizhi-bastion。"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $SkillTarget -Parent) | Out-Null
    Copy-Item -Path $BundleSource -Destination $SkillTarget -Recurse
    # 清理本地开发时产生的 Python 缓存，保证安装目录始终干净
    Get-ChildItem $SkillTarget -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "技能文件："
    Get-ChildItem $SkillTarget -Recurse -File | ForEach-Object { Write-Host "      $($_.FullName)" }
} else {
    Write-Warn2 "WhatIf：将复制 $BundleSource -> $SkillTarget"
    Write-Warn2 "WhatIf：若存在则删除旧技能 $LegacySkillTarget"
}

Write-Host ""
Write-Host "======== 技能插件安装完成 ========" -ForegroundColor Green
Write-Host "DeepSeek Harness 已实时加载该技能（无需重启）。使用方法："
Write-Host "  * 在 DSH 对话中直接输入 /bastion-qizhi 唤起技能；"
Write-Host "  * 或让 AI 完成与「齐治堡垒机服务器操作」相关的任务，它会自动调用该技能。"
Write-Host ""

# ---------- 3. 可选：部署桥接器 putty.exe ----------
if ($InstallBridge) {
    if (-not $Owner) { $Owner = $DefaultOwner }
    if (-not $Owner) {
        Write-Warn2 "未提供 -Owner，跳过桥接器 EXE 下载（技能插件已安装）。"
    } else {
        $ExeUrl = "https://github.com/$Owner/$Repo/releases/latest/download/putty.exe"
        if ($BridgeTag -ne "latest") {
            $ExeUrl = "https://github.com/$Owner/$Repo/releases/download/$BridgeTag/putty.exe"
        }
        $StageDir = Join-Path $env:LOCALAPPDATA "sshbridge\QizhiSSHBridge\release"
        $StageExe = Join-Path $StageDir "putty.exe"
        Write-Step "下载桥接器 putty.exe：$ExeUrl"
        if (-not $WhatIf) {
            New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
            Invoke-WebRequest -Uri $ExeUrl -OutFile $StageExe -UseBasicParsing
            Write-Ok "已下载到：$StageExe"
        } else {
            Write-Warn2 "WhatIf：将下载 $ExeUrl -> $StageExe"
        }

        if (Test-Path $QizhiDir) {
            $TargetExe = Join-Path $QizhiDir "putty.exe"
            $BackupExe = Join-Path $QizhiDir ("putty.exe.bak." + (Get-Date -Format "yyyyMMddHHmmss"))

            $CanWrite = $false
            if (-not $WhatIf) {
                try {
                    $probe = Join-Path $QizhiDir ".writeprobe_$([guid]::NewGuid().ToString('N'))"
                    New-Item -ItemType File -Path $probe -Force | Out-Null
                    Remove-Item $probe -Force
                    $CanWrite = $true
                } catch { $CanWrite = $false }
            }

            if ($CanWrite) {
                Write-Step "替换齐治客户端 putty.exe：$TargetExe"
                if (-not $WhatIf) {
                    if (Test-Path $TargetExe) {
                        Copy-Item $TargetExe $BackupExe -Force
                        Write-Ok "原文件已备份为：$BackupExe"
                    }
                    Copy-Item $StageExe $TargetExe -Force
                    Write-Ok "桥接器已部署。回到齐治控制台点击 SSH 资源即可唤起。"
                } else {
                    Write-Warn2 "WhatIf：将备份并替换 $TargetExe"
                }
            } else {
                Write-Warn2 "写入 $QizhiDir 需要管理员权限，正在弹出 UAC 提权窗口……"
                $SelfPath = $MyInvocation.MyCommand.Path
                if ($SelfPath) {
                    if (-not $WhatIf) {
                        Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
                            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $SelfPath,
                            "-Owner", $Owner, "-Repo", $Repo, "-Ref", $Ref,
                            "-InstallBridge", "-BridgeTag", $BridgeTag, "-QizhiDir", "`"$QizhiDir`""
                        )
                    } else {
                        Write-Warn2 "WhatIf：将提权重跑脚本完成替换。"
                    }
                } else {
                    Write-Warn2 "当前通过管道运行无法自动提权，请手动完成（管理员 PowerShell）："
                    Write-Host "      Copy-Item '$StageExe' '$TargetExe' -Force   # 建议先备份原 putty.exe"
                }
            }
        } else {
            Write-Warn2 "未找到齐治客户端目录 $QizhiDir，跳过替换。桥接器已下载到 $StageExe，可手动复制到齐治客户端目录。"
        }
    }
}

Write-Host "安装结束。" -ForegroundColor Green
