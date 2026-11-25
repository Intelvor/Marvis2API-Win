#!/usr/bin/env python3
"""pick_token.py — 从扫描结果里挑出真正可用的 access_token

背景：客户端升级后会重新签发 token，但旧 token 可能仍残留在进程内存里。
按字典序或长度挑都会出错（实测升级后旧 token 恰好排在前面），
唯一可靠的判据是拿它向上游发一次请求。

用法：
  python tools/pick_token.py --file tools/token-scan.txt
  python tools/pick_token.py --file tools/token-scan.txt --dry-run   # 只列候选，不探测

输出（stdout 最后一行）：可用的 token 明文，供 PowerShell 捕获。
诊断信息一律走 stderr。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.toml"

TOKEN_RE = re.compile(r"mv_[A-Za-z0-9_\-]{20,}")
PROBE_TIMEOUT = 90


def builtin_headers(m: dict, token: str) -> dict[str, str]:
    """按 config.toml 当前指纹拼一次真实请求（与 bridge.py 保持一致）。"""
    ext = {
        "qimei36": m.get("qimei36") or m["device_guid"],
        "osVersion": m["os_version"],
        "guid": m["device_guid"],
        "uskey": "CsDVDCER" + base64.b64encode(os.urandom(240)).decode().rstrip("="),
        "nonce": str(uuid.uuid4()),
        "reqScene": m.get("req_scene", 1),
        "taskType": "user",
        "clientVersion": m["client_version"],
    }
    if m.get("client_platform"):
        ext["clientPlatform"] = m["client_platform"]
        ext["clientPlatformVersion"] = m["client_platform_version"]
    ext["isIoa"] = False
    rid = "resp_" + uuid.uuid4().hex
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "AsyncOpenAI/Python 2.32.0",
        "Authorization": "Bearer placeholder",
        "Ual-Access-Openid": m["openid"],
        "Ual-Access-Access-Token": token,
        "Ual-Access-Login-Type": str(m["login_type"]),
        "Ual-Access-Guid": m["device_guid"],
        "X-Device-GUID": m["device_guid"],
        "Ual-Access-MarvisExt": json.dumps(ext, ensure_ascii=False, separators=(",", ":")),
        "Marvis-AgentTag": "main",
        "Marvis-Scene": m.get("scene", "agent_chat"),
        "Marvis-Protocol-Version": m.get("protocol_version", "1000"),
        "Marvis-Conversation-ID": "conv_" + uuid.uuid4().hex,
        "Marvis-Response-ID": rid,
        "Ual-Access-Requestid": rid,
    }


def probe(m: dict, token: str) -> tuple[int, str]:
    body = json.dumps({
        "model": "main-auto",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "max_tokens": 16,
    }).encode()
    req = urllib.request.Request(
        m["base_url"] + m["chat_path"], data=body,
        headers=builtin_headers(m, token), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return r.status, r.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode("utf-8", "replace")
    except Exception as e:  # 网络问题不算 token 失效
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "tools" / "token-scan.txt"))
    ap.add_argument("--dry-run", action="store_true", help="只列候选，不探测上游")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"[pick] 扫描结果不存在：{src}", file=sys.stderr)
        return 2

    text = src.read_text(encoding="utf-8", errors="replace")
    cands = sorted(set(TOKEN_RE.findall(text)))
    if not cands:
        print("[pick] 结果文件里没有 mv_ 开头的候选", file=sys.stderr)
        return 2

    print(f"[pick] 候选 {len(cands)} 个：", file=sys.stderr)
    for t in cands:
        print(f"       {t[:10]}...{t[-4:]} (len={len(t)})", file=sys.stderr)

    if args.dry_run:
        print(cands[0])
        return 0

    try:
        m = tomllib.loads(CFG.read_text(encoding="utf-8"))["marvis"]
    except Exception as e:
        print(f"[pick] 读取 config.toml 失败：{e}", file=sys.stderr)
        return 2

    net_fail = False
    for t in cands:
        code, body = probe(m, t)
        tag = f"{t[:10]}...{t[-4:]}"
        if code == 200:
            print(f"[pick] {tag} -> HTTP 200 可用", file=sys.stderr)
            print(t)
            return 0
        if code == 0:
            net_fail = True
            print(f"[pick] {tag} -> 网络错误：{body[:120]}", file=sys.stderr)
        else:
            print(f"[pick] {tag} -> HTTP {code} {body[:120]}", file=sys.stderr)

    if net_fail:
        print("[pick] 存在网络错误，无法判定。检查代理/网络后重试；"
              "若确认网络正常则是 token 全部失效，请在客户端重新登录。", file=sys.stderr)
    else:
        print("[pick] 所有候选均被上游拒绝。请在 Marvis 客户端重新登录后再扫。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
