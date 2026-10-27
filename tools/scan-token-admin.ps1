# scan-token-admin.ps1 - 提权取 Marvis 登录态并写入 config.toml（一条命令收尾）
#
#   .\tools\scan-token-admin.ps1              # 扫描 -> 取 token -> 写 config.toml -> 重启 -> 验证
#   .\tools\scan-token-admin.ps1 -NoApply     # 只扫描，不改配置
#   .\tools\scan-token-admin.ps1 -AllProcs    # 连 GUI 进程一起扫（Marvis.exe 虚拟空间 100GB+，很慢）
#
# 为什么需要提权：Marvis 各进程以提升后的完整性级别运行，普通权限 OpenProcess 返回 5；
# MarvisSvr.exe 更是 session 0 的 SYSTEM 服务。扫描器内部会启用 SeDebugPrivilege。
# 只提权「扫描」这一步（用 cmd 包一层以便重定向输出），解析与写配置仍在当前身份下完成。
# 明细落在 tools\token-scan.txt；单进程默认 120 秒预算，超时会把已扫到的结果吐出。

param([switch]$NoApply, [switch]$AllProcs, [string]$MarvisDir, [string]$ProcessName)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$out  = Join-Path $here 'token-scan.txt'
$py   = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host '未找到 python（需在 PATH 中）' -ForegroundColor Red; exit 1 }

$scanner = Join-Path $here 'scan_proc_token.py'

# 默认只扫真正持有登录态的进程：MarvisSvr（SYSTEM 服务，虚拟空间 ~4GB，秒级完成）
# 不同版本可能改名，所以显式找不到时自动发现其它 Marvis* 进程（仍排除 CEF 的 Marvis.exe）
$procs = @()
if ($ProcessName) {
    $procs = @($ProcessName)
} else {
    $procs = @('MarvisSvr.exe')
    $running = @(Get-Process -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
    if ($running -notcontains 'MarvisSvr') {
        $alts = @($running | Where-Object { $_ -like 'Marvis*' -and $_ -ne 'Marvis' } | ForEach-Object { "$_.exe" } | Sort-Object -Unique)
        if ($alts.Count -gt 0) {
            Write-Host ('[scan] 未发现 MarvisSvr.exe，自动改用：' + ($alts -join ', ')) -ForegroundColor Yellow
            $procs = $alts
        }
    }
}
if ($AllProcs) {
    $procs += @('Marvis.exe', 'MarvisHost.exe', 'MarvisAgent.exe')
}
$procs = @($procs | Sort-Object -Unique)

# ---- 提权扫描 ----
Remove-Item $out -ErrorAction SilentlyContinue
$parts = foreach ($n in $procs) {
    'python "{0}" --name {1} --all --max-seconds 120 --pattern mv_ --pattern GZ01' -f $scanner, $n
}
$inner = 'cd /d "{0}" && ({1} & echo scan-done) > "{2}" 2>&1' -f $root, ($parts -join ' & '), $out
Write-Host ('[scan] 目标进程: ' + ($procs -join ', '))
Write-Host '[scan] 读取进程内存需要管理员权限，正在提权 ...'

try {
    $p = Start-Process -FilePath 'cmd.exe' -Verb RunAs -PassThru -ArgumentList '/c', $inner
    $p.WaitForExit(600000) | Out-Null
} catch {
    Write-Host ('[scan] 提权失败：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $out)) {
    Write-Host '[scan] 未生成结果文件（UAC 被取消？）' -ForegroundColor Yellow
    exit 1
}
$lines = Get-Content $out -Encoding UTF8
Write-Host "[scan] 已写入 $out ($($lines.Count) 行)"

# ---- 解析候选 ----
$tokens = @()
foreach ($l in $lines) {
    if ($l -match '^MACHINE\tACCESS_TOKEN\t(\S+)') { $tokens += $Matches[1] }
}
$tokens = @($tokens | Sort-Object -Unique)

Write-Host ''
if ($tokens.Count -eq 0) {
    Write-Host '[scan] 未找到 mv_ 开头的 access_token。' -ForegroundColor Yellow
    Write-Host '       常见原因：客户端未登录 / token 尚未进入内存（先在客户端里发一句话再重跑）。'
    Write-Host "       原始明细：$out"
    exit 1
}
Write-Host ("[scan] 找到 {0} 个 token 候选：" -f $tokens.Count)
foreach ($t in $tokens) {
    $masked = $t.Substring(0, [Math]::Min(10, $t.Length)) + '...' + $t.Substring([Math]::Max(0, $t.Length - 4))
    Write-Host ("   len={0} {1}" -f $t.Length, $masked)
}

if ($NoApply) { Write-Host '[scan] -NoApply，不修改配置'; exit 0 }

$token = $tokens[0]
Write-Host ("[scan] 采用最长候选（len={0}）" -f $token.Length)

# openid / guid / qimei36 由 python 侧自动发现（目录名 + beacon 库），比命令行传参稳
# --marvis-dir 仅在数据目录非标准位置时需要（文件夹重定向 / 便携版 / 多用户）
$credsArgs = @((Join-Path $here 'apply_creds.py'), '--auto', '--token', $token)
if ($MarvisDir) { $credsArgs += @('--marvis-dir', $MarvisDir) }
& $py @credsArgs
if ($LASTEXITCODE -ne 0) { Write-Host '[scan] 写入 config.toml 失败' -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host '[scan] 重启服务 ...'
$logDir = Join-Path $env:TEMP 'marvis-restart'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$uLog = Join-Path $logDir 'undeploy.log'
$dLog = Join-Path $logDir 'deploy.log'

# 关键：不能用 Start-Process -Wait 跑 deploy.ps1 —— PS 的 -Wait 会连带等待它拉起的
# provider/bridge 子进程，而那两个是常驻的，于是永远等不到退出（表现为“卡死”）。
# 也不能用 `| Out-Null` 接管道。改用 cmd /c 自行重定向，之后靠端口就绪来判断。
foreach ($step in @(@('undeploy.ps1', $uLog), @('deploy.ps1', $dLog))) {
    Write-Host ("   -> {0}" -f $step[0])
    cmd /c ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" > "{1}" 2>&1' -f `
        (Join-Path $root $step[0]), $step[1])
    Write-Host ("      退出码 {0}（日志 {1}）" -f $LASTEXITCODE, $step[1])
}

# 等端口真正监听，最多 90 秒
$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    $listen = @(Get-NetTCPConnection -LocalPort 4605, 4607 -State Listen -ErrorAction SilentlyContinue)
    if ($listen.Count -ge 2) { $ready = $true; break }
    Start-Sleep -Milliseconds 2000
}
if ($ready) { Write-Host '      4605 / 4607 已监听' }
else { Write-Host '   [!] 端口未就绪，检查 deploy 日志' -ForegroundColor Yellow }

Get-Content $dLog -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 6 | ForEach-Object { Write-Host "      $_" }

Write-Host '[scan] 向上游验证一次 ...'
$verifyArgs = @((Join-Path $here 'apply_creds.py'), '--auto', '--token', $token, '--test')
if ($MarvisDir) { $verifyArgs += @('--marvis-dir', $MarvisDir) }
& $py @verifyArgs
