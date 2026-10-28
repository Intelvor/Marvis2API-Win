# Quick Start Guide

## Quick Start/Stop Scripts

### Windows Users (Recommended)
- **Start Service**: Double-click `start-service.bat`
- **Stop Service**: Double-click `stop-service.bat`

### PowerShell Users
- **Start Service**: `.\start.ps1`
- **Stop Service**: `.\stop.ps1`

### Manual Method
- **Start Service**: `.\deploy.ps1`
- **Stop Service**: `.\undeploy.ps1`

## Service Information

- **OpenAI Compatible API**: http://127.0.0.1:4605/v1
- **uskey provider**: http://127.0.0.1:4607/uskey
- **Current Model**: deepseek-v4-flash-ga-260731

## Verification

After starting the service, run acceptance test:
```powershell
.\acceptance.ps1 10
```

## Token Refresh

If you encounter 401 errors (token expired), run:
```powershell
.\tools\scan-token-admin.ps1
```

## Auto-start on Boot (Optional)

To auto-start the service on login, use Windows Task Scheduler:

1. Open "Task Scheduler"
2. Create a basic task
3. Trigger: "At log on"
4. Action: "Start a program"
5. Program: `powershell.exe`
6. Arguments: `-ExecutionPolicy Bypass -File "D:\Marvis2API-Win\start.ps1"`
