<#
.SYNOPSIS
  卸载「齐治堡垒机桥接器」DSH 技能插件（可选：还原齐治客户端的原 putty.exe）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
  powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -RestoreBridge

.PARAMETER RestoreBridge
  将齐治客户端目录中最新的 putty.exe.bak.* 还原为 putty.exe（需管理员权限时自动提权）。
.PARAMETER QizhiDir
  齐治客户端安装目录，默认 C:\Program Files (x86)\QizhiTech\AccessClient。
.PARAMETER WhatIf
  只打印将要执行的操作。
#>
[CmdletBinding()]
param(
    [switch]$RestoreBridge,
    [string]$QizhiDir = "C:\Program Files (x86)\QizhiTech\AccessClient",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message){ Write-Host "    [注意] $Message" -ForegroundColor Yellow }

$SkillTarget = Join-Path $HOME ".dsh\skills\qizhi-bastion"
if ($env:DSH_HOME -and $env:DSH_HOME.Trim().Length -gt 0) {
    $SkillTarget = Join-Path $env:DSH_HOME "skills\qizhi-bastion"
}

Write-Step "卸载 DSH 技能插件：$SkillTarget"
if (Test-Path $SkillTarget) {
    if (-not $WhatIf) {
        Remove-Item $SkillTarget -Recurse -Force
        Write-Ok "已删除。"
    } else {
        Write-Warn2 "WhatIf：将删除 $SkillTarget"
    }
} else {
    Write-Ok "技能插件不存在，无需删除。"
}

if ($RestoreBridge) {
    if (Test-Path $QizhiDir) {
        $TargetExe = Join-Path $QizhiDir "putty.exe"
        $Backup = Get-ChildItem $QizhiDir -Filter "putty.exe.bak.*" -File -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($Backup) {
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
                Write-Step "还原原 putty.exe（备份：$($Backup.Name)）"
                if (-not $WhatIf) {
                    Copy-Item $Backup.FullName $TargetExe -Force
                    Write-Ok "已还原。"
                } else {
                    Write-Warn2 "WhatIf：将还原 $($Backup.FullName) -> $TargetExe"
                }
            } else {
                Write-Warn2 "写入需要管理员权限，正在弹出 UAC 提权窗口……"
                $SelfPath = $MyInvocation.MyCommand.Path
                if ($SelfPath -and -not $WhatIf) {
                    Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
                        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $SelfPath,
                        "-RestoreBridge", "-QizhiDir", "`"$QizhiDir`""
                    )
                } else {
                    Write-Warn2 "无法自动提权，请手动（管理员 PowerShell）："
                    Write-Host "      Copy-Item '$($Backup.FullName)' '$TargetExe' -Force"
                }
            }
        } else {
            Write-Ok "未找到 putty.exe.bak.* 备份文件。"
        }
    } else {
        Write-Warn2 "未找到齐治客户端目录 $QizhiDir。"
    }
}

Write-Host "卸载结束。" -ForegroundColor Green
