#!/usr/bin/env python3
"""inject_free.py — 验证能否自由调用（不依赖客户端发消息动作）

核心发现（本轮的钥匙）：
  Marvis-Response-ID / Ual-Access-Requestid 必须与客户端的会话状态匹配。
    自己生成新 id  → 403
    沿用当次请求的原 id → 200

所以「自由调用」的前提是：能拿到一个当前有效的 response-id。
思路：每次客户端发出真实请求时把它用过的 id 记下来，之后直接复用。
若上游对同一个 id 允许多次使用，就能自由调用。

本脚本：
  1. 记录客户端最近一次 agent_chat 请求的完整 header（含 response-id）
  2. 提供 API：用这份 header 发任意 content
  3. 记录每次结果，看同一个 id 能否重复使用

用法（需管理员）：python tools/inject_free.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "free.log"
CAP = ROOT / "tools" / "free-cap.jsonl"
TPL = ROOT / "tools" / "free-tpl.json"
PIDFILE = ROOT / "tools" / "free.pid"

PYCODE = r'''
import asyncio, json, pathlib, time, threading, traceback
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "free-cap.jsonl"
TPL = BASE / "tools" / "free-tpl.json"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
    except Exception:
        pass

_state = {"tpl": None, "url": None, "loop": None, "client": None}
_queue = []

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

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        if "/v1/chat/completions" not in url:
            return await orig(self, request, **kwargs)

        # 只记录带完整上下文的请求（agent_chat），它的 header 才有效
        if len(request.content) > 50000:
            tpl = {k: v for k, v in request.headers.items()
                   if k.lower() not in SKIP}
            _state["tpl"] = tpl
            _state["url"] = url
            _state["client"] = self
            try:
                _state["loop"] = asyncio.get_running_loop()
            except Exception:
                pass
            try:
                TPL.write_text(json.dumps(tpl, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            except Exception:
                pass
            log({"stage": "tpl_saved", "n": len(tpl),
                 "rid": tpl.get("marvis-response-id", "")[:24],
                 "conv": tpl.get("marvis-conversation-id", "")[:24]})

            # 顺手把排队中的请求发出去（此刻 client 还活着）
            while _queue:
                body, fut = _queue.pop(0)
                try:
                    h = dict(tpl)
                    r = await orig(self, httpx.Request(
                        "POST", url, headers=h, content=body))
                    fut["status"] = r.status_code
                    try:
                        await r.aread()
                    except Exception:
                        pass
                    log({"stage": "free_call", "status": r.status_code,
                         "len": len(body)})
                except Exception:
                    fut["status"] = -1
                    log({"stage": "free_err",
                         "err": traceback.format_exc()[:500]})

        return await orig(self, request, **kwargs)

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _handle(body: bytes):
        if _state.get("tpl") is None:
            return 503, b'{"error":"no template: send one message in client"}'
        slot = {"status": None}
        _queue.append((body, slot))
        for _ in range(1200):
            if slot["status"] is not None:
                break
            time.sleep(0.1)
        if slot["status"] is None:
            return 504, b'{"error":"timeout: need a client send to piggyback"}'
        return 200, json.dumps({"status": slot["status"]}).encode()

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

        def do_POST(self):
            try:
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b"{}"
                st, data = _handle(body)
                log({"stage": "api", "status": st})
                self._send(st, data)
            except Exception:
                log({"stage": "api_err", "err": traceback.format_exc()[:600]})
                self._send(500, b'{"error":"internal"}')

        def do_GET(self):
            self._send(200, json.dumps({
                "ok": True,
                "has_tpl": _state.get("tpl") is not None,
                "rid": ((_state.get("tpl") or {}).get("marvis-response-id") or "")[:24],
            }).encode())

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4892), H)
        log({"stage": "listening", "port": 4892})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4892})

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


def run_fg() -> None:
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
    if CAP.exists():
        for ln in CAP.read_text(encoding="utf-8", errors="replace").splitlines()[-4:]:
            log("  cap| " + ln[:180])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    a = ap.parse_args()
    if a.start:
        LOG.write_text("", encoding="utf-8")
        out = open(ROOT / "tools" / "free.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
