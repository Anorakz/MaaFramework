@echo off
setlocal EnableDelayedExpansion

REM ===========================================================================
REM  SongForPrism launcher - pick the Python interpreter that has maafw (import name: maa).
REM  Detection order (probe each with "import maa.library", use the first that works):
REM    1) every "python" on current PATH (e.g. conda base, if user installed maafw there)
REM    2) fallback: the WorkBuddy-bundled interpreter found during task #11
REM  If none works, show the install hint (official PyPI index is REQUIRED:
REM  the machine global pip points at a Tsinghua mirror that does not carry maafw).
REM  This script does NOT self-elevate and does NOT trigger UAC.
REM  All arguments are forwarded to agent\main.py (e.g. --check).
REM  NOTE: batch body is pure ASCII on purpose. Chinese messages live in
REM        msg_install.txt / msg_admin.txt (GBK) and are shown via "type",
REM        because GBK Chinese echoed inside IF blocks can contain low bytes
REM        that equal cmd metacharacters and break parsing. chcp 65001 (UTF-8) is
REM        set so main.py UTF-8 stdout and the UTF-8 hint files display correctly.
REM ===========================================================================

chcp 65001 >nul 2>&1
cd /d "D:\DMM\scsp_MAA"
if errorlevel 1 (
    echo [ERROR] Cannot cd to D:\DMM\scsp_MAA
    if not defined SCSP_NO_PAUSE pause
    exit /b 1
)

set "PYEXE="
set "MAAFW_OK=0"

REM ---- 1) Probe every python on PATH ----
for /f "delims=" %%P in ('where python 2^>nul') do (
    if "!PYEXE!"=="" (
        "%%P" -c "import maa.library" >nul 2>&1
        if !errorlevel! EQU 0 (
            set "PYEXE=%%P"
            set "MAAFW_OK=1"
        )
    )
)

REM ---- 2) Fallback: interpreter with maafw installed (found in task #11) ----
set "WB_PY=C:\Users\Anorak\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if "!PYEXE!"=="" (
    if exist "!WB_PY!" (
        "!WB_PY!" -c "import maa.library" >nul 2>&1
        if !errorlevel! EQU 0 (
            set "PYEXE=!WB_PY!"
            set "MAAFW_OK=1"
        )
    )
)

REM ---- No interpreter with maafw: show install hint (Chinese via type) and exit ----
if "!MAAFW_OK!"=="0" (
    echo.
    type "%~dp0msg_install.txt"
    echo.
    if not defined SCSP_NO_PAUSE pause
    exit /b 3
)

echo [INFO] Using Python: !PYEXE!
"!PYEXE!" -c "from maa.library import Library; print('[INFO] maafw version:', Library.version())"
echo.

REM ---- Admin check (prompt only; NEVER self-elevate / NEVER trigger UAC) ----
net session >nul 2>&1
if !errorlevel! NEQ 0 (
    echo.
    type "%~dp0msg_admin.txt"
    echo.
)

REM ---- Run agent\main.py, forwarding ALL arguments, keep exit code ----
"!PYEXE!" "agent\main.py" %*
set "RC=!errorlevel!"
echo.
echo [DONE] exit code: !RC!
if not defined SCSP_NO_PAUSE pause
exit /b !RC!
