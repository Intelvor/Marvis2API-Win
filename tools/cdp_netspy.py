#!/usr/bin/env python3
"""cdp_netspy.py — 通过 CDP 监听页面的网络请求，抓出「发消息」用的 API

思路：页面发消息必然走 fetch/XHR。用 Network domain 监听，只打印
非静态资源请求（排除 .js/.css/.png 等），从而定位真正的业务接口。

用法：
  python tools/cdp_netspy.py --seconds 45
然后在客户端里发一句话，脚本会把请求打出来。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

from websocket import create_connection

SKIP_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff",
            ".woff2", ".ttf", ".ico", ".map", ".mp4", ".webp")


def get_page_ws() -> str:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=10) as r:
        targets = json.loads(r.read())
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("[!] 无 page target")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=45)
    args = ap.parse_args()

    ws = create_connection(get_page_ws(), timeout=120)
    ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
    # 等 enable 回执
    deadline = time.time() + 5
    while time.time() < deadline:
        m = json.loads(ws.recv())
        if m.get("id") == 1:
            break

    print(f"[net] 监听 {args.seconds}s ... 请现在在客户端发一句话", flush=True)
    seen = {}
    end = time.time() + args.seconds
    ws.settimeout(2)
    while time.time() < end:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            continue
        method = msg.get("method")
        p = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            req = p.get("request", {})
            url = req.get("url", "")
            if any(url.lower().split("?")[0].endswith(e) for e in SKIP_EXT):
                continue
            if url.startswith("data:") or url.startswith("blob:"):
                continue
            rid = p.get("requestId")
            seen[rid] = {"url": url, "method": req.get("method"),
                         "headers": req.get("headers", {}),
                         "postData": req.get("postData")}
        elif method == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in seen:
                seen[rid]["status"] = p.get("response", {}).get("status")

    print(f"\n[net] 共捕获 {len(seen)} 个业务请求\n")
    for rid, r in seen.items():
        print("=" * 70)
        print(f"{r['method']} {r['url'][:150]}  -> {r.get('status')}")
        h = {k: v for k, v in r["headers"].items()
             if k.lower().startswith(("ual-", "marvis-", "authorization", "content-type"))}
        if h:
            print("  headers:", json.dumps(h, ensure_ascii=False)[:400])
        if r.get("postData"):
            print("  body:", r["postData"][:500])
    ws.close()


if __name__ == "__main__":
    main()
