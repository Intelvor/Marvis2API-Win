#!/usr/bin/env python3
"""sync_fingerprint.py — 从内存抓到的真实请求头里同步客户端指纹字段

为什么需要：os_version / client_version 不是安装包的 FileVersion（实测 exe 是
1.60.3300.540，客户端实际发送 1.60.3300.765），只能从客户端真正发出去的
Ual-Access-MarvisExt 原文里取。客户端升级后这两个值会变，不同步就会被上游
边缘层秒拒（4100404，~0.2s 返回）。

流程：
  tools/dump-headers-admin.ps1   # 提权把内存里的 header 原文写到 tools/header-dump.txt
  python tools/sync_fingerprint.py            # 解析并写回 config.toml
  python tools/sync_fingerprint.py --check    # 只比较，不改文件

只同步会随版本漂移的字段。qimei36 / guid / openid 属于账号设备标识，
不从 header 覆盖（避免抓到内部请求里的临时值）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.toml"
DUMP = ROOT / "tools" / "header-dump.txt"

# 配置键 -> MarvisExt 里的 JSON 键
SYNC_KEYS = {
    "os_version": "osVersion",
    "client_version": "clientVersion",
}

# 已知会出现的内部场景子字段，不属于稳定指纹，忽略
IGNORED = {"subScene", "uskey", "nonce", "qimei36", "guid", "isIoa", "taskType"}


def extract_exts(text: str) -> list[dict]:
    """从 dump 原文里捞出所有 MarvisExt JSON 对象（按花括号配对，容忍前后噪声）。"""
    out: list[dict] = []
    for m in re.finditer(r'\{[^{}]*"osVersion"[^{}]*\}', text):
        raw = m.group(0)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "osVersion" in obj:
            out.append(obj)
    return out


def set_key(text: str, key: str, value: str) -> str:
    pat = re.compile(r"^(\s*)" + re.escape(key) + r"\s*=.*$", re.M)
    if pat.search(text):
        return pat.sub(lambda m: f'{m.group(1)}{key} = "{value}"', text, count=1)
    return text + f'\n{key} = "{value}"\n'


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=str(DUMP))
    ap.add_argument("--config", default=str(CFG))
    ap.add_argument("--check", action="store_true", help="只比较，不写文件")
    args = ap.parse_args()

    dump = Path(args.dump)
    if not dump.exists():
        print(f"[fp] 未找到 {dump}", file=sys.stderr)
        print("[fp] 先跑：powershell -File tools\\dump-headers-admin.ps1（会弹 UAC）", file=sys.stderr)
        return 2

    exts = extract_exts(dump.read_text(encoding="utf-8", errors="replace"))
    if not exts:
        print("[fp] dump 里没解析到 MarvisExt。可能客户端未发请求——", file=sys.stderr)
        print("     先在客户端里发一句话，再重跑 dump。", file=sys.stderr)
        return 2

    print(f"[fp] 从 dump 里解析到 {len(exts)} 条 MarvisExt")

    # 取众数：长跑进程里可能残留旧版本的请求体，出现最多的才是当前生效值
    new_vals: dict[str, str] = {}
    for cfg_key, json_key in SYNC_KEYS.items():
        cnt = Counter(e[json_key] for e in exts if e.get(json_key))
        if not cnt:
            print(f"[fp] 警告：dump 里没有 {json_key}，跳过", file=sys.stderr)
            continue
        val, n = cnt.most_common(1)[0]
        new_vals[cfg_key] = val
        if len(cnt) > 1:
            others = ", ".join(f"{k}({v})" for k, v in cnt.most_common()[1:])
            print(f"[fp] {json_key} 有多个取值，取出现最多的 {val}（{n} 次）；其它：{others}",
                  file=sys.stderr)

    texts = Path(args.config).read_text(encoding="utf-8")
    try:
        import tomllib
        cur = tomllib.loads(texts)["marvis"]
    except Exception as e:
        print(f"[fp] 读取 config 失败：{e}", file=sys.stderr)
        return 2

    changed = []
    for k, v in new_vals.items():
        old = str(cur.get(k, ""))
        flag = "  一致" if old == v else f"  变更 {old} -> {v}"
        print(f"[fp] {k:<15} = {v}{flag}")
        if old != v:
            changed.append((k, v))
            texts = set_key(texts, k, v)

    if not changed:
        print("[fp] 指纹无需更新")
        return 0
    if args.check:
        print(f"[fp] --check：{len(changed)} 个字段需要更新（未写入）")
        return 1

    Path(args.config).write_text(texts, encoding="utf-8", newline="\r\n")
    print(f"[fp] 已写入 {args.config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
