#!/usr/bin/env python3
"""inject_diff.py — 字节级 diff：客户端 200 请求 vs 我们的 403 请求

目标：搞清两者在 HTTP 报文层的实际差异。

做两件事：
  1. 记录客户端自己发出的 chat 请求（拿到 200 的那次）的完整
     method / url / headers 原序原大小写 / body 长度
  2. 记录我们借同一 client 发出的 chat 请求（拿到 403 的那次）
  3. 逐字段 diff，输出 JSON 到 pair-diff.json

关键：用 request.headers.raw 保留原始大小写与顺序，
不能只看 dict（dict 会把大小写归一化，掩盖差异）。

用法（需管理员）：python tools/inject_diff.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "diff.log"
CAP = ROOT / "tools" / "diff-cap.jsonl"
OUT = ROOT / "tools" / "pair-diff.json"
PIDFILE = ROOT / "tools" / "diff.pid"

PYCODE = r'''
import json, pathlib, traceback, uuid
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "diff-cap.jsonl"
OUT = BASE / "tools" / "pair-diff.json"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
    except Exception:
        pass

_seen = {"client": None, "relay": None}
_pending = []

try:
    import httpx

    orig = getattr(httpx, "_marvis_orig_send", None)
    if orig is None:
        orig = httpx.AsyncClient.send
        httpx._marvis_orig_send = orig

    SKIP = ("content-length", "host", "accept-encoding", "connection")

    def dump(request):
        """完整记录一个请求：原序原大小写的头 + body 摘要。"""
        try:
            raw = request.headers.raw
            hdrs = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in raw]
        except Exception:
            hdrs = [[k, v] for k, v in request.headers.items()]
        return {
            "method": request.method,
            "url": str(request.url),
            "headers": hdrs,
            "body_len": len(request.content),
            "http_version": getattr(request, "extensions", {}).get(
                "http_version", b"?").decode("latin-1") if isinstance(
                getattr(request, "extensions", {}).get("http_version"), bytes) else "?",
        }

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        is_chat = "/v1/chat/completions" in url
        if not is_chat:
            return await orig(self, request, **kwargs)

        # 有待跑中继时，先借这个 client 发一次（这段会 403，正是要对照的）
        if _pending:
            body, fut = _pending.pop(0)
            try:
                # 决定性验证：完全继承客户端当前请求的 response-id /
                # requestid，只换 body。inject_body 成功时正是这样。
                h = {k: v for k, v in request.headers.items()
                     if k.lower() not in SKIP}
                h["marvis-response-id"] = request.headers.get(
                    "marvis-response-id", "")
                h["ual-access-requestid"] = request.headers.get(
                    "ual-access-requestid", "")
                req2 = httpx.Request("POST", url, headers=h, content=body)
                _seen["relay"] = dump(req2)
                r = await orig(self, req2)
                fut["status"] = r.status_code
                log({"stage": "relay", "status": r.status_code})
            except Exception:
                fut["status"] = -1
                log({"stage": "relay_err", "err": traceback.format_exc()[:500]})

        # 记录客户端自己的请求（这次会是 200）
        _seen["client"] = dump(request)
        resp = await orig(self, request, **kwargs)
        log({"stage": "client_req", "status": resp.status_code,
             "len": len(request.content)})

        if resp.status_code == 200 and _seen.get("relay") and not OUT.exists():
            a, b = _seen["client"], _seen["relay"]
            ha = {k.lower(): v for k, v in a["headers"]}
            hb = {k.lower(): v for k, v in b["headers"]}
            only_client = sorted(set(ha) - set(hb))
            only_relay = sorted(set(hb) - set(ha))
            common_diff = {}
            for k in sorted(set(ha) & set(hb)):
                if ha[k] != hb[k]:
                    common_diff[k] = {"client": ha[k][:80], "relay": hb[k][:80]}
            report = {
                "client": a, "relay": b,
                "header_only_in_client": only_client,
                "header_only_in_relay": only_relay,
                "same_key_diff_value": common_diff,
                "client_header_order": [k for k, _ in a["headers"]],
                "relay_header_order": [k for k, _ in b["headers"]],
                "order_equal": [k for k, _ in a["headers"]] == [k for k, _ in b["headers"]],
            }
            OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            log({"stage": "diff_written",
                 "only_client": only_client, "only_relay": only_relay,
                 "diff_keys": list(common_diff.keys()),
                 "order_equal": report["order_equal"]})
        return resp

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    def _handle(body: bytes):
        slot = {"status": None}
        _pending.append((body, slot))
        for _ in range(1800):
            if slot["status"] is not None:
                break
            time.sleep(0.1)
        if slot["status"] is None:
            return 504, b'{"error":"timeout: send a message in the client"}'
        return 200, json.dumps({"relay_status": slot["status"]}).encode()

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
                "ok": True, "has_diff": OUT.exists()}).encode())

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4897), H)
        log({"stage": "listening", "port": 4897})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4897})

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
        out = open(ROOT / "tools" / "diff.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
