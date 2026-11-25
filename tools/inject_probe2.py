#!/usr/bin/env python3
"""inject_probe2.py — 二分定位：完整 body 里哪些部分是必需的

已确证的事实：
  - 极简 body（200 字节）→ 403
  - 以客户端真实 body 为模板，只换最后一条 user 消息 → 200
  - 新建 httpx.Request 本身不导致 403（刚成功那次就是新建的）

所以差异只在 body 内容。本脚本以客户端当次的真实 body 为模板，
生成多个裁剪变体一次发出，看削到哪一步开始 403。

用法（需管理员）：python tools/inject_probe2.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "probe2.log"
CAP = ROOT / "tools" / "probe2-cap.jsonl"
PIDFILE = ROOT / "tools" / "probe2.pid"

PYCODE = r'''
import asyncio, json, pathlib, traceback, uuid
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "probe2-cap.jsonl"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
    except Exception:
        pass

_flags = {"requested": False, "done": False}
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
        log({"stage": "orig_reset",
             "native": getattr(orig, "__closure__", None) is None})

    SKIP_H = ("content-length",)

    def make_variants(tpl):
        """基于真实 body 生成裁剪变体，返回 [(名字, body_dict)]。"""
        import copy
        out = []

        def add(name, fn):
            try:
                d = copy.deepcopy(tpl)
                fn(d)
                out.append((name, d))
            except Exception as e:
                log({"stage": "variant_fail", "name": name, "err": repr(e)[:200]})

        add("base_asis", lambda d: None)
        add("no_tools", lambda d: d.pop("tools", None))
        add("no_think", lambda d: d.pop("thinking_enabled", None))
        add("no_stream_opts", lambda d: d.pop("stream_options", None))
        add("no_max_tokens", lambda d: d.pop("max_tokens", None))
        add("only_params", lambda d: d.__setitem__("messages", [m for m in d.get("messages", []) if m.get("role") != "system"][-1:]))
        add("no_tools_only_params", lambda d: (d.pop("tools", None), d.__setitem__("messages", [m for m in d.get("messages", []) if m.get("role") != "system"][-1:])))
        add("no_sys", lambda d: d.__setitem__("messages", [m for m in d.get("messages", []) if m.get("role") != "system"]))
        return out

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        if "/v1/chat/completions" not in url:
            return await orig(self, request, **kwargs)

        if _flags["requested"] and not _flags["done"]:
            # 只在拿到「完整上下文」的请求时做模板（memory 场景只有几 KB，
            # 用它当模板必 403）。并且一次只测一个变体：
            # 短时间内连发多个会被频率限流，导致连 base_asis 都 403。
            if len(request.content) < 50000:
                log({"stage": "skip_small",
                     "scene": request.headers.get("marvis-scene", "?"),
                     "len": len(request.content)})
                return await orig(self, request, **kwargs)
            _flags["done"] = True
            try:
                tpl = json.loads(request.content)
            except Exception:
                tpl = None
            if isinstance(tpl, dict):
                variants = make_variants(tpl)
                # 用持久化索引逐个推进，避免一次触发跑太多被限流
                idx_file = BASE / "tools" / "probe2-idx.txt"
                try:
                    idx = int(idx_file.read_text().strip())
                except Exception:
                    idx = 0
                if idx < len(variants):
                    name, body = variants[idx]
                    idx_file.write_text(str(idx + 1))
                    rid = "resp_" + uuid.uuid4().hex
                    h = [(k, v) for k, v in request.headers.items()
                         if k.lower() not in SKIP_H]
                    for i, (k, v) in enumerate(h):
                        if k.lower() in ("marvis-response-id",
                                         "ual-access-requestid"):
                            h[i] = (k, rid)
                    nb = json.dumps(body, ensure_ascii=False,
                                    separators=(",", ":")).encode()
                    try:
                        r = await orig(self, httpx.Request(
                            request.method, request.url, headers=h, content=nb))
                        st = r.status_code
                    except Exception:
                        st = -1
                    log({"stage": "variant", "idx": idx, "name": name,
                         "len": len(nb), "status": st})
                else:
                    log({"stage": "all_tested"})

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
            self._send(200, json.dumps({"patched": True}).encode())

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4894), H)
        log({"stage": "listening", "port": 4894})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4894})

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
        for ln in CAP.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]:
            log("  cap| " + ln[:200])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    a = ap.parse_args()
    if a.start:
        LOG.write_text("", encoding="utf-8")
        out = open(ROOT / "tools" / "probe2.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
