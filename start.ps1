# Quick start Marvis Bridge service
# Usage: Double-click to run or execute in PowerShell: .\start.ps1

cd $PSScriptRoot
.\deploy.ps1
Write-Host ""
Write-Host "Service started! Press any key to close window..." -ForegroundColor Green
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
