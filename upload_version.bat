@echo off
setlocal
set /p GITHUB_TOKEN=请输入GITHUB_TOKEN：
cd /d h:\Python\PyQt5\数据生成器
powershell -NoProfile -ExecutionPolicy Bypass -File ".\upload_version.ps1" -Owner "RuiHe-cell" -Repo "data-generator-update-feed" -Branch "main" -TargetPath "version.json"
endlocal
