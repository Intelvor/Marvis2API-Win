$ErrorActionPreference = 'Continue'
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'

Write-Host '=== 还原系统代理为 127.0.0.1:7897 ==='
Set-ItemProperty -Path $key -Name 'ProxyEnable' -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name 'ProxyServer' -Value '127.0.0.1:7897' -Type String
Set-ItemProperty -Path $key -Name 'ProxyOverride' -Value 'localhost;127.*;192.168.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;172.31.*;*.local;<local>' -Type String

$sig = @'
using System;
using System.Runtime.InteropServices;
public class WinInetR {
    [DllImport("wininet.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern bool InternetSetOption(IntPtr h, int o, IntPtr b, int l);
    public static void Refresh() {
        InternetSetOption(IntPtr.Zero, 39, IntPtr.Zero, 0);
        InternetSetOption(IntPtr.Zero, 37, IntPtr.Zero, 0);
    }
}
'@
try { Add-Type -TypeDefinition $sig -ErrorAction Stop; [WinInetR]::Refresh(); Write-Host '  [ok] 已刷新' }
catch { Write-Host ('  [!] ' + $_.Exception.Message) }

# 清掉用户级 *_PROXY（之前为抓包加的）
foreach ($n in 'HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','NO_PROXY','no_proxy') {
    if ([Environment]::GetEnvironmentVariable($n,'User')) {
        [Environment]::SetEnvironmentVariable($n, $null, 'User')
        Write-Host "  已清除 $n"
    }
}

Write-Host ''
Get-ItemProperty -Path $key | Select-Object ProxyEnable, ProxyServer | Format-List | Out-String

# 移除抓包 CA（安全收尾）
Write-Host '=== 移除 mitmproxy 根证书 ==='
$cn = Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like '*mitmproxy*' }
foreach ($c in $cn) {
    try { Remove-Item -Path ("Cert:\LocalMachine\Root\" + $c.Thumbprint) -Force -ErrorAction Stop; Write-Host '  已移除 ' + $c.Thumbprint }
    catch { Write-Host ('  移除失败: ' + $_.Exception.Message) }
}
$cu = Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Subject -like '*mitmproxy*' }
foreach ($c in $cu) {
    try { Remove-Item -Path ("Cert:\CurrentUser\Root\" + $c.Thumbprint) -Force -ErrorAction Stop; Write-Host '  已移除(用户) ' + $c.Thumbprint }
    catch { Write-Host ('  移除失败(用户): ' + $_.Exception.Message) }
}
