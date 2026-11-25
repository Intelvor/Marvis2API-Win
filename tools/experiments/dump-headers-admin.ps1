$ErrorActionPreference = 'Stop'
$root = 'D:\Documents\Marvis2API-Win'
$out  = Join-Path $root 'tools\header-dump.txt'
Remove-Item $out -ErrorAction SilentlyContinue

$py = (Get-Command python -ErrorAction SilentlyContinue).Source

# 抓两处：MarvisAgent（携带 MarvisExt 原文）+ MarvisSvr（token 宿主）
$parts = @(
    ('"{0}" "{1}" --name MarvisAgent.exe --key Ual-Access-MarvisExt --window 2000 --max 3' -f $py, (Join-Path $root 'tools\dump_headers.py')),
    ('"{0}" "{1}" --name MarvisSvr.exe --key MarvisExt --window 2000 --max 3' -f $py, (Join-Path $root 'tools\dump_headers.py'))
)
$inner = 'cd /d "{0}" && ({1} & echo dump-done) > "{2}" 2>&1' -f $root, ($parts -join ' & '), $out

Write-Host '[dump] 提权抓取真实请求头 ...'
$p = Start-Process -FilePath 'cmd.exe' -Verb RunAs -PassThru -ArgumentList '/c', $inner
$p.WaitForExit(600000) | Out-Null
if (Test-Path $out) {
    Write-Host ("[dump] 已写入 $out")
} else {
    Write-Host '[dump] 未生成结果（UAC 取消？）'
}
