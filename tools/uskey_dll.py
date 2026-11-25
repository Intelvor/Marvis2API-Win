#!/usr/bin/env python3
"""uskey_dll.py — 直接调用腾讯 beacon_wrapper.dll 生成合法 uskey

背景（逆向 marvis_agent.pyd 得到）：
新版 Marvis 的 uskey 不再是随机串，而是由腾讯设备指纹 SDK 实时计算：
    _BeaconWrapperSDKBackend._get_uskey_with_size(busiid, bus_info, buffer_size)
        -> beacon_get_uskey(...)  [beacon_wrapper.dll]
服务端会校验 uskey 与设备指纹的一致性，随机生成的 uskey 直接触发 4100404。

本脚本加载客户端自带的 beacon_wrapper.dll，按原生调用约定取 uskey，
从而让网关发出的 uskey 与真实客户端完全一致。

用法（需与客户端同架构，无需管理员）：
  python tools/uskey_dll.py --probe                 # 探测可用性，打印 qimei36/uskey
  python tools/uskey_dll.py --busiid <id> --bus-info <json>
  python tools/uskey_dll.py --probe --dll "路径\\beacon_wrapper.dll"
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import sys
from pathlib import Path

# 客户端安装根（可从注册表 HKLM\SOFTWARE\Tencent\Marvis 读，这里给默认值兜底）
DEFAULT_DLL = Path(r"D:\Program Files\Tencent\Marvis\Application\1.60.3400.35\win32-native\beacon_wrapper.dll")

USKEY_BUF = 4096
QIMEI_BUF = 4096


def resolve_dll(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        sys.exit(f"[!] 找不到 dll: {p}")

    # 优先读注册表里的安装路径
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Marvis") as k:
            root, _ = winreg.QueryValueEx(k, "InstallPath")
            cands = sorted(Path(root).glob("Application/*/win32-native/beacon_wrapper.dll"),
                           reverse=True)
            for c in cands:
                if c.is_file():
                    return c
    except OSError:
        pass

    if DEFAULT_DLL.is_file():
        return DEFAULT_DLL
    sys.exit("[!] 未找到 beacon_wrapper.dll，请用 --dll 指定")


def load(dll_path: Path):
    # 让 dll 能找到同目录依赖
    add_dll = getattr(os, "add_dll_directory", None)
    handle = add_dll(str(dll_path.parent)) if add_dll else None
    dll = ctypes.CDLL(str(dll_path))
    return dll, handle


def setup(dll):
    """按 pyd 的 _setup_function_signatures 推断的原生签名声明。"""
    sigs = {}

    # beacon_init(...) — 内部初始化，参数不确定，先按无参/单参尝试
    for name in ("beacon_init", "beacon_get_uskey", "beacon_get_qimei"):
        try:
            sigs[name] = getattr(dll, name)
        except AttributeError:
            sigs[name] = None
    return sigs


def try_uskey(dll, busiid: str, bus_info: dict, size: int) -> tuple[int, str]:
    """尝试多种调用约定，返回 (返回码, uskey 文本)。

    pyd 里 _get_uskey_with_size 走的是：
        buffer = create_string_buffer(size)
        ret = beacon_get_uskey(busiid, bus_info_json, buffer, size)
    但 busiid/bus_info 的具体类型（c_char_p / 结构体）需实测。
    """
    bi_json = json.dumps(bus_info, ensure_ascii=False, separators=(",", ":"))
    attempts = [
        # (标签, argtypes, args)
        ("c_char_p x4",
         [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int],
         (busiid.encode(), bi_json.encode(), None, size)),
        ("buf,size (2参)",
         [ctypes.c_char_p, ctypes.c_int],
         (None, size)),
        ("busiid,bus_info,buf,size 全指针",
         [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int],
         (busiid.encode(), bi_json.encode(), None, size)),
    ]
    last = (-1, "")
    for label, argtypes, args in attempts:
        buf = ctypes.create_string_buffer(size)
        real = list(args)
        # 把 None 占位替换成 buffer
        for i, a in enumerate(real):
            if a is None:
                real[i] = ctypes.cast(buf, ctypes.c_char_p) if argtypes[i] is ctypes.c_char_p else ctypes.cast(buf, ctypes.c_void_p)
        try:
            dll.beacon_get_uskey.argtypes = argtypes
            dll.beacon_get_uskey.restype = ctypes.c_int
            ret = dll.beacon_get_uskey(*real)
            txt = buf.value.decode("utf-8", errors="replace").strip("\x00").strip()
            print(f"  [try] {label:34} ret={ret}  len={len(txt)}  value={txt[:80]!r}")
            if ret == 0 and txt:
                return ret, txt
            last = (ret, txt)
        except Exception as e:
            print(f"  [try] {label:34} EXC {type(e).__name__}: {e}")
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", help="beacon_wrapper.dll 路径")
    ap.add_argument("--busiid", default="", help="busiid（应用业务标识）")
    ap.add_argument("--bus-info", default="{}", help="bus_info JSON")
    ap.add_argument("--size", type=int, default=USKEY_BUF)
    ap.add_argument("--probe", action="store_true", help="只探测 dll 与符号")
    args = ap.parse_args()

    dll_path = resolve_dll(args.dll)
    print(f"[dll] {dll_path}")
    dll, handle = load(dll_path)
    sigs = setup(dll)

    for name, fn in sigs.items():
        print(f"[sym] {name:20} {'OK' if fn else 'MISSING'}")
    if args.probe and not args.busiid:
        # 也尝试取 qimei（不需要 busiid），验证 dll 能工作
        buf = ctypes.create_string_buffer(QIMEI_BUF)
        try:
            dll.beacon_get_qimei.argtypes = [ctypes.c_char_p, ctypes.c_int]
            dll.beacon_get_qimei.restype = ctypes.c_int
            ret = dll.beacon_get_qimei(ctypes.cast(buf, ctypes.c_char_p), QIMEI_BUF)
            print(f"[qimei] ret={ret} value={buf.value.decode('utf-8','replace')!r}")
        except Exception as e:
            print(f"[qimei] EXC {type(e).__name__}: {e}")
        return

    bus_info = json.loads(args.bus_info)
    print(f"[call] busiid={args.busiid!r} bus_info={bus_info}")
    ret, txt = try_uskey(dll, args.busiid, bus_info, args.size)
    print(f"\n[result] ret={ret} uskey={txt!r}")


if __name__ == "__main__":
    main()
