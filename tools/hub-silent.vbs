' 后台静默启动 Marvis Hub，不占终端
Set sh = CreateObject("WScript.Shell")
wd = "D:\Documents\Marvis2API-Win"
sh.CurrentDirectory = wd

cmd = "cmd /c cd /d """ & wd & """ && python tools\marvis_hub.py > tools\hub.log 2>&1"
sh.Run cmd, 0, False
