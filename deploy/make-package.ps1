# 本地打包：只带代码与部署配置，不带 data / logs / File / .venv。
# 用法：powershell -ExecutionPolicy Bypass -File deploy\make-package.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

$target = Join-Path $dist "autocode-slim-$stamp.zip"
$items = @(
    (Join-Path $root "autocode"),
    (Join-Path $root "deploy"),
    (Join-Path $root "requirements.txt")
)
Compress-Archive -Path $items -DestinationPath $target -Force
Write-Output "package: $target"
Write-Output "size: $([math]::Round((Get-Item $target).Length / 1MB, 2)) MB"
