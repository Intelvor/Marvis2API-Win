#!/usr/bin/env python3
"""dump_headers.py — 从 Marvis 进程内存里抓「真实请求头」原文

在进程内存中搜索 Ual-Access-* / MarvisExt 等关键字，把命中位置前后一段
原始字节按可打印文本吐出，用来还原客户端真正发出去的 header 组合。

用法（需管理员）：
  python tools/dump_headers.py --name MarvisAgent.exe --key Ual-Access-MarvisExt
  python tools/dump_headers.py --name Marvis.exe --key "Marvis-Protocol-Version"
  python tools/dump_headers.py --name MarvisSvr.exe --key Ual-Access --window 800
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import re
import subprocess
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
MAX_ADDR = 0x7FFFFFFEFFFF
CHUNK = 4 * 1024 * 1024


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", w.DWORD),
        ("__a1", w.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
        ("__a2", w.DWORD),
    ]


k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_ulonglong, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.restype = w.BOOL
k32.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_ulonglong, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.GetCurrentProcess.restype = w.HANDLE
k32.CloseHandle.argtypes = [w.HANDLE]


def elevate() -> bool:
    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]

    class LAA(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]

    class TP(ctypes.Structure):
        _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", LAA * 1)]

    advapi.OpenProcessToken.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID)]
    tok = w.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x28, ctypes.byref(tok)):
        return False
    try:
        luid = LUID()
        advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid))
        tp = TP()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x2
        advapi.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        k32.CloseHandle(tok)


def pids_of(name: str) -> list[int]:
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                         text=True, errors="replace").stdout
    res = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                res.append(int(parts[1]))
            except ValueError:
                pass
    return res


def printable(b: bytes) -> str:
    return "".join(chr(c) if 0x20 <= c < 0x7F else ("\n" if c in (10, 13) else ".") for c in b)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="MarvisAgent.exe")
    ap.add_argument("--key", default="Ual-Access-MarvisExt")
    ap.add_argument("--window", type=int, default=1200)
    ap.add_argument("--max", type=int, default=6, help="最多输出几处命中")
    args = ap.parse_args()

    if not elevate():
        print("[dump] 未能启用 SeDebugPrivilege（需要管理员）", file=sys.stderr)

    key = args.key.encode()
    pids = pids_of(args.name)
    if not pids:
        print(f"[dump] 未找到进程 {args.name}")
        return 2

    total = 0
    for pid in pids:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            print(f"[dump] {args.name} pid={pid} OpenProcess err={ctypes.get_last_error()}")
            continue
        print(f"[dump] {args.name} pid={pid}")
        mbi = MBI()
        addr = 0
        buf = ctypes.create_string_buffer(CHUNK)
        nread = ctypes.c_size_t()
        try:
            while addr < MAX_ADDR and total < args.max:
                if not k32.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                size = mbi.RegionSize or 0x1000
                if (mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD)
                        and (mbi.Protect & 0xFF) in READABLE):
                    off = 0
                    while off < size and total < args.max:
                        n = min(CHUNK, size - off)
                        if k32.ReadProcessMemory(h, addr + off, buf, n, ctypes.byref(nread)) and nread.value:
                            data = buf.raw[: nread.value]
                            for m in re.finditer(re.escape(key), data):
                                s = max(0, m.start() - args.window // 2)
                                e = min(len(data), m.end() + args.window // 2)
                                print(f"\n----- hit @ {hex(addr + off + m.start())} -----")
                                print(printable(data[s:e]))
                                total += 1
                                if total >= args.max:
                                    break
                        off += n
                addr += size
        finally:
            k32.CloseHandle(h)
    print(f"\n[dump] 共 {total} 处命中")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
