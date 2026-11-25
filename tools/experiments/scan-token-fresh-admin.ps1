$ErrorActionPreference = 'Continue'
$root = 'D:\Documents\Marvis2API-Win'
$out  = Join-Path $root 'tools\token-fresh.txt'
Remove-Item $out -ErrorAction SilentlyContinue

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$env:PYTHONIOENCODING = 'utf-8'

$lines = @()

# mv_ 是 access_token 前缀；扫所有 Marvis 进程里出现的 mv_ 串（UTF-16 与 UTF-8 都扫）
foreach ($proc in @('MarvisSvr.exe', 'Marvis.exe', 'MarvisAgent.exe', 'MarvisHost.exe')) {
    $lines += "===== $proc :: mv_ (utf16) ====="
    $lines += & $py (Join-Path $root 'tools\dump_values.py') --name $proc --find 'mv_' --enc utf16 --context 120 --max 8 2>&1
    $lines += ''
    $lines += "===== $proc :: mv_ (utf8) ====="
    $lines += & $py (Join-Path $root 'tools\dump_values.py') --name $proc --find 'mv_' --enc utf8 --context 120 --max 8 2>&1
    $lines += ''
}

$lines | Out-File -FilePath $out -Encoding utf8
Write-Host "[dump] 写入 $out"
