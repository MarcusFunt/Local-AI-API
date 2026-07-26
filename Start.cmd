@echo off
rem ============================================================================
rem  Local AI API - one-click start.
rem  Double-click this file to start the already-installed Docker stack
rem  (Ollama + gateway + Agent Zero) and open the status page in your browser.
rem  It does NOT rebuild or update; run Install.cmd for that.
rem ============================================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-stack.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo Start failed with exit code %EXITCODE%.
    echo Press any key to close...
    pause >nul
)
exit /b %EXITCODE%
