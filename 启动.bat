@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"

if not exist "%~dp0autocode\desktop\app.py" (
    echo [错误] 未找到 autocode 桌面版，请确认下载完整。
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
        echo [错误] 创建虚拟环境失败。请先安装 Python 3.10+ 并加入 PATH。
        pause
        exit /b 1
    )
)

"%~dp0.venv\Scripts\python.exe" -c "import webview, psutil" >nul 2>&1
if errorlevel 1 (
    echo [首次运行] 正在安装依赖...
    "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

start "" /d "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run_desktop.pyw"
exit /b 0
