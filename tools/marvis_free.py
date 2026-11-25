#!/usr/bin/env python3
"""marvis_free.py — 免搭载的 OpenAI 兼容服务

核心发现（B 路线的终点）：
  Marvis-Response-ID / Ual-Access-Requestid 是**会话凭证**，
    自己生成新值 → 403
    沿用客户端当次请求的原值 → 200
  所以只要保存一份客户端刚用过的有效 header，就能自由发请求，
  不需要 CDP 触发、不需要客户端发消息每次搭载。

工作方式：
  1. 注入 MarvisAgent，捕获任意一次 agent_chat 的完整 header（bootstrap）
  2. 之后每次请求都复用这份 header，只换 body
  3. 若凭证过期（返回 403），自动提示需要重新 bootstrap

用法：
  python tools/marvis_free.py --inject    # 注入（需管理员）
  python tools/marvis_free.py --serve     # 起服务（默认 4891）
  python tools/marvis_free.py --bootstrap # 提示去客户端发一条消息以刷新凭证
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "mfree.log"
CAP = ROOT / "tools" / "mfree-cap.jsonl"
TPL = ROOT / "tools" / "mfree-tpl.json"
PIDFILE = ROOT / "tools" / "mfree.pid"
PORT = 4891

PYCODE = r'''
import asyncio, json, pathlib, time, threading, traceback, uuid
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "mfree-cap.jsonl"
TPL = BASE / "tools" / "mfree-tpl.json"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:30000] + "\n")
    except Exception:
        pass

_state = {"tpl": None, "url": None, "ready": False}

try:
    import httpx

    orig = getattr(httpx, "_marvis_orig_send", None)
    if orig is None or getattr(orig, "__closure__", None) is not None:
        cur, cand = httpx.AsyncClient.send, None
        seen = set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if getattr(cur, "__closure__", None) is None:
                cand = cur
                break
            nxt = None
            for cell in cur.__closure__:
                try:
                    v = cell.cell_contents
                except Exception:
                    continue
                if callable(v) and getattr(v, "__name__", "") == "send":
                    nxt = v
                    break
            cur = nxt
        orig = cand or httpx.AsyncClient.send
        httpx._marvis_orig_send = orig

    SKIP = ("content-length", "host", "accept-encoding", "connection")

    # 预先取出 ssl 与代理设置，复用客户端的 tls 上下文
    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        if "/v1/chat/completions" in url and len(request.content) > 50000:
            tpl = {k: v for k, v in request.headers.items()
                   if k.lower() not in SKIP}
            _state["tpl"] = tpl
            _state["url"] = url
            _state["ready"] = True
            try:
                TPL.write_text(json.dumps(tpl, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            except Exception:
                pass
            log({"stage": "tpl_saved", "n": len(tpl),
                 "rid": (tpl.get("marvis-response-id") or "")[:26]})
        return await orig(self, request, **kwargs)

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _raw_call(body: bytes):
        """在独立的 event loop 里用保存的 header 发请求。"""
        tpl = _state.get("tpl")
        url = _state.get("url")
        if not tpl or not url:
            return 503, b'{"error":"no credential: send one message in client"}'

        async def go():
            async with httpx.AsyncClient(timeout=600.0, trust_env=True) as c:
                r = await c.post(url, headers=dict(tpl), content=body)
                return r.status_code, await r.aread()

        try:
            return asyncio.run(go())
        except Exception:
            return 500, json.dumps(
                {"error": traceback.format_exc()[:600]}).encode()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, st, data):
            if isinstance(data, str):
                data = data.encode()
            self.send_response(st)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            tpl = _state.get("tpl") or {}
            self._send(200, json.dumps({
                "ok": True, "ready": _state["ready"],
                "rid": (tpl.get("marvis-response-id") or "")[:26],
            }).encode())

        def do_POST(self):
            try:
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                req = json.loads(raw)
                tpl = _state.get("tpl")
                if not tpl:
                    self._send(503, {"error": "no credential"})
                    return
                # 用客户端的模板 body 结构：只换 messages
                base = {"model": req.get("model") or "main-auto",
                        "messages": req.get("messages") or [],
                        "max_tokens": int(req.get("max_tokens") or 32000),
                        "stream": bool(req.get("stream", True)),
                        "stream_options": {"include_usage": True},
                        "temperature": float(req.get("temperature", 1)),
                        "thinking_enabled": True}
                nb = json.dumps(base, ensure_ascii=False,
                                separators=(",", ":")).encode()
                st, data = _raw_call(nb)
                log({"stage": "call", "status": st, "len": len(nb)})
                self._send(st, data)
            except Exception:
                log({"stage": "api_err", "err": traceback.format_exc()[:600]})
                self._send(500, {"error": "internal"})

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4891), H)
        log({"stage": "listening", "port": 4891})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4891})

except Exception:
    log({"stage": "fatal", "err": traceback.format_exc()[:1500]})
'''

JS_TMPL = r"""
(function () {
  const py = Process.findModuleByName('python311.dll');
  if (!py) { send({ok:false, err:'no python311'}); return; }
  const ensure = new NativeFunction(py.findExportByName('PyGILState_Ensure'), 'int', []);
  const release = new NativeFunction(py.findExportByName('PyGILState_Release'), 'void', ['int']);
  const run = new NativeFunction(py.findExportByName('PyRun_SimpleString'), 'int', ['pointer']);
  const mem = Memory.allocUtf8String(__CODE__);
  const gil = ensure();
  const rc = run(mem);
  release(gil);
  send({ok: true, rc: rc});
})();
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def do_inject() -> None:
    import frida

    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name.lower() == "marvisagent.exe"]
    if not procs:
        log("FATAL: no MarvisAgent.exe")
        return
    proc = procs[0]
    log(f"attach {proc.name} pid={proc.pid}")
    session = dev.attach(proc.pid)
    escaped = (PYCODE.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", ""))
    script = session.create_script(JS_TMPL.replace("__CODE__", '"' + escaped + '"'))
    done = {"v": False}

    def on_msg(m, data):
        log("MSG: " + str(m)[:300])
        done["v"] = True

    script.on("message", on_msg)
    script.load()
    for _ in range(40):
        if done["v"]:
            break
        time.sleep(0.25)
    time.sleep(1)
    try:
        script.unload()
    except Exception:
        pass
    session.detach()
    log("注入完成")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", action="store_true")
    a = ap.parse_args()
    if a.inject:
        do_inject()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
