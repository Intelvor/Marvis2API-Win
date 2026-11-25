#Requires -Version 5.1
<#
    Marvis Hub 停止脚本
    停止注入通道(4512)、面板(4800)，可选清除注入。
    用法：
      .\stop.ps1            只停服务
      .\stop.ps1 -Deep      同时重启 MarvisAgent（清掉 Frida 注入）
#>
param([switch]$Deep)

$ErrorActionPreference = 'Continue'

# MarvisAgent 以 SYSTEM 权限运行，普通权限杀不掉，必须提权
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Host '需要管理员权限（MarvisAgent 以 SYSTEM 运行），正在提权...' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', "`"$PSCommandPath`"")
    if ($Deep) { $argList += '-Deep' }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit
}
$Root = $PSScriptRoot

Write-Host ''
Write-Host '  Marvis Hub  停止' -ForegroundColor White
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray

function Stop-Port($p, $label) {
    $found = $false
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen `
        -ErrorAction SilentlyContinue
    foreach ($pid_ in ($conns.OwningProcess | Sort-Object -Unique)) {
        if (-not $pid_) { continue }
        try {
            $pr = Get-Process -Id $pid_ -ErrorAction Stop
            Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
            Write-Host "  OK  已停止 $label (PID $pid_, $($pr.ProcessName))" -ForegroundColor Green
            $found = $true
        } catch {}
    }
    if (-not $found) {
        Write-Host "  --  $label 未在运行" -ForegroundColor DarkGray
    }
}

Stop-Port 4800 '面板'
Stop-Port 4512 '注入通道'

if ($Deep) {
    Write-Host ''
    Write-Host '  重启 MarvisAgent 以清除注入...' -ForegroundColor Yellow
    foreach ($p in (Get-Process MarvisAgent -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Host "  OK  已结束 MarvisAgent (PID $($p.Id))" -ForegroundColor Green
    }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Get-Process MarvisAgent -ErrorAction SilentlyContinue) {
            Write-Host '  OK  MarvisAgent 已重启（注入已清除）' -ForegroundColor Green
            break
        }
    }
}

Write-Host ''
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
Write-Host ''
