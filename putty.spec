# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller specification for the Qizhi (齐治) PuTTY bridge.

This produces ``dist\putty.exe``, which is designed to replace the original
PuTTY executable at::

    C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe

The output filename is intentionally ``putty.exe`` because the Qizhi bastion
launches that exact filename.

Usage:
    pyinstaller putty.spec --clean

Or simply run ``build.bat`` which handles the virtual environment and build.
"""

from PyInstaller.building.build_main import Analysis, PYZ, EXE

import sys
from pathlib import Path

# Generate the Windows VERSIONINFO resource from version.py before building.
# Without EXE(version=...), Explorer shows no file version / product name.
SPEC_DIR = Path(SPECPATH)
sys.path.insert(0, str(SPEC_DIR))
from version import __version__  # noqa: E402
from build_version_info import write_version_info  # noqa: E402

VERSION_INFO_PATH = SPEC_DIR / "version_info.txt"
write_version_info(VERSION_INFO_PATH, __version__)

block_cipher = None


analysis = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # SSH engine
        "paramiko",
        "paramiko.transport",
        "paramiko.rsakey",
        "paramiko.ed25519key",
        "paramiko.ecdsakey",
        "paramiko.sftp",
        # Web server
        "flask",
        "flask_socketio",
        "engineio",
        "engineio.async_drivers.threading",
        # Common transitive dependencies
        "cryptography",
        "bcrypt",
        "nacl",
        "nacl.bindings",
        "werkzeug",
        "jsonschema",
        # Standard library modules sometimes dynamically imported
        "uuid",
        "json",
        "logging",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
        "scipy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="putty",
    version=str(VERSION_INFO_PATH),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
