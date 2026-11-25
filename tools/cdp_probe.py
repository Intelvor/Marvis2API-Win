#!/usr/bin/env python3
"""cdp_probe.py — 通过 CDP 连接 Marvis 渲染进程，探测可用的内部 API

连上后可在页面上下文里执行 JS。Marvis 是 Electron 应用，渲染进程能看到
preload 注入的桥接对象（window.xxx），那是调用主进程能力的入口。

用法：
  python tools/cdp_probe.py --eval "Object.keys(window).filter(k=>!k.startsWith('_'))"
  python tools/cdp_probe.py --probe          # 探测常见桥接对象
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

from websocket import create_connection  # websocket-client


def get_page_ws() -> str:
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=10) as r:
        targets = json.loads(r.read())
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("[!] 没找到 page target")


class CDP:
    def __init__(self, ws_url: str):
        self.ws = create_connection(ws_url, timeout=30)
        self.id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self.id += 1
        mid = self.id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg
        return {"error": "timeout"}

    def eval(self, expr: str, timeout: float = 30) -> object:
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": True, "userGesture": True,
        }, timeout=timeout)
        if "error" in r:
            return f"[ERR] {r['error']}"
        res = r.get("result", {})
        if res.get("exceptionDetails"):
            return f"[EXC] {json.dumps(res['exceptionDetails'], ensure_ascii=False)[:300]}"
        return (res.get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


PROBES = {
    "window keys": "Object.keys(window).filter(k => !['0','1','2','3','4','5','6','7','8','9'].includes(k) && !k.startsWith('on')).slice(0,200)",
    "electron 对象": "({hasRequire: typeof require, hasProcess: typeof process, hasElectronAPI: typeof window.electronAPI, hasElectron: typeof window.electron, hasMarvis: typeof window.marvis})",
    "疑似桥接": "Object.keys(window).filter(k => /marvis|bridge|ipc|agent|sdk|api|native|host/i.test(k))",
    "process 信息": "typeof process !== 'undefined' ? {platform: process.platform, versions: process.versions, argv: (process.argv||[]).slice(0,5)} : null",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", help="直接执行的 JS 表达式")
    ap.add_argument("--probe", action="store_true", help="跑内置探测")
    args = ap.parse_args()

    ws_url = get_page_ws()
    print(f"[cdp] {ws_url}")
    c = CDP(ws_url)
    try:
        if args.eval:
            print(json.dumps(c.eval(args.eval), ensure_ascii=False, indent=2)[:3000])
            return
        for name, expr in PROBES.items():
            print(f"\n--- {name} ---")
            print(json.dumps(c.eval(expr), ensure_ascii=False, indent=2)[:1500])
    finally:
        c.close()


if __name__ == "__main__":
    main()
