"""桥接器版本号。

源码运行时保持与即将发布的 Release 一致；CI 打 tag 构建时会被
.github/workflows/release.yml 自动改写为标签版本，保证 EXE 里的版本与 Release 一致。
"""

__version__ = "1.3.0"
