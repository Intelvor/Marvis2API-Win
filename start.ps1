#Requires -Version 5.1
<#
    Marvis Hub 一键启动

    把「注入 → 服务 → 面板 → 凭证初始化」串成一条流程：
      1. 自提权（Frida 注入需要管理员）
      2. 检查 Python 与 Marvis 安装
      3. 保证客户端带 CDP 启动（必要时重启）
      4. 启动注入通道 marvis_auto.py（4512）
      5. 启动面板 marvis_hub.py（4800）
      6. 凭证未就绪时自动 CDP 初始化
      7. 打开面板

    用法：
      .\start.ps1              正常启动（已运行的部分会跳过）
      .\start.ps1 -Restart     强制重启客户端与服务
      .\start.ps1 -NoUI        不自动打开浏览器
      .\stop.ps1               停止服务
#>
param(
    [switch]$Restart,
    [switch]$NoUI
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'

# ---------------------------------------------------------------- 自提权
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Host '需要管理员权限（Frida 注入），正在提权...' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', "`"$PSCommandPath`"")
    if ($Restart) { $argList += '-Restart' }
    if ($NoUI)    { $argList += '-NoUI' }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit
}

# ---------------------------------------------------------------- 工具函数
function Step($n, $msg) {
    Write-Host ''
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}
function Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    !   $msg" -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "    X   $msg" -ForegroundColor Red }
function Info($msg) { Write-Host "        $msg" -ForegroundColor DarkGray }

function Pause-Exit($code = 0) {
    Write-Host ''
    Write-Host '  按任意键关闭窗口...' -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') }
    catch { $null = Read-Host }
    exit $code
}

function Test-Port($p) {
    try {
        return (Test-NetConnection -ComputerName '127.0.0.1' -Port $p `
            -InformationLevel Quiet -WarningAction SilentlyContinue)
    } catch {
        $c = New-Object Net.Sockets.TcpClient
        try {
            $c.Connect('127.0.0.1', $p); $c.Close(); return $true
        } catch { return $false }
    }
}

function Wait-Port($p, $seconds, $label) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-Port $p) { return $true }
        Start-Sleep -Seconds 1
        if ($i -gt 0 -and $i % 5 -eq 0) {
            Write-Host "        等待 $label ... ${i}s" -ForegroundColor DarkGray
        }
    }
    return $false
}

function Get-Py {
    $p = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $p) { $p = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    return $p
}

Write-Host ''
Write-Host '  Marvis Hub  一键启动' -ForegroundColor White
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray

# ---------------------------------------------------------------- 1. 环境
Step 1 '检查环境'

$Py = Get-Py
if (-not $Py) { Bad '未找到 Python'; Pause-Exit 1 }
Ok "Python $Py"

$MarvisDir = 'D:\Program Files\Tencent\Marvis\Application'
$MarvisExe = $null
if (Test-Path $MarvisDir) {
    $MarvisExe = (Get-ChildItem $MarvisDir -Filter 'Marvis.exe' -Recurse `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $MarvisExe) { Bad '未找到 Marvis.exe'; Pause-Exit 1 }
Ok "客户端 $MarvisExe"

if (-not (Test-Path (Join-Path $Root 'config.toml'))) {
    Bad '缺少 config.toml'; Pause-Exit 1
}
Ok '配置文件就位'

# ---------------------------------------------------------------- 2. 客户端
Step 2 '准备 Marvis 客户端（CDP 9222）'

$agentProc = Get-Process MarvisAgent -ErrorAction SilentlyContinue
$marvisProc = Get-Process Marvis -ErrorAction SilentlyContinue
$cdpUp = Test-Port 9222

if ($Restart) {
    Info '按参数要求强制重启'
    foreach ($n in @('Marvis', 'MarvisAgent', 'MarvisHost', 'MarvisMCP',
                     'MarvisKnowledgebase')) {
        Get-Process -Name $n -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 4
    $marvisProc = $null
    $agentProc = $null
    $cdpUp = $false
}

if ($marvisProc -and -not $cdpUp) {
    Warn '客户端在运行但未开 CDP，需要重启才能自动续期凭证'
    Info '正在重启客户端...'
    foreach ($n in @('Marvis', 'MarvisAgent', 'MarvisHost', 'MarvisMCP',
                     'MarvisKnowledgebase')) {
        Get-Process -Name $n -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 4
    $marvisProc = $null
    $agentProc = $null
}

if (-not $marvisProc) {
    Info "启动客户端（带 --remote-debugging-port=9222）"
    Start-Process -FilePath $MarvisExe `
        -ArgumentList '--remote-debugging-port=9222', '--remote-allow-origins=*'
    if (Wait-Port 9222 40 'CDP 端口') { Ok '客户端已启动，CDP 就绪' }
    else { Warn 'CDP 端口未就绪，自动续期将不可用（不影响对话）' }
    # 等 Agent 进程起来
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $agentProc = Get-Process MarvisAgent -ErrorAction SilentlyContinue
        if ($agentProc) { break }
    }
}
else {
    Ok "客户端已在运行（PID $($marvisProc.Id -join ','))"
}

if ($agentProc) {
    Ok "MarvisAgent 就绪（PID $($agentProc.Id -join ',')）"
} else {
    Bad 'MarvisAgent 未启动，注入会失败'
    Pause-Exit 1
}

# ---------------------------------------------------------------- 3. 注入通道
Step 3 '启动注入通道（端口 4512）'

if ($Restart) {
    foreach ($p in @(4512, 4800)) {
        foreach ($pid_ in (Get-NetTCPConnection -LocalPort $p -State Listen `
                -ErrorAction SilentlyContinue).OwningProcess) {
            Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

if (Test-Port 4512) {
    # 端口在监听，但可能是「上一次注入的残留」——那种情况下
    # 注入代码是旧的，必须重启 MarvisAgent 才能加载新代码。
    $owner = (Get-NetTCPConnection -LocalPort 4512 -State Listen `
        -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
    $ownerName = ''
    if ($owner) {
        try { $ownerName = (Get-Process -Id $owner).ProcessName } catch {}
    }
    if ($ownerName -ieq 'MarvisAgent') {
        Warn "4512 被 MarvisAgent(PID $owner) 占用，判定为上轮注入残留"
        Info '重启 MarvisAgent 以清除旧注入并加载当前代码...'
        foreach ($p in (Get-Process MarvisAgent -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Seconds 1
            if (Get-Process MarvisAgent -ErrorAction SilentlyContinue) { break }
        }
        Start-Sleep -Seconds 2
        $agentProc = Get-Process MarvisAgent -ErrorAction SilentlyContinue
        if (-not $agentProc) {
            Bad 'MarvisAgent 未能重启，请手动重启客户端'
            Pause-Exit 1
        }
        Ok "MarvisAgent 已重启（PID $($agentProc.Id -join ',')）"
    } else {
        Ok "通道已在运行（$ownerName），跳过"
    }
}

if (-not (Test-Port 4512)) {
    Info '注入 MarvisAgent 并启动服务...'
    $out = Join-Path $Root 'tools\auto.serve.log'
    $err = Join-Path $Root 'tools\auto.serve.err.log'
    Start-Process -FilePath $Py `
        -ArgumentList (Join-Path $Root 'tools\marvis_auto.py'), '--start' `
        -WorkingDirectory $Root -RedirectStandardOutput $out `
        -RedirectStandardError $err -WindowStyle Hidden
    if (Wait-Port 4512 30 '注入通道') { Ok '通道就绪 127.0.0.1:4512' }
    else {
        Bad '通道启动失败'
        if (Test-Path $err) { Get-Content $err -Tail 8 | ForEach-Object { Info $_ } }
        Pause-Exit 1
    }
}

# ---------------------------------------------------------------- 4. 面板
Step 4 '启动面板（端口 4800）'

if (Test-Port 4800) {
    Ok '面板已在运行，跳过'
} else {
    Info '启动 marvis_hub.py...'
    $out = Join-Path $Root 'tools\hub.log'
    Start-Process -FilePath $Py `
        -ArgumentList (Join-Path $Root 'tools\marvis_hub.py') `
        -WorkingDirectory $Root -RedirectStandardOutput $out `
        -RedirectStandardError (Join-Path $Root 'tools\hub.err.log') `
        -WindowStyle Hidden
    if (Wait-Port 4800 20 '面板') { Ok '面板就绪 http://127.0.0.1:4800' }
    else {
        Bad '面板启动失败'
        if (Test-Path $out) { Get-Content $out -Tail 8 | ForEach-Object { Info $_ } }
    }
}

# ---------------------------------------------------------------- 5. 凭证
Step 5 '检查凭证'

$ready = $false
try {
    $st = Invoke-RestMethod -Uri 'http://127.0.0.1:4512/' -TimeoutSec 6
    $ready = [bool]$st.ready
} catch {
    Warn '无法读取通道状态'
}

if ($ready) {
    Ok "凭证已就绪（$($st.rid)）"
} else {
    Warn '凭证未初始化，正在用 CDP 自动触发一次...'
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:4800/api/ping' `
            -Method Post -TimeoutSec 60
        if ($r.ok) {
            Info '已让客户端发出一条消息，等待凭证捕获...'
            for ($i = 0; $i -lt 20; $i++) {
                Start-Sleep -Seconds 1
                try {
                    $st2 = Invoke-RestMethod -Uri 'http://127.0.0.1:4512/' -TimeoutSec 5
                    if ($st2.ready) { $ready = $true; break }
                } catch {}
            }
            if ($ready) { Ok "凭证已捕获（$($st2.rid)）" }
            else { Warn '仍未捕获，请在客户端手动发一条消息' }
        } else {
            Warn "自动触发失败：$($r.result)"
            Info '请在 Marvis 客户端手动发一条消息'
        }
    } catch {
        Warn '自动触发失败，请在客户端手动发一条消息'
    }
}

# ---------------------------------------------------------------- 6. 汇总
Step 6 '就绪'

$ok4512 = Test-Port 4512
$ok4800 = Test-Port 4800
$ok9222 = Test-Port 9222

Write-Host ''
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
$s = if ($ok4512) { 'OK' } else { '--' }
Write-Host "  注入通道  127.0.0.1:4512   $s"
$s = if ($ok4800) { 'OK' } else { '--' }
Write-Host "  监控面板  http://127.0.0.1:4800   $s"
$s = if ($ok9222) { 'OK' } else { '--' }
Write-Host "  CDP 调试  127.0.0.1:9222   $s"
$s = if ($ready) { 'OK' } else { '待初始化' }
Write-Host "  凭证状态  $s"
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  OpenAI 接口：' -ForegroundColor DarkGray
Write-Host '    http://127.0.0.1:4800/v1/chat/completions' -ForegroundColor White
Write-Host ''
Write-Host '  余额：' -ForegroundColor DarkGray
Write-Host '    http://127.0.0.1:4800/v3/marvis_get_wallet' -ForegroundColor White
Write-Host ''

if (-not $NoUI -and $ok4800) {
    try { Start-Process 'http://127.0.0.1:4800' } catch {}
}

if (-not $ok4512 -or -not $ok4800) {
    Warn '有服务未就绪，请查看 tools\ 下的日志'
    Pause-Exit 0
}
