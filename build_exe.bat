@echo off
title Build Standalone ModelMonitor.exe

echo ================================================================
echo    Building Standalone ModelMonitor.exe
echo ================================================================

echo [1/3] Installing PyInstaller if missing...
python -m pip install pyinstaller

taskkill /F /IM ModelMonitor.exe >nul 2>&1

echo.
echo [2/3] Compiling ModelMonitor.exe with embedded web UI...
python -m PyInstaller --onefile --clean --name "ModelMonitor" --add-data "web_ui.html;." monitor_server.py

echo.
echo [3/3] Checking build output...
if exist "dist\ModelMonitor.exe" (
    echo ================================================================
    echo [SUCCESS] ModelMonitor.exe has been generated successfully!
    echo Location: dist\ModelMonitor.exe
    echo You can double-click dist\ModelMonitor.exe to run without Python.
    echo ================================================================
) else (
    echo ================================================================
    echo [ERROR] Build failed. Please check the error output above.
    echo ================================================================
)

pause
