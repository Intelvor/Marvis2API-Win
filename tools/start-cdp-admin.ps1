$ErrorActionPreference = 'Continue'
Write-Host '=== 关闭 Marvis ==='
foreach ($n in @('Marvis','MarvisAgent','MarvisHost','MarvisMCP','MarvisKnowledgebase')) {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($p) { $p | Stop-Process -Force -ErrorAction SilentlyContinue; Write-Host ("  停止 {0} x{1}" -f $n,$p.Count) }
}
Start-Sleep -Seconds 3

$exe = (Get-ChildItem 'D:\Program Files\Tencent\Marvis\Application' -Filter 'Marvis.exe' -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1).FullName
if (-not $exe) { Write-Host '[!] 未找到 Marvis.exe'; exit 1 }

Write-Host "=== 带 CDP 端口启动: $exe ==="
# Electron 标准调试参数
$args = @('--remote-debugging-port=9222', '--remote-allow-origins=*')
Start-Process -FilePath $exe -ArgumentList $args
Write-Host '  已启动，等待 CDP 就绪...'

for ($i=0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 1000
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 2 -UseBasicParsing
        Write-Host ('  [ok] CDP 可用: ' + $r.Content.Substring(0, [Math]::Min(200, $r.Content.Length)))
        exit 0
    } catch { }
}
Write-Host '  [!] CDP 端口未就绪（可能被 Electron 禁用）'
