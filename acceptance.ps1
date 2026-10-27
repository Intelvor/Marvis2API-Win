# acceptance.ps1 - Marvis Bridge 验收
#
#   .\acceptance.ps1 10
#
# 判据：
#   1. provider(:4607) 能产出非空 uskey
#   2. bridge(:4605) 连续 N 次 /v1/chat/completions 返回 200（非 4100404）
# 注意：会真实打上游 N 次（耗额度、可能触发账号风控）。N<=10 且间隔 >1s；勿压测。

param([int]$N = 10)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ErrorActionPreference = 'Stop'
$Base = 'http://127.0.0.1:4605'
$Model = 'main-auto'

function Invoke-Chat {
    param([string]$Body, [int]$TimeoutSec = 90)
    try {
        $r = Invoke-WebRequest -Uri "$Base/v1/chat/completions" -Method Post -ContentType 'application/json' `
            -Body $Body -TimeoutSec $TimeoutSec -UseBasicParsing
        return @{ Code = [int]$r.StatusCode; Body = $r.Content }
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
            return @{ Code = [int]$resp.StatusCode; Body = $sr.ReadToEnd() }
        }
        return @{ Code = 0; Body = $_.Exception.Message }
    }
}

Write-Host "== Marvis Bridge 验收 (N=$N) =="

# 1) provider 自检
try {
    $u = Invoke-RestMethod -Uri 'http://127.0.0.1:4607/uskey' -TimeoutSec 25
    if ($u.len -gt 0) { Write-Host ("[ok] provider uskey len={0} source={1}" -f $u.len, $u.source) }
    else { Write-Host '[!] provider 返回空 uskey' -ForegroundColor Yellow }
} catch {
    Write-Host ("[!] provider 不可用: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
}

# 2) 连续请求
Write-Host "[..] 连续请求 $N 次 (间隔 1.2s)"
$pass = 0
$codes = @()
$lastBody = ''
for ($i = 1; $i -le $N; $i++) {
    $body = @{
        model    = $Model
        messages = @(@{ role = 'user'; content = 'reply OK' })
        stream   = $false
    } | ConvertTo-Json -Depth 6
    $res = Invoke-Chat -Body $body
    $codes += $res.Code
    $lastBody = $res.Body
    if ($res.Code -eq 200) { $pass++ }
    Write-Host ("  [{0,2}] {1}" -f $i, $res.Code)
    Start-Sleep -Milliseconds 1200
}

$fail = $N - $pass
$lastModel = '?'
try { $lastModel = ($lastBody | ConvertFrom-Json).model } catch { }

Write-Host ''
Write-Host '== 结果 =='
Write-Host ("  200: {0} / {1}   (序列: {2})" -f $pass, $N, ($codes -join ' '))
Write-Host ("  last response model: {0}" -f $lastModel)
if ($fail -eq 0) {
    Write-Host '  VERDICT: PASS' -ForegroundColor Green
    exit 0
} else {
    Write-Host '  VERDICT: FAIL  (4100404/4100403 = 账号级风控，冷却后再试；401 = 凭证失效)' -ForegroundColor Red
    exit 1
}
