# set-credentials.ps1 - 把凭证写进 config.toml，并可选做一次连通性验证
#
#   .\set-credentials.ps1 -Openid o_xxx -Token mv_xxx -Guid xxxxxxxxxxxx -Qimei36 40位指纹
#   .\set-credentials.ps1 ... -Test        # 写入后立刻打一次 /v1/chat/completions
#
# 凭证来源：已登录 Marvis 客户端的请求头（或直接用 tools\scan-token-admin.ps1 自动取）
#   openid        <- Ual-Access-Openid
#   access_token  <- Ual-Access-Access-Token（mv_ 开头）
#   device_guid   <- Ual-Access-Guid / X-Device-GUID（客户端实例 ID）
#   qimei36       <- Ual-Access-MarvisExt 里 JSON 的 "qimei36"（40 位设备指纹，与 guid 不同！）
#
# 注意：qimei36 与 device_guid 是两个不同的设备标识。两者都填错会导致上游 4100404 风控。

param(
    [Parameter(Mandatory = $true)][string]$Openid,
    [Parameter(Mandatory = $true)][string]$Token,
    [Parameter(Mandatory = $true)][string]$Guid,
    [string]$Qimei36,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$cfgPath = Join-Path $Root 'config.toml'
if (-not (Test-Path $cfgPath)) { throw 'config.toml 不存在，先运行 .\deploy.ps1 生成' }

if ($Token -notmatch '^mv_') {
    Write-Host '[warn] access_token 通常以 mv_ 开头，请确认抓的是 Ual-Access-Access-Token' -ForegroundColor Yellow
}

function Set-TomlKey([string[]]$lines, [string]$key, [string]$value) {
    $out = New-Object System.Collections.Generic.List[string]
    $done = $false
    $pattern = '^\s*' + [regex]::Escape($key) + '\s*='
    foreach ($line in $lines) {
        if (-not $done -and $line -match $pattern) {
            $out.Add(('{0} = "{1}"' -f $key, $value))
            $done = $true
        } else {
            $out.Add($line)
        }
    }
    if (-not $done) { $out.Add(('{0} = "{1}"' -f $key, $value)) }
    return $out.ToArray()
}

# 必须显式 -Encoding UTF8：PS 5.1 默认按 ANSI(GBK) 读取，会把中文注释读坏并吃掉换行
$lines = Get-Content $cfgPath -Encoding UTF8
$lines = Set-TomlKey $lines 'openid' $Openid
$lines = Set-TomlKey $lines 'access_token' $Token
$lines = Set-TomlKey $lines 'device_guid' $Guid
if ($Qimei36) { $lines = Set-TomlKey $lines 'qimei36' $Qimei36 }

# 写回也要 UTF-8 无 BOM：tomllib 不接受 BOM 前缀
[System.IO.File]::WriteAllText($cfgPath, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding($false)))

$oidHead = $Openid.Substring(0, [Math]::Min(6, $Openid.Length))
$tokTail = $Token.Substring([Math]::Max(0, $Token.Length - 4))
Write-Host ("[creds] 已写入 config.toml: openid={0}... token=...{1} guid={2} qimei36={3}" -f $oidHead, $tokTail, $Guid, $(if ($Qimei36) { $Qimei36.Substring(0, 6) + '...' } else { '(未填)' }))
Write-Host '[creds] 重启服务后生效: .\undeploy.ps1 ; .\deploy.ps1'

if ($Test) {
    $body = @{
        model      = 'main-auto'
        messages   = @(@{ role = 'user'; content = 'ping' })
        stream     = $false
        max_tokens = 16
    } | ConvertTo-Json -Depth 6

    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:4605/v1/chat/completions' -Method Post `
            -ContentType 'application/json' -Body $body -TimeoutSec 120 -UseBasicParsing
        Write-Host ("[test] HTTP {0}" -f $r.StatusCode) -ForegroundColor Green
        Write-Host $r.Content.Substring(0, [Math]::Min(400, $r.Content.Length))
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
            Write-Host ("[test] HTTP {0}" -f [int]$resp.StatusCode) -ForegroundColor Red
            Write-Host $sr.ReadToEnd()
        } else {
            Write-Host ("[test] 请求失败: {0}" -f $_.Exception.Message) -ForegroundColor Red
        }
    }
}
