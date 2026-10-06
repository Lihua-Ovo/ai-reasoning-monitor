@echo off
@chcp 65001 >nul
title Bind AI Client Environment

echo ================================================================
echo    Binding AI Client Environment Variables to Monitor (Port 5050)
echo ================================================================

REM Set Windows User Environment Variables
setx ANTHROPIC_BASE_URL "http://127.0.0.1:5050" >nul
setx OPENAI_BASE_URL "http://127.0.0.1:5050/v1" >nul

echo.
echo [OK] Successfully bound!
echo   - Claude Code:         http://127.0.0.1:5050  (Forward to cc-switch: 15721)
echo   - OpenAI / Codex:      http://127.0.0.1:5050/v1 (Forward to cc-switch: 15721)
echo.
echo New CMD / PowerShell windows will automatically use these settings.
echo Run unbind_env.bat anytime to restore default direct connection.
echo ================================================================
pause
