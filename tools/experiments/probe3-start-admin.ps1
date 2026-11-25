$ErrorActionPreference = 'Continue'
$root = 'D:\Documents\Marvis2API-Win'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$env:PYTHONIOENCODING = 'utf-8'
& $py (Join-Path $root 'tools\inject_probe3.py') --start
Start-Sleep -Seconds 8
Write-Host 'probe3-injected'
