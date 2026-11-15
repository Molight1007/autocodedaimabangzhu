@echo off
setlocal
cd /d "%~dp0"
start "" /d "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" -m autocode.desktop
exit /b 0
