@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 未找到虚拟环境，请先执行：
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install PySide6 markdown pyflakes
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py
