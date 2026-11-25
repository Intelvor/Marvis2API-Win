#!/usr/bin/env python3
"""inject_api.py — 在 MarvisAgent 进程内开一个 OpenAI 兼容入口

原理（已实证）：
  官方客户端的 chat 请求经它自己的 httpx 客户端发出，能拿到 200。
  风控不在 body 内容层 —— 把 body 换成 104 字节的极简 JSON 仍返回 200。
  也就是说，复用客户端的「客户端实例 + 连接栈」就能发任意请求。

做法：
  1. 劫持 httpx.AsyncClient.send，抓住客户端实例 self 与真实请求头模板
  2. 在进程内起一个本地 HTTP 服务（默认 127.0.0.1:4899）
  3. 外部按 OpenAI 格式请求它 → 它用客户端实例转发到上游 → 回传

默认 observe 模式：只观察客户端自己的请求（记录 scene / 状态码），
不修改它们，安全。收到一个 200 的 chat 请求后即保存头模板。

用法（需管理员）：
  python tools/inject_api.py --start / --run / --stop
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "api.log"
CAP = ROOT / "tools" / "api-cap.jsonl"
TPL = ROOT / "tools" / "api-template.json"
PIDFILE = ROOT / "tools" / "api.pid"

PYCODE = r'''
import asyncio, json, pathlib, threading, time, traceback, uuid
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "api-cap.jsonl"
TPL = BASE / "tools" / "api-template.json"
PORT = 4899

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:20000] + "\n")
    except Exception:
        pass

_state = {"client": None, "loop": None, "headers": None, "url": None}
_dll = {"m": None}

def _uskey():
    """用客户端自带的 beacon_wrapper.dll 实时生成 uskey。"""
    try:
        if _dll["m"] is None:
            import ctypes, glob, os
            cands = glob.glob(r"D:\Program Files\Tencent\Marvis\Application\*\win32-native\beacon_wrapper.dll")
            if not cands:
                return None
            path = cands[0]
            try:
                os.add_dll_directory(os.path.dirname(path))
            except Exception:
                pass
            m = ctypes.CDLL(path)
            m.beacon_init.argtypes = []
            m.beacon_init.restype = ctypes.c_int
            m.beacon_init()
            m.beacon_get_uskey.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                           ctypes.c_char_p, ctypes.c_int]
            m.beacon_get_uskey.restype = ctypes.c_int
            _dll["m"] = m
        buf = ctypes.create_string_buffer(8192)
        r = _dll["m"].beacon_get_uskey(b"marvis_client", b"{}",
                                       ctypes.cast(buf, ctypes.c_char_p), 8192)
        return buf.value.decode("utf-8", "replace") if r > 0 else None
    except Exception:
        return None

_pending = []          # 待发队列：[(body_bytes, scene, future)]
_last = {"code": None, "body": b""}

try:
    import httpx

    orig = getattr(httpx, "_marvis_orig_send", None)
    if orig is None:
        orig = httpx.AsyncClient.send
        httpx._marvis_orig_send = orig

    SKIP = ("content-length", "host", "accept-encoding", "connection")

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        is_chat = "/v1/chat/completions" in url
        if not is_chat:
            return await orig(self, request, **kwargs)

        _state["client"] = self
        _state["url"] = url
        try:
            _state["loop"] = asyncio.get_running_loop()
        except Exception:
            pass
        scene = request.headers.get("marvis-scene", "?")

        # 在同一个 client 实例还活着的时候，先把待发请求发出去。
        # 关键：client 在 send 返回后会被调用方关闭，只有此刻复用它
        # 才保有相同的连接池 / transport / 代理配置。
        # 一次 send 只放行一个待发请求，避免被 memory/agent_chat 各发一次。
        tried = set()
        idx = 0
        while idx < len(_pending):
            body, sc, fut = _pending[idx]
            if id(fut) in tried:
                idx += 1
                continue
            tried.add(id(fut))
            _pending.pop(idx)
            try:
                # 关键：用当前真实请求的头（含新鲜的 uskey / nonce），
                # 不能用保存的模板 —— 旧 uskey 会被判风控。
                h = {k: v for k, v in request.headers.items()
                     if k.lower() not in SKIP}
                if sc:
                    h["marvis-scene"] = sc
                rid = "resp_" + uuid.uuid4().hex
                h["marvis-response-id"] = rid
                h["ual-access-requestid"] = rid
                # 关键：同一 uskey 在同一连接上复用会被判重放。
                # 中继请求重新生成 uskey / nonce。
                try:
                    ext = json.loads(h.get("ual-access-marvisext", "{}"))
                    uk = _uskey()
                    if uk:
                        ext["uskey"] = uk
                        ext["nonce"] = "nonce_" + uuid.uuid4().hex[:16]
                        h["ual-access-marvisext"] = json.dumps(
                            ext, ensure_ascii=False, separators=(",", ":"))
                        log({"stage": "uskey_refreshed", "len": len(uk)})
                except Exception as e:
                    log({"stage": "uskey_skip", "err": repr(e)[:200]})
                r = await orig(self, httpx.Request(
                    "POST", url, headers=h, content=body))
                fut["code"] = r.status_code
                try:
                    fut["body"] = r.text[:8000]
                except Exception:
                    fut["body"] = "<stream>"
                log({"stage": "relay", "status": r.status_code,
                     "len": len(body), "scene": sc})
            except Exception:
                fut["code"] = -1
                fut["body"] = traceback.format_exc()[:600]
                log({"stage": "relay_err", "err": fut["body"][:300]})
            break

        resp = await orig(self, request, **kwargs)
        log({"stage": "client_req", "scene": scene,
             "len": len(request.content), "status": resp.status_code})
        if resp.status_code == 200 and _state["headers"] is None:
            tpl = {k: v for k, v in request.headers.items()
                   if k.lower() not in SKIP}
            _state["headers"] = tpl
            try:
                TPL.write_text(json.dumps(tpl, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            except Exception:
                pass
            log({"stage": "template_saved", "n": len(tpl)})
        return resp

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    # ---------- 本地 OpenAI 兼容入口 ----------
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _handle(body: bytes) -> tuple[int, bytes]:
        scene = None
        try:
            obj = json.loads(body)
            scene = obj.pop("_scene", None)
            body2 = json.dumps(obj, ensure_ascii=False,
                               separators=(",", ":")).encode()
        except Exception:
            body2 = body
        if not _state.get("headers"):
            return 503, b'{"error":"no template yet: send one message in the client first"}'
        # 入队，等客户端下一次 send 时在同一 client 实例上发出
        slot = {"code": None, "body": b""}
        _pending.append((body2, scene, slot))
        for _ in range(1200):        # 最多等 120 秒
            if slot["code"] is not None:
                break
            time.sleep(0.1)
        if slot["code"] is None:
            try:
                _pending.remove((body2, scene, slot))
            except ValueError:
                pass
            return 504, b'{"error":"timeout: send a message in the client to trigger relay"}'
        if slot["code"] < 0:
            return 502, json.dumps({"error": slot["body"]}).encode()
        return slot["code"], slot["body"]

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
                log({"stage": "api", "path": self.path, "status": st,
                     "len": len(data)})
                self._send(st, data)
            except Exception:
                log({"stage": "api_err", "err": traceback.format_exc()[:800]})
                self._send(500, b'{"error":"internal"}')

        def do_GET(self):
            self._send(200, json.dumps({
                "ok": True, "ready": _state.get("headers") is not None,
            }).encode())

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        log({"stage": "listening", "port": PORT})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": PORT})

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
        for ln in CAP.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]:
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
        out = open(ROOT / "tools" / "api.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
