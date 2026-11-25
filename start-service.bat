@echo off
chcp 65001 >nul
echo Starting Marvis Hub (inject + services + panel)...
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
