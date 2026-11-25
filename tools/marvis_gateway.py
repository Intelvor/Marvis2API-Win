#!/usr/bin/env python3
"""marvis_gateway.py — 通过 CDP 拿 MarvisHost 的 gateway token / 服务端口

用途：Marvis 1.60.3400 起，上游请求由 MarvisHost.exe 统一代理，本地
gateway 监听 127.0.0.1:10123，需要运行时随机 token 才能访问。
token 只存在内存里，拿它的唯一可靠途径是客户端自己的桥接 API：

    window.Jsbridge.AiStarter.GetGatewayToken()

本工具用 CDP（需客户端带 --remote-debugging-port 启动）在页面里执行这段 JS。

用法：
  python tools/marvis_gateway.py --token        # 取 gateway token + port
  python tools/marvis_gateway.py --ports        # 取所有服务端口
  python tools/marvis_gateway.py --probe        # 用 token 验证 gateway 可否访问
  python tools/marvis_gateway.py --enable-cdp   # 打印启用 CDP 的启动参数
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

try:
    from websocket import create_connection
except ImportError:
    raise SystemExit("[!] 需要 websocket-client:  python -m pip install websocket-client")

CDP_LIST = "http://127.0.0.1:9222/json/list"


def page_ws() -> str:
    with urllib.request.urlopen(CDP_LIST, timeout=10) as r:
        targets = json.loads(r.read())
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("[!] 没有 page target。客户端是否带 --remote-debugging-port=9222 启动？")


def evaluate(expr: str, wait_ms: int = 8000) -> object:
    ws = create_connection(page_ws(), timeout=wait_ms / 1000 + 15)
    try:
        payload = json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": True, "userGesture": True},
        })
        ws.send(payload)
        deadline = time.time() + wait_ms / 1000 + 10
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            res = msg.get("result", {})
            if res.get("exceptionDetails"):
                return {"_error": str(res["exceptionDetails"])[:400]}
            v = (res.get("result") or {}).get("value")
            return v
        return {"_error": "timeout"}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def get_token() -> dict:
    expr = """
    new Promise((resolve) => {
      const t = setTimeout(() => resolve(JSON.stringify({error:'timeout'})), 7000);
      try {
        const p = window.Jsbridge.AiStarter.GetGatewayToken();
        if (p && typeof p.then === 'function') {
          p.then(v => { clearTimeout(t); resolve(JSON.stringify(v)); })
           .catch(e => { clearTimeout(t); resolve(JSON.stringify({error:String(e)})); });
        } else { clearTimeout(t); resolve(JSON.stringify({error:'not promise', value:p})); }
      } catch (e) { clearTimeout(t); resolve(JSON.stringify({error:String(e)})); }
    })
    """
    raw = evaluate(expr)
    try:
        outer = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": raw}
    inner = outer.get("data") if isinstance(outer, dict) else None
    if isinstance(inner, str):
        try:
            return json.loads(inner)
        except Exception:
            return {"raw": inner}
    return outer


def get_ports() -> dict:
    expr = """
    new Promise((resolve) => {
      const t = setTimeout(() => resolve(JSON.stringify({error:'timeout'})), 7000);
      try {
        const p = window.marvis.getServicePorts();
        if (p && typeof p.then === 'function') {
          p.then(v => { clearTimeout(t); resolve(JSON.stringify(v)); })
           .catch(e => { clearTimeout(t); resolve(JSON.stringify({error:String(e)})); });
        } else { clearTimeout(t); resolve(JSON.stringify(p)); }
      } catch (e) { clearTimeout(t); resolve(JSON.stringify({error:String(e)})); }
    })
    """
    raw = evaluate(expr)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": raw}


def probe_gateway(tok: str, port: int) -> None:
    # 页面实际用的就是这条（含 handler 参数）
    urls = [
        f"http://localhost:{port}/mobile/status?token={tok}&handler=openclaw",
        f"http://127.0.0.1:{port}/mobile/status?token={tok}&handler=openclaw",
    ]
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=8) as r:
                body = r.read().decode("utf-8", "replace")
                print(f"  [ok] {r.status}  {u.split('?')[0]}  ->  {body[:160]}")
        except urllib.error.HTTPError as e:
            print(f"  [{e.code}] {u.split('?')[0]}")
        except Exception as e:
            print(f"  [ERR] {u.split('?')[0]}  {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", action="store_true")
    ap.add_argument("--ports", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--enable-cdp", action="store_true")
    args = ap.parse_args()

    if args.enable_cdp:
        print("启用 CDP（需重启客户端）：")
        print(r'  "D:\Program Files\Tencent\Marvis\Application\1.60.3400.35\Marvis.exe"'
              r" --remote-debugging-port=9222 --remote-allow-origins=*")
        return

    do_all = not (args.token or args.ports or args.probe)

    if args.token or do_all:
        info = get_token()
        print("gateway:", json.dumps(info, ensure_ascii=False))
        if args.probe and info.get("token"):
            probe_gateway(info["token"], info.get("port", 10123))

    if args.ports or do_all:
        print("services:", json.dumps(get_ports(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
