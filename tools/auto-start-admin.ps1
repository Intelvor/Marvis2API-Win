$ErrorActionPreference = 'Continue'
$root = 'D:\Documents\Marvis2API-Win'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$env:PYTHONIOENCODING = 'utf-8'
$out = Join-Path $root 'tools\auto.serve.log'
$err = Join-Path $root 'tools\auto.serve.err.log'
Start-Process -FilePath $py -ArgumentList (Join-Path $root 'tools\marvis_auto.py'), '--start' `
    -WorkingDirectory $root -RedirectStandardOutput $out -RedirectStandardError $err `
    -WindowStyle Hidden
Start-Sleep -Seconds 10
Write-Host 'auto-started'
