@echo off
rem windows_first_run.bat - double-click installer for Windows.
rem Picks Python 3.13 (or 3.12/3.14) via the py launcher and runs setup.ps1,
rem bypassing the ExecutionPolicy only for this one script.
rem
rem Before running: install Python 3.13 if you do not have it:
rem     winget install -e --id Python.Python.3.13
setlocal
cd /d "%~dp0"

set "PYBIN="
for %%V in (3.13 3.12 3.14) do (
    if not defined PYBIN (
        py -%%V -c "import sys" >nul 2>&1 && set "PYBIN=py -%%V"
    )
)
if not defined PYBIN (
    where python >nul 2>&1 && set "PYBIN=python"
)
if not defined PYBIN (
    echo [setup] Python 3.12-3.14 not found.
    echo [setup] Install it:  winget install -e --id Python.Python.3.13
    echo [setup] then run this file again.
    pause
    exit /b 1
)
echo [setup] Using %PYBIN%
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -PythonBin "%PYBIN%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo [setup] Finished successfully.) else (echo [setup] Finished with errors, code %RC%. See docs\TROUBLESHOOTING.md)
pause
exit /b %RC%
