#!/usr/bin/env python3
"""apply_creds.py — 把凭证写入 config.toml（UTF-8 无 BOM，tomllib 可直接解析）

用法：
  python tools/apply_creds.py --openid oAN... --token mv_xxx --guid cd27...
  python tools/apply_creds.py --auto --token mv_xxx           # openid/guid 自动发现
  python tools/apply_creds.py --auto --token mv_xxx --test    # 写完立刻打一次上游
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.toml"


def candidate_dirs() -> list[Path]:
    """Marvis 用户数据目录的候选位置。

    注意：这里找的是**用户数据目录**（openid / db / 登录态），不是安装目录。
    数据目录跟着 Windows 用户走，与客户端装在哪个盘无关；
    但企业环境的文件夹重定向、便携版、以及 UWP 封装都可能把它搬到别处，
    所以这里探测多个候选，并允许用 --marvis-dir / MARVIS_DATA_DIR 显式指定。
    """
    out: list[Path] = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / "Tencent" / "Marvis")
    return out


def resolve_marvis_dir(explicit: str | None = None) -> Path | None:
    """返回可用的 Marvis 数据目录；找不到返回 None。"""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    env = os.environ.get("MARVIS_DATA_DIR")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p
    for c in candidate_dirs():
        # 判定标准：至少要有 User/ 或 db/ 之一，否则不是真正在用过的目录
        if c.is_dir() and ((c / "User").is_dir() or (c / "db").is_dir()):
            return c
    return None


def discover_openid(mdir: Path) -> str | None:
    """登录用户目录名即 openid（.../Marvis/User/<openid>/）。

    一台机器登录过多个账号时会有多个候选，按目录最后修改时间取最近登录的那个
    （iterdir 的顺序不保证，直接取第一个可能拿到已废弃的旧账号）。"""
    userdir = mdir / "User"
    if not userdir.is_dir():
        return None
    cands = [
        p for p in userdir.iterdir()
        if p.is_dir() and p.name not in ("default_user",)
        and re.fullmatch(r"o[A-Za-z0-9_\-]{10,}", p.name)
    ]
    if not cands:
        return None
    if len(cands) > 1:
        cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        others = ", ".join(p.name for p in cands[1:])
        print(f"[creds] 发现 {len(cands)} 个登录账号，取最近登录的 {cands[0].name}"
              f"（其它：{others}，可用 --openid 指定）", file=sys.stderr)
    return cands[0].name


def discover_guid(mdir: Path) -> str | None:
    """从 beacon 事件库里取设备 GUID（A3 / A50 字段）。"""
    dbdir = mdir / "db"
    if not dbdir.is_dir():
        return None
    for db in sorted(dbdir.glob("bc_*.db")):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = c.execute(
                "select event_content from beacon_events "
                "where event_content like '%\"A3\"%' order by id desc limit 1"
            ).fetchone()
            c.close()
        except sqlite3.Error:
            continue
        if not row:
            continue
        m = re.search(rb'"A3"\s*:\s*"([0-9a-f]{16,64})"', row[0] or b"")
        if m:
            return m.group(1).decode()
    return None


def discover_qimei36(mdir: Path) -> str | None:
    """Qimei36 是独立于 guid 的 40 位设备指纹，落在 beacon 事件的 A153 字段。"""
    dbdir = mdir / "db"
    if not dbdir.is_dir():
        return None
    for db in sorted(dbdir.glob("bc_*.db")):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = c.execute(
                "select event_content from beacon_events "
                "where event_content like '%\"A153\"%' order by id desc limit 1"
            ).fetchone()
            c.close()
        except sqlite3.Error:
            continue
        if not row:
            continue
        m = re.search(rb'"A153"\s*:\s*"([0-9a-fA-F]{32,48})"', row[0] or b"")
        if m:
            return m.group(1).decode()
    return None


def set_key(text: str, key: str, value: str) -> str:
    pattern = re.compile(r"^(\s*)" + re.escape(key) + r"\s*=.*$", re.M)
    if pattern.search(text):
        return pattern.sub(lambda m: f'{m.group(1)}{key} = "{value}"', text, count=1)
    return text + f'\n{key} = "{value}"\n'


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--openid")
    ap.add_argument("--token", required=True, help="mv_ 开头的 access_token")
    ap.add_argument("--guid")
    ap.add_argument("--qimei36")
    ap.add_argument("--marvis-dir", help="Marvis 用户数据目录（默认自动探测 %%APPDATA%%\\Tencent\\Marvis）")
    ap.add_argument("--auto", action="store_true", help="自动发现 openid / device_guid / qimei36")
    ap.add_argument("--test", action="store_true", help="写入后向上游发一次请求验证")
    args = ap.parse_args()

    mdir = resolve_marvis_dir(args.marvis_dir)
    if not mdir:
        print("[creds] 未找到 Marvis 用户数据目录。已探测：", file=sys.stderr)
        for c in candidate_dirs():
            print(f"          {c}  ->  {'存在' if c.is_dir() else '不存在'}", file=sys.stderr)
        print("        可用 --marvis-dir <路径> 或设置环境变量 MARVIS_DATA_DIR 指定。",
              file=sys.stderr)
        print("        提示：数据目录与安装目录无关，通常在 %APPDATA%\\Tencent\\Marvis。",
              file=sys.stderr)
        return 2
    print(f"[creds] Marvis 数据目录：{mdir}")

    openid = args.openid or (discover_openid(mdir) if args.auto else None)
    guid = args.guid or (discover_guid(mdir) if args.auto else None)
    qimei36 = args.qimei36 or (discover_qimei36(mdir) if args.auto else None)

    if not openid or not guid:
        print(f"[creds] 缺少 openid/guid（openid={openid} guid={guid}），请显式传入", file=sys.stderr)
        print(f"        已检查 {mdir / 'User'}（openid）与 {mdir / 'db'}（guid）", file=sys.stderr)
        return 2
    if qimei36 is None and args.auto:
        print("[creds] 警告：未从 beacon 库解析出 qimei36，将保留 config.toml 里的原值。"
              "该字段不符会被上游 4100404 风控。", file=sys.stderr)
    if not args.token.startswith("mv_"):
        print(f"[creds] 警告：token 不以 mv_ 开头（{args.token[:8]}...）", file=sys.stderr)

    text = CFG.read_text(encoding="utf-8")
    text = set_key(text, "openid", openid)
    text = set_key(text, "access_token", args.token)
    text = set_key(text, "device_guid", guid)
    if qimei36:
        text = set_key(text, "qimei36", qimei36)
    CFG.write_text(text, encoding="utf-8", newline="\r\n")

    # 自校验：必须能被 tomllib 解析
    import tomllib
    cfg = tomllib.loads(CFG.read_text(encoding="utf-8"))["marvis"]
    assert cfg["openid"] == openid and cfg["access_token"] == args.token and cfg["device_guid"] == guid

    print(f"[creds] 已写入 {CFG}")
    print(f"        openid       = {openid}")
    print(f"        access_token = {args.token[:6]}...{args.token[-4:]} (len={len(args.token)})")
    print(f"        device_guid  = {guid}")
    if qimei36:
        print(f"        qimei36      = {qimei36}")

    if args.test:
        body = json.dumps({
            "model": "main-auto",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 16,
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:4605/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                print(f"[test] HTTP {r.status}")
                print("       ", r.read(400).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            print(f"[test] HTTP {e.code}")
            print("       ", e.read(400).decode("utf-8", "replace"))
        except Exception as e:
            print(f"[test] 请求失败: {e}（bridge 未启动？先 .\\deploy.ps1）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
