@echo off
setlocal
title Shambhala Reel Creator
chcp 65001 >nul
cd /d "%~dp0"
rem Publishing stays off unless you choose it inside the launcher.
set "IG_PUBLISHING_MODE="
set "IG_ACCESS_TOKEN="
set "RESULT=1"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" reel_launcher.py %*
    call set "RESULT=%%ERRORLEVEL%%"
) else (
    echo The Python environment is missing. See "Windows setup" in README.md.
)
echo.
if not "%RESULT%"=="0" echo The launcher stopped with code %RESULT%. Read the messages above before closing this window.
rem Always wait, so a double-clicked window stays open and errors can be read.
pause
exit /b %RESULT%
