@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

set "PROJECT_DIR=%~dp0"
set "VENV_PYTHON=I:\.PythonVenv\.venv_deploy\Scripts\python.exe"
set "VENV_PYINSTALLER=I:\.PythonVenv\.venv_deploy\Scripts\pyinstaller.exe"

echo ==========================================
echo   数据生成器 - 打包工具
echo ==========================================
echo.

if not exist "%VENV_PYINSTALLER%" (
    echo [错误] 未找到 PyInstaller，请先安装
    pause
    exit /b 1
)

if exist "%PROJECT_DIR%dist" (
    echo [清理] 删除旧的 dist 目录...
    rmdir /s /q "%PROJECT_DIR%dist" 2>nul
)

if exist "%PROJECT_DIR%build" (
    echo [清理] 删除旧的 build 目录...
    rmdir /s /q "%PROJECT_DIR%build" 2>nul
)

echo.
echo [1/2] 正在打包 exe (这可能需要几分钟)...
echo.

"%VENV_PYINSTALLER%" ^
    --name="数据生成器" ^
    --onefile ^
    --windowed ^
    --clean ^
    --noconfirm ^
    --strip ^
    --exclude-module=PySide6.Qt3DAnimation ^
    --exclude-module=PySide6.Qt3DCore ^
    --exclude-module=PySide6.Qt3DExtras ^
    --exclude-module=PySide6.Qt3DInput ^
    --exclude-module=PySide6.Qt3DLogic ^
    --exclude-module=PySide6.Qt3DRender ^
    --exclude-module=PySide6.QtBluetooth ^
    --exclude-module=PySide6.QtCharts ^
    --exclude-module=PySide6.QtDataVisualization ^
    --exclude-module=PySide6.QtDesigner ^
    --exclude-module=PySide6.QtGraphs ^
    --exclude-module=PySide6.QtHelp ^
    --exclude-module=PySide6.QtHttpServer ^
    --exclude-module=PySide6.QtLocation ^
    --exclude-module=PySide6.QtMultimedia ^
    --exclude-module=PySide6.QtMultimediaWidgets ^
    --exclude-module=PySide6.QtNetworkAuth ^
    --exclude-module=PySide6.QtNfc ^
    --exclude-module=PySide6.QtOpenGL ^
    --exclude-module=PySide6.QtOpenGLWidgets ^
    --exclude-module=PySide6.QtPdf ^
    --exclude-module=PySide6.QtPdfWidgets ^
    --exclude-module=PySide6.QtPositioning ^
    --exclude-module=PySide6.QtPrintSupport ^
    --exclude-module=PySide6.QtQml ^
    --exclude-module=PySide6.QtQuick ^
    --exclude-module=PySide6.QtQuick3D ^
    --exclude-module=PySide6.QtQuickControls2 ^
    --exclude-module=PySide6.QtQuickTest ^
    --exclude-module=PySide6.QtQuickWidgets ^
    --exclude-module=PySide6.QtRemoteObjects ^
    --exclude-module=PySide6.QtScxml ^
    --exclude-module=PySide6.QtSensors ^
    --exclude-module=PySide6.QtSerialBus ^
    --exclude-module=PySide6.QtSerialPort ^
    --exclude-module=PySide6.QtSpatialAudio ^
    --exclude-module=PySide6.QtSql ^
    --exclude-module=PySide6.QtStateMachine ^
    --exclude-module=PySide6.QtSvg ^
    --exclude-module=PySide6.QtSvgWidgets ^
    --exclude-module=PySide6.QtTest ^
    --exclude-module=PySide6.QtTextToSpeech ^
    --exclude-module=PySide6.QtUiTools ^
    --exclude-module=PySide6.QtWebChannel ^
    --exclude-module=PySide6.QtWebEngineCore ^
    --exclude-module=PySide6.QtWebEngineQuick ^
    --exclude-module=PySide6.QtWebEngineWidgets ^
    --exclude-module=PySide6.QtWebSockets ^
    --exclude-module=PySide6.QtWebView ^
    --hidden-import=PySide6.QtCore ^
    --hidden-import=PySide6.QtGui ^
    --hidden-import=PySide6.QtWidgets ^
    --collect-all=PySide6 ^
    --paths="%PROJECT_DIR%" ^
    "%PROJECT_DIR%main.py"

if %ERRORLEVEL% neq 0 (
    echo.
    echo [错误] 打包失败！
    pause
    exit /b 1
)

echo.
echo [2/2] 清理临时文件...
if exist "%PROJECT_DIR%build" (
    rmdir /s /q "%PROJECT_DIR%build" 2>nul
)

echo.
echo ==========================================
echo   打包完成！
echo ==========================================
echo.
echo 输出文件: %PROJECT_DIR%dist\数据生成器.exe
echo.
echo 提示: 将此 exe 文件复制到没有 Python 的电脑上即可直接运行
echo.
pause
