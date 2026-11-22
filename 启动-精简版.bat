@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"

if not exist "%~dp0autocode\slim2\server.py" (
    echo [错误] 未找到 autocode\slim2，请确认文件完整。
    pause
    exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [首次运行] 正在创建虚拟环境...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv "%~dp0.venv"
    ) else (
        python -m venv "%~dp0.venv"
    )
    if errorlevel 1 (
        echo [失败] 创建虚拟环境失败，请先安装 Python 3.10+。
        pause
        exit /b 1
    )
    "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
)

if not defined AUTOCODE_SLIM2_HOST set AUTOCODE_SLIM2_HOST=127.0.0.1
if not defined AUTOCODE_SLIM2_PORT set AUTOCODE_SLIM2_PORT=8767
if not defined AUTOCODE_SLIM2_ALLOWED_HOSTS set AUTOCODE_SLIM2_ALLOWED_HOSTS=127.0.0.1,localhost

echo.
echo autoCode 精简版（导入 + 生成数据）正在启动...
echo   监听：%AUTOCODE_SLIM2_HOST%:%AUTOCODE_SLIM2_PORT%
echo   首次启动的密码写在 data\slim2-initial-password.txt
echo   数据库：data\slim2.db（与完整版互不影响）
echo.

"%~dp0.venv\Scripts\python.exe" -m autocode.slim2.server
pause
