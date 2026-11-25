$ErrorActionPreference = 'Continue'
$root = 'D:\Documents\Marvis2API-Win'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$env:PYTHONIOENCODING = 'utf-8'

# 结束 MarvisAgent，让 Electron 主进程重新拉起一个干净的 Python 环境
foreach ($p in (Get-Process MarvisAgent -ErrorAction SilentlyContinue)) {
    Write-Host ("kill " + $p.Id)
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 6

# 确认新进程已起来
for ($i = 0; $i -lt 20; $i++) {
    $np = Get-Process MarvisAgent -ErrorAction SilentlyContinue
    if ($np) {
        Write-Host ("restarted pid=" + $np.Id)
        break
    }
    Start-Sleep -Seconds 2
}

Start-Sleep -Seconds 3
Write-Host 'agent-restarted'
