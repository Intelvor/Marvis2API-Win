#!/usr/bin/env python3
"""inject_clone.py — 克隆原 Request 对象后替换 body（关键实验）

已确证的对照：
  A. 就地替换客户端正在发出的 Request 的 body  → 200
  B. 用 request.headers 新建 httpx.Request 再发 → 403
     两者头部字段逐一相同（只差 content-length），仍然 403。

唯一剩下的差异：B 是**新建**的 Request 对象，丢掉了原对象的
extensions（http_version / network_stream / timeout / sni_hostname 等），
这些字段决定 httpcore 走哪条连接、哪条 TLS 会话。

本脚本验证：把原 Request 的 extensions 一并复制到新对象上，
看能否让「另起请求」也拿到 200。

三种模式依次测试，全部借客户端自己的 client 实例：
  mode=clone  复制 extensions + 原 headers，只换 body
  mode+sni    额外显式设置 sni_hostname
  mode=stream 直接在原请求的 network_stream 上写（如可用）

用法（需管理员）：python tools/inject_clone.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "clone.log"
CAP = ROOT / "tools" / "clone-cap.jsonl"
PIDFILE = ROOT / "tools" / "clone.pid"

PYCODE = r'''
import json, pathlib, traceback

BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "clone-cap.jsonl"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
    except Exception:
        pass

_pending = []

try:
    import httpx

    orig = getattr(httpx, "_marvis_orig_send", None)
    if orig is None:
        orig = httpx.AsyncClient.send
        httpx._marvis_orig_send = orig

    SKIP = ("content-length",)

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        is_chat = "/v1/chat/completions" in url
        if not is_chat:
            return await orig(self, request, **kwargs)

        if _pending:
            body, fut = _pending.pop(0)
            results = []
            # ---- A: stream=true（真实客户端总是这样发）----
            try:
                b1 = json.dumps({"model": "main-auto",
                                 "messages": [{"role": "user",
                                               "content": "reply with OK"}],
                                 "stream": True,
                                 "stream_options": {"include_usage": True},
                                 "max_tokens": 32000,
                                 "temperature": 1,
                                 "thinking_enabled": True},
                                ensure_ascii=False,
                                separators=(",", ":")).encode()
                hdrs = [(k, v) for k, v in request.headers.items()
                        if k.lower() not in SKIP]
                hdrs.append(("content-length", str(len(b1))))
                req2 = httpx.Request(request.method, request.url,
                                     headers=hdrs, content=b1)
                if getattr(request, "extensions", None):
                    req2.extensions.update(request.extensions)
                r = await orig(self, req2)
                results.append({"mode": "stream_true", "len": len(b1),
                                "status": r.status_code})
                log({"stage": "try", **results[-1]})
            except Exception:
                results.append({"mode": "stream_true", "status": -1,
                                "err": traceback.format_exc()[:400]})
                log({"stage": "try_err", "mode": "stream_true"})

            # ---- B: stream=false（此前所有失败用例都是这个）----
            try:
                b2 = json.dumps({"model": "main-auto",
                                 "messages": [{"role": "user",
                                               "content": "reply with OK"}],
                                 "stream": False},
                                ensure_ascii=False,
                                separators=(",", ":")).encode()
                hdrs = [(k, v) for k, v in request.headers.items()
                        if k.lower() not in SKIP]
                hdrs.append(("content-length", str(len(b2))))
                req3 = httpx.Request(request.method, request.url,
                                     headers=hdrs, content=b2)
                if getattr(request, "extensions", None):
                    req3.extensions.update(request.extensions)
                r = await orig(self, req3)
                results.append({"mode": "stream_false", "len": len(b2),
                                "status": r.status_code})
                log({"stage": "try", **results[-1]})
            except Exception:
                results.append({"mode": "stream_false", "status": -1,
                                "err": traceback.format_exc()[:400]})
                log({"stage": "try_err", "mode": "stream_false"})

            fut["results"] = results
            log({"stage": "done", "results": results})

        resp = await orig(self, request, **kwargs)
        log({"stage": "client_req", "status": resp.status_code,
             "len": len(request.content),
             "ext_keys": sorted((getattr(request, "extensions", {}) or {}).keys())})
        return resp

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    def _handle(body: bytes):
        slot = {"results": None}
        _pending.append((body, slot))
        for _ in range(1800):
            if slot["results"] is not None:
                break
            time.sleep(0.1)
        if slot["results"] is None:
            return 504, b'{"error":"timeout: send a message in the client"}'
        return 200, json.dumps(slot["results"], ensure_ascii=False).encode()

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
            self._send(200, b'{"ok":true}')

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4896), H)
        log({"stage": "listening", "port": 4896})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4896})

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
  const code = __CODE__;
  const mem = Memory.allocUtf8String(code);
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
    js = JS_TMPL.replace("__CODE__", '"' + escaped + '"')
    script = session.create_script(js)
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
        for ln in CAP.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]:
            log("  cap| " + ln[:240])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()

    if a.stop:
        if PIDFILE.exists():
            pid = PIDFILE.read_text().strip()
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            PIDFILE.unlink(missing_ok=True)
            print(f"stopped {pid}")
        return
    if a.start:
        LOG.write_text("", encoding="utf-8")
        out = open(ROOT / "tools" / "clone.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
