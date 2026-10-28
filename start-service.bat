@echo off
chcp 65001 >nul
echo Starting Marvis Bridge service...
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
