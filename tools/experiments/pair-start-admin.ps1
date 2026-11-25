$ErrorActionPreference = 'Continue'
$root = 'D:\Documents\Marvis2API-Win'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$env:PYTHONIOENCODING = 'utf-8'
& $py (Join-Path $root 'tools\inject_pair.py') --start
Start-Sleep -Seconds 10
Write-Host 'pair-injected'
