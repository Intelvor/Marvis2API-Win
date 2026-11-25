#!/usr/bin/env python3
"""win_focus.py — 用 Win32 API 把 Marvis 窗口强制置前并激活

CDP 的 Page.bringToFront 在 Electron 里只切换 tab 可见性，
不会抢系统焦点，导致 Input.dispatchMouseEvent 点击后
document.activeElement 仍是 BODY。

用法：python tools/win_focus.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

u32 = ctypes.WinDLL("user32", use_last_error=True)

SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


def find_window(pid: int) -> int:
    """按 PID 找可见的主窗口。"""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, lparam):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            # 过滤掉尺寸过小的辅助窗口
            r = wt.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            if (r.right - r.left) > 400 and (r.bottom - r.top) > 300:
                found.append(hwnd)
        return True

    u32.EnumWindows(cb, 0)
    return found[0] if found else 0


def main() -> None:
    import subprocess

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process Marvis -ErrorAction SilentlyContinue | "
         "Where-Object { $_.MainWindowHandle -ne 0 } | "
         "Select-Object -First 1 -ExpandProperty Id"],
        capture_output=True, text=True, errors="ignore").stdout.strip()
    if not out.isdigit():
        print("找不到 Marvis 主窗口")
        return
    pid = int(out)
    hwnd = find_window(pid)
    print(f"pid={pid} hwnd={hwnd}")
    if not hwnd:
        return

    # 强制置前：先 topmost 再取消，能绕过前台锁
    u32.ShowWindow(hwnd, SW_RESTORE)
    u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.6)
    print("已置前，前台窗口:", u32.GetForegroundWindow() == hwnd)


if __name__ == "__main__":
    main()
