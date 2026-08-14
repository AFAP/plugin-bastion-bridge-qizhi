@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: Build script for Qizhi (齐治) PuTTY bridge.
:: Produces a single-file Windows EXE named dist\putty.exe.
::
:: The resulting putty.exe is intended to replace the original file at:
::   C:\Program Files (x86)\QizhiTech\AccessClient\putty.exe
::
:: Requirements:
::   - Python 3.10+ installed and on PATH.
::
:: Usage:
::   build.bat           Build the EXE (creates .venv on first run).
::   build.bat clean     Clean previous build artifacts before building.

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "BUILD_LOG=%PROJECT_DIR%build.log"

cd /d "%PROJECT_DIR%" || (
    echo [ERROR] Failed to enter project directory: %PROJECT_DIR% >&2
    exit /b 1
)

echo ========================================
echo  Qizhi PuTTY Bridge - Build Script
echo ========================================
echo Project dir: %PROJECT_DIR%
echo.

if /i "%~1"=="clean" (
    echo [INFO] Cleaning previous build artifacts...
    if exist "build" rmdir /s /q "build"
    if exist "dist" rmdir /s /q "dist"
    if exist "%BUILD_LOG%" del /q "%BUILD_LOG%"
    echo [INFO] Clean complete.
)

:: Verify host Python exists.
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH. Please install Python 3.10+ and try again. >&2
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [INFO] Host Python: %%v

:: Create virtual environment if missing.
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [INFO] Creating virtual environment: %VENV_DIR%
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. >&2
        exit /b 1
    )
)

:: Upgrade build tools.
echo [INFO] Upgrading pip / setuptools / wheel...
"%PYTHON%" -m pip install --upgrade pip setuptools wheel >> "%BUILD_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip/setuptools/wheel. See %BUILD_LOG% >&2
    exit /b 1
)

:: Install project dependencies.
if exist "requirements.txt" (
    echo [INFO] Installing dependencies from requirements.txt...
    "%PYTHON%" -m pip install -r requirements.txt >> "%BUILD_LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to install requirements. See %BUILD_LOG% >&2
        exit /b 1
    )
) else (
    echo [WARN] requirements.txt not found; skipping dependency install.
)

:: Install PyInstaller if not already present.
echo [INFO] Ensuring PyInstaller is installed...
"%PYTHON%" -m pip install "PyInstaller>=5.0" >> "%BUILD_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller. See %BUILD_LOG% >&2
    exit /b 1
)

:: Build the EXE.
echo [INFO] Running PyInstaller...
"%PYTHON%" -m PyInstaller "putty.spec" --clean --noconfirm >> "%BUILD_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See %BUILD_LOG% >&2
    exit /b 1
)

:: Report result.
set "EXE_PATH=%PROJECT_DIR%dist\putty.exe"
if exist "%EXE_PATH%" (
    echo.
    echo ========================================
    echo  BUILD SUCCESSFUL
    echo ========================================
    echo Output: %EXE_PATH%
    for %%F in ("%EXE_PATH%") do echo Size:  %%~zF bytes
    echo.
    echo Next steps:
    echo   1. Back up the original PuTTY at:
    echo      C:\Program Files ^(x86^)\QizhiTech\AccessClient\putty.exe
    echo   2. Rename or remove the original putty.exe.
    echo   3. Copy the new putty.exe to that directory.
    echo   4. In the Qizhi bastion console, click an SSH resource to launch the bridge.
) else (
    echo [ERROR] Expected EXE not found: %EXE_PATH% >&2
    exit /b 1
)

endlocal

:: Only pause when the script is launched interactively.
echo %cmdcmdline% | findstr /i " /c " >nul
if errorlevel 1 pause
