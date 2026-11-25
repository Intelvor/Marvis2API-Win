#!/usr/bin/env python3
"""inject_probe3.py — 验证「是否必须用原 request 对象」

已确证：
  - base_asis（逐字节复制原 body + 新 response-id，新建 Request）→ 403
  - passthru（原 request 对象原样发）→ 200
  同样 105499 字节，只差「是不是原对象」。

本脚本对比三种发法，每次只跑一种（单步，避免限流）：
  A. 原对象 + 只改 body（改 _content + content-length）
  B. 原对象 + 只改 response-id 头（body 不动）
  C. 原对象什么都没改，再发一遍（看重复发同一对象会怎样）

用法（需管理员）：python tools/inject_probe3.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "probe3.log"
CAP = ROOT / "tools" / "probe3-cap.jsonl"
PIDFILE = ROOT / "tools" / "probe3.pid"

PYCODE = r'''
import json, pathlib, traceback, uuid
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "probe3-cap.jsonl"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
    except Exception:
        pass

_flags = {"requested": False, "done": False}
MODES = ["E_fresh_prev_rid"]

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

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        if "/v1/chat/completions" not in url:
            return await orig(self, request, **kwargs)

        if _flags["requested"] and not _flags["done"]:
            if len(request.content) < 50000:
                log({"stage": "skip_small", "len": len(request.content)})
                return await orig(self, request, **kwargs)
            _flags["done"] = True

            idx_file = BASE / "tools" / "probe3-idx.txt"
            try:
                idx = int(idx_file.read_text().strip())
            except Exception:
                idx = 0
            if idx >= len(MODES):
                log({"stage": "all_tested"})
                return await orig(self, request, **kwargs)
            mode = MODES[idx]
            idx_file.write_text(str(idx + 1))

            try:
                # E: 用「客户端上一次用过的 response-id」发新请求。
                # 若这条能过，说明只要 id 与会话状态匹配即可自由调用。
                if mode == "E_fresh_prev_rid":
                    pass
                # A: 换 body 内容，但 response-id 原样沿用。
                # 上一轮已证明：换了 response-id 就必 403，
                # 所以这里保留原 id，只改最后一条 user 消息。
                if mode == "A_body_only":
                    tpl = json.loads(request.content)
                    msgs = list(tpl.get("messages") or [])
                    msgs[-1] = {"role": "user", "content": "用一句话说明什么是UDP"}
                    tpl["messages"] = msgs
                    nb = json.dumps(tpl, ensure_ascii=False,
                                    separators=(",", ":")).encode()
                    hh = [(k, v) for k, v in request.headers.items()
                          if k.lower() != "content-length"]
                    request = httpx.Request(request.method, request.url,
                                            headers=hh, content=nb)
                elif mode == "B_rid_only":
                    request.headers["marvis-response-id"] = "resp_" + uuid.uuid4().hex
                    request.headers["ual-access-requestid"] = \
                        request.headers["marvis-response-id"]
                elif mode == "C_resend_same":
                    pass  # 什么也不改，原样再发一遍
                elif mode == "D_body_no_rid":
                    tpl = json.loads(request.content)
                    msgs = list(tpl.get("messages") or [])
                    msgs[-1] = {"role": "user", "content": "用一句话说明什么是UDP"}
                    tpl["messages"] = msgs
                    nb = json.dumps(tpl, ensure_ascii=False,
                                    separators=(",", ":")).encode()
                    request._content = nb
                    request.headers["content-length"] = str(len(nb))
                    # 不换 response-id

                r = await orig(self, request)
                log({"stage": "try", "mode": mode,
                     "len": len(request.content), "status": r.status_code})
                try:
                    await r.aread()
                except Exception:
                    pass
            except Exception:
                log({"stage": "try_err", "mode": mode,
                     "err": traceback.format_exc()[:600]})

        resp = await orig(self, request, **kwargs)
        log({"stage": "passthru", "scene": request.headers.get("marvis-scene", "?"),
             "len": len(request.content), "status": resp.status_code})
        return resp

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

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
            n = int(self.headers.get("content-length") or 0)
            if n:
                self.rfile.read(n)
            _flags["requested"] = True
            _flags["done"] = False
            log({"stage": "armed"})
            self._send(200, b'{"armed":true}')

        def do_GET(self):
            self._send(200, b'{"patched":true}')

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4893), H)
        log({"stage": "listening", "port": 4893})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4893})

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
        out = open(ROOT / "tools" / "probe3.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
