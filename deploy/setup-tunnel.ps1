# 一键把 ybslmmd.cn 接到本机的 8000 端口（Cloudflare Tunnel）
#
# 用法（PowerShell，管理员启动也可以，但不是必须）：
#   .\deploy\setup-tunnel.ps1 -Domain ybslmmd.cn
#   .\deploy\setup-tunnel.ps1 -Domain ybslmmd.cn -Hostname mmd.ybslmmd.cn
#
# 做四件事：装 cloudflared -> 登录 -> 建隧道 -> 写配置 + 绑 DNS

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Domain,
    [string]$Hostname = "",
    [string]$TunnelName = "mmdhub",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
if (-not $Hostname) { $Hostname = $Domain }

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "    ✓ $msg" -ForegroundColor Green }
function Bad($msg)      { Write-Host "    ✗ $msg" -ForegroundColor Red }

# ---------------------------------------------------------------- 1 安装
Step 1 "检查 cloudflared"
$cf = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cf) {
    Write-Host "    没装，正在用 winget 安装（可能会弹 UAC，点是）…"
    winget install --id Cloudflare.cloudflared -e `
        --accept-package-agreements --accept-source-agreements | Out-Host
    # 刷新 PATH，不然当前窗口还找不到
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $cf = Get-Command cloudflared -ErrorAction SilentlyContinue
    if (-not $cf) {
        Bad "装完还是找不到 cloudflared，请关闭这个窗口重新打开再跑一次"
        exit 1
    }
}
Ok "cloudflared 在 $($cf.Source)"

# ---------------------------------------------------------------- 2 登录
Step 2 "登录 Cloudflare"
$certPath = Join-Path $env:USERPROFILE ".cloudflared\cert.pem"
if (Test-Path $certPath) {
    Ok "已经登录过（$certPath）"
} else {
    Write-Host "    会弹出浏览器，登录后选择域名 $Domain 授权"
    cloudflared tunnel login
    if (-not (Test-Path $certPath)) {
        Bad "没有登录成功。请确认域名 $Domain 已经添加到 Cloudflare 并且状态是 Active"
        exit 1
    }
    Ok "登录完成"
}

# ---------------------------------------------------------------- 3 隧道
Step 3 "创建隧道 $TunnelName"
$tunnels = @()
try { $tunnels = cloudflared tunnel list --output json | ConvertFrom-Json } catch { }
$tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if ($tunnel) {
    Ok "隧道已存在，id = $($tunnel.id)"
} else {
    cloudflared tunnel create $TunnelName | Out-Host
    $tunnels = cloudflared tunnel list --output json | ConvertFrom-Json
    $tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
    if (-not $tunnel) { Bad "创建隧道失败"; exit 1 }
    Ok "已创建，id = $($tunnel.id)"
}
$tunnelId = $tunnel.id

# ---------------------------------------------------------------- 4 配置
Step 4 "写配置文件"
$cfgDir = Join-Path $env:USERPROFILE ".cloudflared"
$cfgPath = Join-Path $cfgDir "config.yml"
$credPath = Join-Path $cfgDir "$tunnelId.json"
if (-not (Test-Path $credPath)) { Bad "找不到凭据文件 $credPath"; exit 1 }

$yaml = @"
tunnel: $tunnelId
credentials-file: "$credPath"

ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
  - service: http_status:404
"@
Set-Content -Path $cfgPath -Value $yaml -Encoding utf8
Ok "已写入 $cfgPath"

# ---------------------------------------------------------------- 5 DNS
Step 5 "绑定域名 $Hostname"
try {
    cloudflared tunnel route dns $TunnelName $Hostname | Out-Host
    Ok "DNS 已指向隧道"
} catch {
    Write-Host "    自动绑定失败，可能是记录已存在。可以到 Cloudflare 后台手动加一条 CNAME：" -ForegroundColor Yellow
    Write-Host "      名称 = $Hostname" -ForegroundColor Yellow
    Write-Host "      目标 = $tunnelId.cfargotunnel.com" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 完成
Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host " 配置完成" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""
Write-Host " 接下来分两个窗口跑："
Write-Host ""
Write-Host "   窗口 1（网站本体）" -ForegroundColor White
Write-Host "     双击 mmd-hub\启动服务.bat"
Write-Host ""
Write-Host "   窗口 2（隧道）" -ForegroundColor White
Write-Host "     cloudflared tunnel run $TunnelName"
Write-Host ""
Write-Host " 然后浏览器打开 https://$Hostname"
Write-Host ""
Write-Host " 两个都通了之后，再做开机自启（见 deploy\DEPLOY.md 第 7 步）" -ForegroundColor DarkGray
