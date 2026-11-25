#!/usr/bin/env python3
"""inject_pair.py — 验证「成对请求」假设

假设来源：抓到的真实请求里，一次发消息会连发两个 chat 请求：
  #1 Marvis-Scene: memory      reqScene=1  subScene=mem_read_rag_rewrite
  #2 Marvis-Scene: agent_chat  reqScene=0
且两者共用同一个 Marvis-Response-ID 与 Marvis-Conversation-ID。

此前的中继每次都刷新 response-id 单独发 agent_chat —— 相当于缺了前置登记。

本脚本：
  1. 借客户端自己的 httpx client 实例（在它的 send() 时刻）
  2. 用同一个 response-id 先后发 memory 与 agent_chat
  3. 记录两步状态码，判断前置 memory 是否是放行前提

用法（需管理员）：python tools/inject_pair.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "pair.log"
CAP = ROOT / "tools" / "pair-cap.jsonl"
PAIR = ROOT / "tools" / "pair-body.json"
PIDFILE = ROOT / "tools" / "pair.pid"

PYCODE = r'''
import asyncio, json, pathlib, traceback, uuid
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "pair-cap.jsonl"
PAIR = BASE / "tools" / "pair-body.json"

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

    SKIP = ("content-length", "host", "accept-encoding", "connection")

    def _scene_headers(base_headers, scene, reqscene, subscene, rid):
        h = {k: v for k, v in base_headers.items() if k.lower() not in SKIP}
        h["marvis-scene"] = scene
        h["marvis-response-id"] = rid
        h["ual-access-requestid"] = rid
        try:
            ext = json.loads(h.get("ual-access-marvisext", "{}"))
            ext["reqScene"] = reqscene
            if subscene:
                ext["subScene"] = subscene
            else:
                ext.pop("subScene", None)
            h["ual-access-marvisext"] = json.dumps(
                ext, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pass
        return h

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        is_chat = "/v1/chat/completions" in url
        if not is_chat:
            return await orig(self, request, **kwargs)

        # 有待跑任务时，先把成对请求跑完（在 client 还活着的时候）
        if _pending:
            body, fut = _pending.pop(0)
            rid = "resp_" + uuid.uuid4().hex
            cid = "conv_" + uuid.uuid4().hex
            base = {k: v for k, v in request.headers.items()
                    if k.lower() not in SKIP}
            base["marvis-conversation-id"] = cid
            steps = []
            for scene, rs, sub in (("memory", 1, "mem_read_rag_rewrite"),
                                   ("agent_chat", 0, None)):
                h = _scene_headers(base, scene, rs, sub, rid)
                try:
                    r = await orig(self, httpx.Request(
                        "POST", url, headers=h, content=body))
                    steps.append({"scene": scene, "status": r.status_code})
                    log({"stage": "pair_step", "scene": scene,
                         "status": r.status_code, "rid": rid,
                         "len": len(body)})
                except Exception:
                    steps.append({"scene": scene,
                                  "status": -1,
                                  "err": traceback.format_exc()[:400]})
                    log({"stage": "pair_err", "scene": scene,
                         "err": traceback.format_exc()[:400]})
            fut["steps"] = steps
            log({"stage": "pair_done", "steps": steps})

        resp = await orig(self, request, **kwargs)
        log({"stage": "client_req", "len": len(request.content),
             "status": resp.status_code,
             "scene": request.headers.get("marvis-scene", "?")})
        return resp

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _handle(body: bytes):
        slot = {"steps": None}
        _pending.append((body, slot))
        for _ in range(1800):
            if slot["steps"] is not None:
                break
            time.sleep(0.1)
        if slot["steps"] is None:
            return 504, json.dumps({"error": "timeout: send a message in the client"}).encode()
        return 200, json.dumps(slot["steps"], ensure_ascii=False).encode()

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
                log({"stage": "api", "status": st, "len": len(data)})
                self._send(st, data)
            except Exception:
                log({"stage": "api_err", "err": traceback.format_exc()[:800]})
                self._send(500, b'{"error":"internal"}')

        def do_GET(self):
            self._send(200, json.dumps({"ok": True}).encode())

        def log_message(self, *a):
            pass

    import threading
    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4898), H)
        log({"stage": "listening", "port": 4898})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4898})

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
        out = open(ROOT / "tools" / "pair.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
