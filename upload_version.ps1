param(
    [string]$Owner,
    [string]$Repo,
    [string]$Branch = "main",
    [string]$TargetPath = "version.json",
    [string]$Token = $env:GITHUB_TOKEN,
    [string]$FilePath = (Join-Path $PSScriptRoot "File\version.json"),
    [string]$Message = "Update version.json"
)

if ([string]::IsNullOrWhiteSpace($Owner) -or [string]::IsNullOrWhiteSpace($Repo)) {
    throw "Owner 和 Repo 不能为空"
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    throw "GITHUB_TOKEN 未设置"
}

if (-not (Test-Path -Path $FilePath)) {
    throw "找不到文件: $FilePath"
}

$content = Get-Content -Path $FilePath -Raw -Encoding UTF8
$contentBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($content))

$headers = @{
    "Authorization" = "Bearer $Token"
    "Accept" = "application/vnd.github+json"
    "User-Agent" = "version-uploader"
}

$uri = "https://api.github.com/repos/$Owner/$Repo/contents/$TargetPath"
$sha = $null

try {
    $existing = Invoke-RestMethod -Method Get -Uri "$uri?ref=$Branch" -Headers $headers
    $sha = $existing.sha
} catch {
    $sha = $null
}

$body = @{
    message = $Message
    content = $contentBase64
    branch = $Branch
}

if ($sha) {
    $body.sha = $sha
}

$jsonBody = $body | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Put -Uri $uri -Headers $headers -Body $jsonBody
