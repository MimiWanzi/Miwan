# 一键重连临时隧道：换新网址 → 更新 config.toml → 重启网关。
# 断线的症状：手机打开公网地址报 Error 1016 / 530。双击「重连隧道.cmd」跑一次即可。
# 免费临时隧道的网址每次重启都会变，所以"重连"就是上面三件事一次做完。
#
# 网关两种形态都认：同目录/dist 下的 RemoteAgent.exe（exe 双击场景）
# 优先，没有才用 python -m gateway serve（开发场景）。
# 杀旧网关按**端口**找 PID——exe 和 python 通吃，按进程名找会漏掉 exe。

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Port = 8765
$OutLog = Join-Path $Root "data\tunnel.out.log"
$ErrLog = Join-Path $Root "data\tunnel.err.log"

# exe 的候选位置：脚本旁边、dist\ 里
$GatewayExe = @((Join-Path $Root "RemoteAgent.exe"), (Join-Path $Root "dist\RemoteAgent.exe")) |
    Where-Object { Test-Path $_ } | Select-Object -First 1
$Python = @("D:\programFiles\miniconda\envs\RemoteAgent\python.exe", "python") |
    Where-Object { $_ -eq "python" -or (Test-Path $_) } | Select-Object -First 1
$Cloudflared = @("C:\Program Files (x86)\cloudflared\cloudflared.exe", "cloudflared") |
    Where-Object { $_ -eq "cloudflared" -or (Test-Path $_) } | Select-Object -First 1

if ($GatewayExe) {
    Write-Host "==> 网关形态：exe（$GatewayExe）"
} else {
    Write-Host "==> 网关形态：python -m gateway"
}

Write-Host "==> Remote Agent 隧道重连" -ForegroundColor Cyan

# 重启网关会把正在跑的任务标成中断，有任务在跑时先问一声
$health = $null
try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 } catch {}
if ($health -and $health.running_tasks -gt 0 -and -not $env:RA_NOPAUSE) {
    $ans = Read-Host "网关里有 $($health.running_tasks) 个任务在跑，重启会中断它们。继续？(y/n)"
    if ($ans -ne "y") { exit }
}

Write-Host "==> 关掉旧的隧道和网关"
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
# 按端口找网关：健康检查刚确认过 8765 上是我们的网关，这个 PID 就是它。
# exe 是 PyInstaller onefile（引导进程 + 子进程），杀父进程会带走子进程。
try {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
} catch {}
Start-Sleep 1

$url = $null
foreach ($attempt in 1..3) {
    Write-Host "==> 起新隧道（第 $attempt 次尝试）"
    Remove-Item $ErrLog -ErrorAction SilentlyContinue
    Start-Process $Cloudflared -ArgumentList "tunnel", "--url", "http://localhost:$Port" `
        -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    foreach ($i in 1..10) {
        Start-Sleep 2
        $txt = Get-Content $ErrLog -Raw -ErrorAction SilentlyContinue
        if ($txt -match "https://[a-z0-9-]+\.trycloudflare\.com") { $url = $Matches[0]; break }
    }
    if ($url) { break }
    Write-Host "    没起来，重试..."
    Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
}
if (-not $url) {
    Write-Host "隧道连续 3 次没连上（免费隧道偶尔抽风），过几分钟再双击一次。" -ForegroundColor Red
    exit 1
}
Write-Host "    新网址：$url"

Write-Host "==> 更新 config.toml"
# 配置和数据统一放在应用目录（%LOCALAPPDATA%\RemoteAgent），python / exe 读同一份
$configPath = Join-Path $env:LOCALAPPDATA "RemoteAgent\config.toml"
if (Test-Path $configPath) {
    $txt = Get-Content $configPath -Raw -Encoding UTF8
    $txt = $txt -replace "public_url\s*=\s*`"[^`"]*`"", "public_url = `"$url`""
} else {
    $txt = "[server]`npublic_url = `"$url`"`n"
}
[System.IO.File]::WriteAllText($configPath, $txt, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "    $configPath"

Write-Host "==> 重启网关（跑在新弹出的窗口里，配对码在那个窗口）"
if ($GatewayExe) {
    # 不带 --open：重连场景不需要再弹一个浏览器标签页
    Start-Process $GatewayExe -ArgumentList "serve" -WorkingDirectory (Split-Path $GatewayExe -Parent)
} else {
    Start-Process $Python -ArgumentList "-m", "gateway", "serve" -WorkingDirectory $Root
}

$ok = $false
foreach ($i in 1..15) {
    Start-Sleep 1
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($h.ok) { $ok = $true; break }
    } catch {}
}

Write-Host ""
if (-not $ok) {
    Write-Host "网关没起来？看新弹出的那个窗口里报了什么。" -ForegroundColor Red
    if (-not $env:RA_NOPAUSE) { Read-Host "按回车关闭" }
    exit 1
}

Write-Host "本机网关：已就绪" -ForegroundColor Green
try {
    Invoke-RestMethod "$url/api/health" -TimeoutSec 15 | Out-Null
    Write-Host "公网穿透：已打通" -ForegroundColor Green
} catch {
    Write-Host "公网穿透还没通（Cloudflare 生效要几秒），稍等刷新即可" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  电脑上：刷新监控台页面，右侧二维码已是新网址"
Write-Host "  手机上：重新扫码即可（旧二维码作废）"
Write-Host ""
if (-not $env:RA_NOPAUSE) { Read-Host "按回车关闭" }
