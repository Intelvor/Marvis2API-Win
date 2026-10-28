# Quick stop Marvis Bridge service
# Usage: Double-click to run or execute in PowerShell: .\stop.ps1

cd $PSScriptRoot
.\undeploy.ps1
Write-Host ""
Write-Host "Service stopped! Press any key to close window..." -ForegroundColor Green
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
