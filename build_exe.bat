@echo off
chcp 65001 >nul
echo 正在检查 PyInstaller 打包环境...

python -m pip install pyinstaller --quiet

echo 正在打包为单文件可执行程序 ModelMonitor.exe ...
pyinstaller --onefile --clean --name "ModelMonitor" --add-data "web_ui.html;." monitor_server.py

if exist "dist\ModelMonitor.exe" (
    echo.
    echo ================================================================
    echo 打包成功！可执行文件已生成在: dist\ModelMonitor.exe
    echo 双击 dist\ModelMonitor.exe 即可直接运行，无需 Python 环境。
    echo ================================================================
) else (
    echo 打包失败，请检查报错信息。
)

pause
