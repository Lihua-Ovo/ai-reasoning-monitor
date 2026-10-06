@echo off
@chcp 65001 >nul
title Unbind AI Client Environment

echo ================================================================
echo    Unbinding AI Client Environment Variables
echo ================================================================

REM Delete User Environment Variables
REG delete "HKCU\Environment" /F /V ANTHROPIC_BASE_URL >nul 2>&1
REG delete "HKCU\Environment" /F /V OPENAI_BASE_URL >nul 2>&1

echo.
echo [OK] Successfully unbound! Removed ANTHROPIC_BASE_URL and OPENAI_BASE_URL.
echo Clients are now restored to default direct connection.
echo ================================================================
pause
