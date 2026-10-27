try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
# undeploy.ps1 - 停止 provider(:4607) 与 bridge(:4605)
#
#   .\undeploy.ps1

$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Stop-Port([int]$port) {
    try {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    } catch { }
}

foreach ($name in @('bridge', 'provider')) {
    $pidFile = Join-Path $Root "$name.pid"
    if (Test-Path $pidFile) {
        $svcPid = Get-Content $pidFile | Select-Object -First 1
        if ($svcPid) {
            Stop-Process -Id ([int]$svcPid) -Force -ErrorAction SilentlyContinue
            Write-Host ("[undeploy] {0} 已停止 (pid {1})" -f $name, $svcPid)
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

Stop-Port 4605
Stop-Port 4607
Write-Host '[undeploy] 端口 4605 / 4607 已释放'
