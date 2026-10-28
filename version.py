"""桥接器版本号。

源码里的版本号与即将发布的 Release 保持一致；CI 打 tag 构建时会由
.github/workflows/release.yml 只替换本文件的 __version__ 一行，保证 EXE 里的
版本与 Release 一致。发新版本时先改这里、再打同名 tag。
"""

__version__ = "1.5.0"

#: 该桥接器适配的堡垒机客户端与传参风格（启动横幅展示用）。
BASTION_CLIENT = "齐治 AccessClient"
ARGUMENT_STYLE = "PuTTY 兼容传参"


def version_lines() -> list[str]:
    """Return the human-readable version block shown on the console."""
    return [
        f"桥接器版本   : v{__version__}",
        f"适用堡垒机   : {BASTION_CLIENT}（{ARGUMENT_STYLE}）",
    ]
