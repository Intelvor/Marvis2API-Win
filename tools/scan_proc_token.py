#!/usr/bin/env python3
"""scan_proc_token.py — 在 Windows 进程内存里定位明文凭证（Marvis 登录态）

用途：Marvis 的 access_token / openid 不落明文磁盘（本地是 MSKV 加密容器），
      但构造请求头时一定在进程内存里是明文。本脚本按 PID / 进程名扫描目标进程
      的 committed 内存，抓出形如 mv_xxx / oANxxx 的候选串，并给出上下文。

用法：
  python tools/scan_proc_token.py --name Marvis.exe
  python tools/scan_proc_token.py --name MarvisSvr.exe --all
  python tools/scan_proc_token.py --pid 1234
  python tools/scan_proc_token.py --name Marvis.exe --pattern Ual-Access

注意：
  - 常驻服务 MarvisSvr.exe 跑在 session 0 / SYSTEM，普通权限读不到，需管理员终端。
  - 只读，不写入目标进程。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import re
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}  # RO/RW/WC/ER/ERW/EWC


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", w.DWORD),
        ("__alignment1", w.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
        ("__alignment2", w.DWORD),
    ]


k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_ulonglong,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.ReadProcessMemory.restype = w.BOOL
k32.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_ulonglong, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [w.HANDLE]

MAX_ADDR = 0x7FFFFFFEFFFF
CHUNK = 4 * 1024 * 1024
MEM_PRIVATE = 0x20000

# 候选字符集：token/openid 常见形态
TOKEN_RE = re.compile(rb"[A-Za-z0-9_\-\.]{20,600}")
# 部分代码路径用宽字符（UTF-16LE）保存字符串
TOKEN_RE16 = re.compile(rb"(?:[A-Za-z0-9_\-\.]\x00){20,600}")
CTX_RE = re.compile(rb"[ -~]{0,80}(?:Ual-Access|uskey|openid|access_token)[ -~]{0,160}", re.I)


def enable_debug_privilege() -> bool:
    """管理员终端默认未启用 SeDebugPrivilege；不启用则打不开 SYSTEM 进程。"""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x0002
    SE_DEBUG_NAME = "SeDebugPrivilege"

    k32.GetCurrentProcess.restype = w.HANDLE
    k32.GetCurrentProcess.argtypes = []
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.LookupPrivilegeValueW.restype = w.BOOL
    advapi.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID)]
    advapi.AdjustTokenPrivileges.restype = w.BOOL
    advapi.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.c_void_p,
                                             w.DWORD, ctypes.c_void_p, ctypes.c_void_p]

    tok = w.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(),
                                   TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(tok)):
        return False
    try:
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, SE_DEBUG_NAME, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not advapi.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None):
            return False
        return ctypes.get_last_error() == 0
    finally:
        k32.CloseHandle(tok)


def is_elevated() -> bool:
    """当前进程是否处于 High/System 完整性（管理员令牌）。"""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", w.DWORD)]

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", w.DWORD)]

    k32.GetCurrentProcess.restype = w.HANDLE
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           w.DWORD, ctypes.POINTER(w.DWORD)]

    tok = w.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):
        return False
    try:
        elev = TOKEN_ELEVATION()
        ret = w.DWORD()
        ok = advapi.GetTokenInformation(tok, 20, ctypes.byref(elev),
                                        ctypes.sizeof(elev), ctypes.byref(ret))
        return bool(ok and elev.TokenIsElevated)
    finally:
        k32.CloseHandle(tok)


def find_pids(name: str) -> list[tuple[int, str]]:
    """按映像名找进程（不依赖 psutil）：tasklist /fo csv。"""
    import subprocess

    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    hits: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                hits.append((int(parts[1]), parts[0]))
            except ValueError:
                pass
    return hits


def scan(pid: int, patterns: list[bytes], show_ctx: bool,
         deadline: float | None = None, private_only: bool = True,
         verbose: bool = False) -> list[bytes]:
    """遍历目标进程的 committed 内存并按正则找凭证。

    Marvis.exe 这类 CEF 应用的虚拟地址空间可达 100 GB 量级，全量遍历会跑上十分钟；
    凭证只落在私有堆/数据段，因此默认只扫 MEM_PRIVATE，并支持时间预算。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        err = ctypes.get_last_error()
        print(f"  [!] OpenProcess({pid}) 失败 err={err}"
              f"{' (需要管理员权限)' if err == 5 else ''}", file=sys.stderr)
        return []

    found: list[bytes] = []
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    buf = ctypes.create_string_buffer(CHUNK)
    read = ctypes.c_size_t(0)
    scanned_mb = 0.0
    regions = 0

    try:
        while addr < MAX_ADDR:
            if deadline is not None and time.time() > deadline:
                print(f"  [!] 达到时间预算，提前结束（已扫 {scanned_mb:.0f} MB / {regions} region）",
                      file=sys.stderr, flush=True)
                break
            if not k32.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            size = mbi.RegionSize or 0x1000
            readable = (
                mbi.State == MEM_COMMIT
                and not (mbi.Protect & PAGE_GUARD)
                and (mbi.Protect & 0xFF) in READABLE
                and (not private_only or mbi.Type == MEM_PRIVATE)
            )
            if readable:
                regions += 1
                off = 0
                while off < size:
                    if deadline is not None and time.time() > deadline:
                        break
                    n = min(CHUNK, size - off)
                    if k32.ReadProcessMemory(h, addr + off, buf, n, ctypes.byref(read)) and read.value:
                        data = buf.raw[: read.value]
                        scanned_mb += read.value / (1024 * 1024)
                        for pat in patterns:
                            for m in TOKEN_RE.finditer(data):
                                s = m.group(0)
                                if s.startswith(pat) and len(s) >= len(pat) + 8:
                                    found.append(s)
                            for m in TOKEN_RE16.finditer(data):
                                s = m.group(0).decode("utf-16-le", "ignore").encode()
                                if s.startswith(pat) and len(s) >= len(pat) + 8:
                                    found.append(s)
                        if show_ctx:
                            for m in CTX_RE.finditer(data):
                                found.append(b"[ctx] " + m.group(0))
                    off += n
                if verbose and regions % 500 == 0:
                    print(f"    ... {regions} region / {scanned_mb:.0f} MB", flush=True)
            addr += size
    finally:
        k32.CloseHandle(h)
    if verbose:
        print(f"  扫描完成：{regions} 个可读 region，{scanned_mb:.1f} MB", flush=True)
    return found


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Windows 进程内存凭证扫描")
    ap.add_argument("--name", default="Marvis.exe", help="进程映像名，如 Marvis.exe")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--pattern", action="append", default=None,
                    help="额外前缀，可多次；默认 mv_ / o + 27 字符 openid")
    ap.add_argument("--all", action="store_true", help="扫描全部匹配进程并输出上下文行")
    ap.add_argument("--max-seconds", type=float, default=120.0,
                    help="单进程扫描时间预算（秒），0 表示不限；默认 120")
    ap.add_argument("--include-mapped", action="store_true",
                    help="同时扫描 MEM_IMAGE/MEM_MAPPED（很慢，CEF 进程可达上百 GB）")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出扫描进度")
    args = ap.parse_args()

    patterns = [p.encode() for p in (args.pattern or ["mv_"])]

    if not enable_debug_privilege():
        print("[scan] 警告：SeDebugPrivilege 未启用（当前非管理员终端？）", file=sys.stderr)
    print(f"[scan] elevated={is_elevated()}", flush=True)

    targets: list[tuple[int, str]] = []
    if args.pid:
        targets = [(args.pid, "pid")]
    else:
        targets = find_pids(args.name)
    if not targets:
        print(f"[scan] 未找到进程 {args.name}（客户端未运行？）")
        return 2

    for pid, name in targets:
        print(f"[scan] {name} pid={pid} ...", flush=True)
        deadline = (time.time() + args.max_seconds) if args.max_seconds > 0 else None
        hits = scan(pid, patterns, args.all,
                    deadline=deadline,
                    private_only=not args.include_mapped,
                    verbose=args.verbose)
        seen: set[bytes] = set()
        uniq = []
        for h in hits:
            if h in seen:
                continue
            seen.add(h)
            uniq.append(h)

        def bucket(b: bytes) -> str:
            if b.startswith(b"mv_"):
                return "ACCESS_TOKEN"
            if b.startswith(b"GZ01"):
                return "QIMEI36"
            if len(b) in (27, 28) and b.startswith(b"o"):
                return "OPENID?"
            if b.startswith(b"[ctx]"):
                return "CTX"
            return "other"

        groups: dict[str, list[bytes]] = {}
        for h in uniq:
            groups.setdefault(bucket(h), []).append(h)

        for key in ("ACCESS_TOKEN", "OPENID?", "QIMEI36", "CTX"):
            vals = groups.pop(key, [])
            if not vals:
                continue
            print(f"  --- {key} ({len(vals)}) ---")
            for v in sorted(vals, key=len, reverse=True)[:12]:
                print("   ", v.decode("utf-8", "replace")[:600])
                if key in ("ACCESS_TOKEN", "OPENID?", "QIMEI36"):
                    # 机器可读行，供上层脚本解析
                    print("MACHINE\t%s\t%s" % (key, v.decode("utf-8", "replace")))
        rest = [v for vs in groups.values() for v in vs]
        print(f"  --- other ({len(rest)})，前 10 ---")
        for v in sorted(rest, key=len, reverse=True)[:10]:
            print("   ", v.decode("utf-8", "replace")[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
