@echo off
@chcp 65001 >nul
title AI Model and Reasoning Monitor

echo ================================================================
echo    AI Reasoning Monitor (Claude Code & Codex for cc-switch)
echo ================================================================

REM Clean up any old process on port 5050 to avoid WinError 10048
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5050 " ^| findstr "LISTENING"') do (
    echo Terminating old monitor process on port 5050 PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting Monitor Server on http://127.0.0.1:5050 ...
start "" "http://127.0.0.1:5050"
python monitor_server.py 5050

pause
