$ErrorActionPreference = 'Continue'
$root = 'D:\Documents\Marvis2API-Win'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$env:PYTHONIOENCODING = 'utf-8'
& $py (Join-Path $root 'tools\marvis_free.py') --inject
Start-Sleep -Seconds 6
Write-Host 'mfree-injected'
