@echo off
chcp 65001 >nul
echo Stopping Marvis Bridge service...
powershell -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
