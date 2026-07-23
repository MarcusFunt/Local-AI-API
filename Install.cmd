@echo off
rem ============================================================================
rem  Local AI API - one-click installer.
rem  Double-click this file to open the graphical configurator, where you can
rem  choose models, the default profile, optional auth, Tailscale, the
rem  accelerator, and the auto-update schedule. It then runs the Docker
rem  install/update for you.
rem ============================================================================
setlocal
cd /d "%~dp0"

echo Launching the Local AI API graphical installer...
echo.

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py "%~dp0scripts\install_gui.py"
    goto :after
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python "%~dp0scripts\install_gui.py"
    goto :after
)

echo Python was not found on this machine.
echo.
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH", then double-click Install.cmd again.
echo.
echo Press any key to close...
pause >nul
exit /b 1

:after
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo The installer exited with code %EXITCODE%.
    echo Press any key to close...
    pause >nul
)
exit /b %EXITCODE%
