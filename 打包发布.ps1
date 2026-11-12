# 打包发布：生成一个能发给别人的 zip，**不含任何密钥**
#
# 用法：
#   .\打包发布.ps1                  # 不含配布地址（只给模板 + 分区角色）
#   .\打包发布.ps1 -IncludeData     # 连你收集的配布地址一起打包
#
# 永远不打包：data\admin.token、data\totp.secret、data\pending.json、
#            data\rejected.json（含访客 IP）、日志、__pycache__

[CmdletBinding()]
param([switch]$IncludeData)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$stage = Join-Path $env:TEMP "mmdhub-pack-$stamp"
$zip = Join-Path ([Environment]::GetFolderPath("Desktop")) "mmd-hub-$stamp.zip"

Write-Host "准备打包…" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# ---- 白名单：只拷这些 ----
$files = @("index.html", "server.py", "README.md", "Dockerfile", "docker-compose.yml", "启动服务.bat")
foreach ($f in $files) {
    $src = Join-Path $root $f
    if (Test-Path $src) { Copy-Item $src -Destination $stage }
}
if (Test-Path (Join-Path $root "deploy")) {
    Copy-Item (Join-Path $root "deploy") -Destination (Join-Path $stage "deploy") -Recurse
}

# ---- data：只带 published.json，且去掉地址（除非 -IncludeData）----
$dataDir = Join-Path $stage "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$published = Join-Path $root "data\published.json"
if (Test-Path $published) {
    $obj = Get-Content $published -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $IncludeData) {
        $obj.links = [pscustomobject]@{}
        Write-Host "  已清空配布地址（只发模板）" -ForegroundColor DarkGray
    } else {
        Write-Host "  保留了配布地址（-IncludeData）" -ForegroundColor Yellow
    }
    $json = $obj | ConvertTo-Json -Depth 12
    # 关键：不能用 Set-Content -Encoding utf8 —— PowerShell 5.1 会写 BOM，
    # Python 那边解析失败，包里的分区就全丢了。
    [System.IO.File]::WriteAllText((Join-Path $dataDir "published.json"), $json,
        [System.Text.UTF8Encoding]::new($false))
}

# 给收包的人留一张说明
@"
这个包里没有任何密钥。
第一次运行 server.py 会自动生成：
  data\admin.token   静态口令（本机兜底用）
  data\totp.secret   动态口令密钥（手机验证器用）
启动时会打印出来，照着绑定就行。

想自己留一份干净的存档，就别把 data\ 整个发给别人。
"@ | Set-Content (Join-Path $dataDir "密钥说明.txt") -Encoding utf8

# ---- 打 zip ----
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "打包完成：" -ForegroundColor Green
Write-Host "  $zip" -ForegroundColor White
Write-Host ""
Write-Host "包内文件：" -ForegroundColor DarkGray
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
$archive.Entries | ForEach-Object { "  " + $_.FullName }
$archive.Dispose()
Write-Host ""
Write-Host "确认：以上列表里没有 admin.token / totp.secret / pending.json" -ForegroundColor DarkGray
