"""Generate the PyInstaller VERSIONINFO resource for ``putty.exe``.

PyInstaller only embeds a Windows VERSIONINFO resource when ``EXE(version=...)``
is given one.  Without it, Explorer's "Details" tab shows no file version,
product name, description, or copyright — the runtime version in ``version.py``
does not affect the PE resource.

``putty.spec`` calls :func:`write_version_info` at build time with the value of
``version.__version__``, so local builds and tag builds always agree with the
console banner and ``/health``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Tuple


def version_tuple(version: str) -> Tuple[int, int, int, int]:
    """Return a Windows four-part version tuple from ``1.2.3`` style input."""
    numbers = [int(part) for part in re.findall(r"\d+", version)[:4]]
    numbers += [0] * (4 - len(numbers))
    return tuple(numbers)  # type: ignore[return-value]


def build_version_info_text(version: str) -> str:
    """Return a PyInstaller ``VSVersionInfo`` expression for ``version``."""
    file_version = version_tuple(version)
    file_version_text = ".".join(str(part) for part in file_version)
    # keep the generated file ASCII-only: PyInstaller evals it as Python source
    description = json.dumps("齐治堡垒机 SSH 桥接器", ensure_ascii=True)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={file_version},
    prodvers={file_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [
            StringStruct('CompanyName', 'AFAP'),
            StringStruct('FileDescription', {description}),
            StringStruct('FileVersion', '{file_version_text}'),
            StringStruct('InternalName', 'putty'),
            StringStruct('LegalCopyright', 'Copyright (c) 2026 plugin-bastion-bridge-qizhi contributors'),
            StringStruct('OriginalFilename', 'putty.exe'),
            StringStruct('ProductName', {description}),
            StringStruct('ProductVersion', '{version}'),
            StringStruct('Comments', 'https://github.com/AFAP/plugin-bastion-bridge-qizhi'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def write_version_info(path: Path, version: str) -> None:
    """Write the VERSIONINFO expression to ``path`` as UTF-8 text."""
    path.write_text(build_version_info_text(version), encoding="utf-8")
