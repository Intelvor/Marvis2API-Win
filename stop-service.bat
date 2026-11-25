@echo off
chcp 65001 >nul
echo Stopping Marvis Hub services...
powershell -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
