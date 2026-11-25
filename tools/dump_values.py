#!/usr/bin/env python3
"""dump_values.py — 在 Marvis 进程内存里按「已知值的形态」搜真实取值

dump_headers.py 按 key 搜到的是 schema 字段名（osVersion/clientVersion 定义处），
拿不到真实值。这里反过来：按值的形态直接搜，例如 UTF-16 的 "1.60." 或
"Windows-10"，命中点附近再抽可读文本。

用法（需管理员）：
  python tools/dump_values.py --name MarvisAgent.exe --find 1.60. --enc utf16
  python tools/dump_values.py --name MarvisAgent.exe --find "Windows-10" --enc utf16
  python tools/dump_values.py --name MarvisAgent.exe --find osVersion --enc utf8 --context 300
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
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
        ("PartitionId", w.WORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
    ]


def enable_debug() -> bool:
    tok = w.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(),
                                  0x0020 | 0x0008, ctypes.byref(tok)):
        return False
    try:
        luid = w.LUID()
        if not advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = (w.DWORD * 3)()
        tp[0] = 1
        tp[1] = luid.LowPart
        tp[2] = luid.HighPart
        advapi.AdjustTokenPrivileges(tok, False, ctypes.byref(
            type("TP", (ctypes.Structure,), {"_fields_": [
                ("Count", w.DWORD), ("Luid", w.LUID), ("Attr", w.DWORD)]})(
                Count=1, Luid=luid, Attr=0x00000002)), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        k32.CloseHandle(tok)


def find_pids(name: str) -> list[int]:
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="ignore").stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def readable_snippet(buf: bytes, off: int, ctx: int) -> str:
    lo = max(0, off - ctx // 2)
    hi = min(len(buf), off + ctx // 2)
    raw = buf[lo:hi]
    # 同时试 UTF-16LE 与 UTF-8，取含可打印字符更多的那个
    u16 = raw.decode("utf-16-le", errors="ignore")
    u8 = raw.decode("utf-8", errors="ignore")
    def score(s):
        return sum(1 for c in s if c.isprintable() or c in "\r\n\t")
    s = u16 if score(u16) >= score(u8) else u8
    # 只保留安全可打印的 ASCII，避免 GBK 控制台编码崩溃
    return "".join(c if (c.isprintable() and ord(c) < 0x2500) else "." for c in s)


def scan(pid: int, needle: bytes, ctx: int, limit: int) -> list[str]:
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"[!] OpenProcess pid={pid} err={ctypes.get_last_error()}", file=sys.stderr)
        return []
    hits: list[str] = []
    addr = 0
    mbi = MBI()
    try:
        while addr < MAX_ADDR and len(hits) < limit:
            if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                      ctypes.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress, mbi.RegionSize
            if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                    and not (mbi.Protect & PAGE_GUARD)):
                got = ctypes.c_size_t(0)
                buf = ctypes.create_string_buffer(CHUNK)
                off = 0
                while off < size and len(hits) < limit:
                    n = min(CHUNK, size - off)
                    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n,
                                               ctypes.byref(got))
                    if ok and got.value:
                        data = buf.raw[:got.value]
                        start = 0
                        while len(hits) < limit:
                            i = data.find(needle, start)
                            if i < 0:
                                break
                            snip = readable_snippet(data, i, ctx)
                            if snip.strip():
                                hits.append(f"hit @ 0x{base + off + i:x}\n{snip}\n")
                            start = i + len(needle)
                    off += n
            if size <= 0:
                break
            addr = base + size
    finally:
        k32.CloseHandle(h)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--find", help="要搜索的值（字面量）")
    g.add_argument("--find-hex", help="要搜索的字节（hex，绕过 shell 引号问题）")
    ap.add_argument("--enc", choices=["utf8", "utf16"], default="utf16")
    ap.add_argument("--context", type=int, default=200)
    ap.add_argument("--max", type=int, default=6)
    args = ap.parse_args()

    if not enable_debug():
        print("[!] SeDebugPrivilege 未启用（若非管理员，部分进程会读不到）", file=sys.stderr)

    needle = bytes.fromhex(args.find_hex) if args.find_hex else args.find.encode(
        "utf-16-le" if args.enc == "utf16" else "utf-8")
    label = f"hex:{args.find_hex}" if args.find_hex else repr(args.find)
    pids = find_pids(args.name)
    if not pids:
        sys.exit(f"[!] 未找到进程 {args.name}")
    print(f"[dump] {args.name} pids={pids} find={label} enc={args.enc}")

    total = 0
    for pid in pids:
        hits = scan(pid, needle, args.context, args.max)
        if hits:
            print(f"\n===== pid={pid} ({len(hits)} 处) =====")
            for h in hits:
                print(h)
            total += len(hits)
    print(f"\n[dump] 共 {total} 处命中")


if __name__ == "__main__":
    main()
