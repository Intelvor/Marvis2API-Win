# deploy.ps1 - Windows 一键启动：uskey_provider(:4607) + bridge(:4605)
#
#   .\deploy.ps1                  启动
#   .\undeploy.ps1                停止

param()

$ErrorActionPreference = 'Stop'
# 统一按 UTF-8 输出，避免中文在重定向/管道下变乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { throw '未在 PATH 中找到 python，请先安装 Python 3.11+ 并加入 PATH' }
$Py = $pyCmd.Source

if (-not (Test-Path (Join-Path $Root 'config.toml'))) {
    Copy-Item (Join-Path $Root 'config.example.toml') (Join-Path $Root 'config.toml')
    Write-Host '[deploy] 未找到 config.toml，已从 config.example.toml 复制（记得填凭证）' -ForegroundColor Yellow
}

function Stop-ServiceByPidFile([string]$pidFile) {
    if (Test-Path $pidFile) {
        $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($oldPid) {
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

function Stop-Port([int]$port) {
    try {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Start-LocalService([string]$name, [string[]]$svcArgs, [string]$logName) {
    $p = Start-Process -FilePath $Py -ArgumentList $svcArgs -WorkingDirectory $Root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Root $logName) `
        -RedirectStandardError (Join-Path $Root ($logName -replace '\.log$', '.err.log'))
    Set-Content -Path (Join-Path $Root "$name.pid") -Value $p.Id -Encoding ascii
    Write-Host ("[deploy] {0} 已启动 (pid {1})" -f $name, $p.Id)
    return $p
}

function Wait-Http([string]$url, [int]$seconds = 20) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try {
            $null = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing
            return $true
        } catch { Start-Sleep -Seconds 1 }
    }
    return $false
}

Write-Host '[deploy] 停止旧进程 ...'
Stop-ServiceByPidFile (Join-Path $Root 'provider.pid')
Stop-ServiceByPidFile (Join-Path $Root 'bridge.pid')
Stop-Port 4607
Stop-Port 4605
Start-Sleep -Seconds 1

# 1) uskey provider
$null = Start-LocalService 'provider' @('uskey_provider.py') 'provider.log'
if (-not (Wait-Http 'http://127.0.0.1:4607/health' 20)) {
    Write-Host '[deploy] provider 启动失败，日志：' -ForegroundColor Red
    Get-Content (Join-Path $Root 'provider.log') -Tail 20 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $Root 'provider.err.log') -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}
$selfTest = Invoke-RestMethod -Uri 'http://127.0.0.1:4607/uskey' -TimeoutSec 25
Write-Host ("[deploy] provider 自检: len={0} source={1}" -f $selfTest.len, $selfTest.source)

# 2) bridge
$null = Start-LocalService 'bridge' @('bridge.py') 'bridge.log'
if (-not (Wait-Http 'http://127.0.0.1:4605/health' 25)) {
    Write-Host '[deploy] bridge 启动失败，日志：' -ForegroundColor Red
    Get-Content (Join-Path $Root 'bridge.log') -Tail 20 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $Root 'bridge.err.log') -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}

$health = Invoke-RestMethod -Uri 'http://127.0.0.1:4605/health' -TimeoutSec 10
Write-Host ''
Write-Host '  OpenAI base URL : http://127.0.0.1:4605/v1'
Write-Host '  uskey provider  : http://127.0.0.1:4607/uskey'
Write-Host ("  bridge health   : uskey_source={0} uskey_live={1}" -f $health.uskey_source, $health.uskey_live)

# 监听范围与访问密钥状态：局域网暴露后没密钥等于把本机凭证借给同网段所有人
$cfgTextForCheck = Get-Content (Join-Path $Root 'config.toml') -Raw -Encoding UTF8
$listenHost = $null
if ($cfgTextForCheck -match '(?m)^\s*host\s*=\s*"([^"]*)"') { $listenHost = $Matches[1] }
$hasKey = $cfgTextForCheck -match '(?m)^\s*api_key\s*=\s*"[^"]+"'

if ($listenHost -and $listenHost -notin @('127.0.0.1', 'localhost', '::1')) {
    $lip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1 -ExpandProperty IPAddress)
    Write-Host ("  局域网监听      : {0}  (http://{1}:4605/v1)" -f $listenHost, $(if ($lip) { $lip } else { '<本机IP>' })) -ForegroundColor Yellow
    if ($hasKey) {
        Write-Host '  访问密钥        : 已启用（客户端需带 Authorization: Bearer <api_key>）' -ForegroundColor Green
    } else {
        Write-Host '  [!] host 已对外开放但 api_key 为空，bridge 会拒绝启动。请先在 config.toml 配置 api_key。' -ForegroundColor Red
    }
} else {
    Write-Host '  监听范围        : 仅本机 (127.0.0.1)'
    if ($hasKey) { Write-Host '  访问密钥        : 已启用（可改为 host = "0.0.0.0" 供局域网使用）' }
}
Write-Host ''

$cfgText = $cfgTextForCheck
if ($cfgText -match '<YOUR_') {
    Write-Host '[deploy] 提醒：config.toml 里仍有占位凭证，未替换前调用会被上游 401 拒绝。' -ForegroundColor Yellow
    Write-Host '         自动填写: .\tools\scan-token-admin.ps1   （会弹 UAC）' -ForegroundColor Yellow
    Write-Host '         手工填写: .\set-credentials.ps1 -Openid xxx -Token mv_xxx -Guid xxx -Qimei36 xxx' -ForegroundColor Yellow
}
Write-Host '验收: .\acceptance.ps1 10     停止: .\undeploy.ps1'
