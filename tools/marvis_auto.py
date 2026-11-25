#!/usr/bin/env python3
"""marvis_auto.py — 全自动 OpenAI 兼容服务（凭证自愈）

体感目标：和普通 OpenAI 接口一样。调用方不需要知道底层有客户端、CDP、Frida。

工作方式：
  进程内（Frida 注入 MarvisAgent）：
    - 监听 httpx.AsyncClient.send，捕获一次 agent_chat 的完整 header 作为凭证
    - 提供本地 HTTP 服务
    - 每个请求都用捕获的 header 发上游
    - 返回 403 时，标记凭证失效，请求方（外部驱动）触发刷新后重试
  外部（本文件同一个进程的父侧）：
    - 检测到凭证失效 → 用 CDP 在客户端自动发一条消息 → 凭证自动刷新
    - 全程无人工介入

服务端口：4512（避免与之前的探针冲突）

用法（需管理员）：python tools/marvis_auto.py --start
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "auto.log"
CAP = ROOT / "tools" / "auto-cap.jsonl"
TPL = ROOT / "tools" / "auto-tpl.json"
NEED = ROOT / "tools" / "auto-need-refresh.flag"
PIDFILE = ROOT / "tools" / "auto.pid"
PORT = 4512

PYCODE = r'''
import asyncio, json, pathlib, threading, time, traceback
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "auto-cap.jsonl"
TPL = BASE / "tools" / "auto-tpl.json"
NEED = BASE / "tools" / "auto-need-refresh.flag"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:40000] + "\n")
    except Exception:
        pass

_state = {"tpl": None, "url": None}
_lock = threading.Lock()

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
        # 捕获凭证：只认带完整上下文的 agent_chat
        if "/v1/chat/completions" in url and len(request.content) > 50000:
            tpl = {k: v for k, v in request.headers.items()
                   if k.lower() not in SKIP}
            with _lock:
                _state["tpl"] = tpl
                _state["url"] = url
            try:
                TPL.write_text(json.dumps(tpl, ensure_ascii=False, indent=1),
                               encoding="utf-8")
            except Exception:
                pass
            try:
                if NEED.exists():
                    NEED.unlink()
            except Exception:
                pass
            log({"stage": "tpl_saved",
                 "rid": (tpl.get("marvis-response-id") or "")[:26]})
        return await orig(self, request, **kwargs)

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched"})

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _call(body: bytes):
        with _lock:
            tpl = dict(_state.get("tpl") or {})
            url = _state.get("url")
        if not tpl or not url:
            return 503, b'{"error":"bootstrap: send one message in client first"}'

        async def go():
            async with httpx.AsyncClient(timeout=600.0, trust_env=True) as c:
                r = await c.post(url, headers=tpl, content=body)
                raw = await r.aread()
                return r.status_code, raw

        try:
            return asyncio.run(go())
        except Exception:
            return 500, json.dumps(
                {"error": traceback.format_exc()[:500]}).encode()

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
                "ok": True,
                "ready": bool(tpl),
                "need_refresh": NEED.exists(),
                "rid": (tpl.get("marvis-response-id") or "")[:26],
            }).encode())

        def do_ANY(self):
            """把 /v3/* 等请求原样转发到上游，复用进程内身份。

            用于探测模型列表这类需要 UAL 签名的接口。
            路径 /fwd/<上游路径>，例如 /fwd/v3/pcyyb_ai_assistant_model_gateway
            """
            try:
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                path = self.path
                if not path.startswith("/fwd/"):
                    self._send(404, b'{"error":"use /fwd/<upstream-path>"}')
                    return
                up_path = path[len("/fwd"):]
                base = "https://yybadaccess.3g.qq.com"
                url = base + up_path

                # 用客户端真实身份：完整 MarvisExt + 真实 token
                with _lock:
                    tpl = dict(_state.get("tpl") or {})
                if not tpl:
                    self._send(503, b'{"error":"no credential"}')
                    return

                # /v3 需要 UAL 签名；用业务头
                import hashlib, random, tomllib, uuid
                ACCESS_KEY = tomllib.load(open(BASE / "config.toml", "rb"))["marvis"]["access_key"]
                ts = str(int(time.time() * 1000))
                nonce = str(random.randint(1000000000, 9999999999))
                txt = raw.decode("utf-8", "replace")
                sig = hashlib.md5(
                    (txt + ts + ACCESS_KEY + nonce).encode()).hexdigest()

                bh = {
                    "Host": "yybadaccess.3g.qq.com",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, zstd",
                    "Connection": "keep-alive",
                    "User-Agent": "python-httpx/0.28.1",
                    "Ual-Access-Requestid": uuid.uuid4().hex,
                    "Ual-Access-Businessid": self.headers.get(
                        "x-businessid", "pc_yyb"),
                    "Ual-Access-Guid": tpl.get("ual-access-guid", ""),
                    "Ual-Access-Timestamp": ts,
                    "Ual-Access-Nonce": nonce,
                    "Ual-Access-Signature": sig,
                    "Content-Type": "application/json; charset=UTF-8",
                }

                def go2():
                    async def inner():
                        async with httpx.AsyncClient(timeout=60.0,
                                                     trust_env=True) as c:
                            r = await c.request(self.command, url, headers=bh,
                                                content=raw)
                            return r.status_code, await r.aread()
                    return asyncio.run(inner())

                st, data = go2()
                log({"stage": "fwd", "path": up_path, "status": st,
                     "len": len(raw)})
                self._send(st, data)
            except Exception:
                log({"stage": "fwd_err", "err": traceback.format_exc()[:600]})
                self._send(500, b'{"error":"fwd failed"}')

        do_PUT = do_ANY
        do_DELETE = do_ANY

        def do_POST(self):
            # /fwd/* 走转发通道，其余当 chat 处理
            if self.path.startswith("/fwd/"):
                return self.do_ANY()
            try:
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                req = json.loads(raw)

                # 原样透传 messages 与 tools，只补必要的服务端参数
                body = {"model": req.get("model") or "main-auto",
                        "messages": req.get("messages") or [],
                        "max_tokens": int(req.get("max_tokens") or 32000),
                        "stream": bool(req.get("stream", False)),
                        "temperature": float(req.get("temperature", 1)),
                        "thinking_enabled": bool(req.get("thinking_enabled", True))}
                if req.get("tools"):
                    body["tools"] = req["tools"]
                if req.get("tool_choice"):
                    body["tool_choice"] = req["tool_choice"]
                if req.get("stream"):
                    body["stream_options"] = {"include_usage": True}

                nb = json.dumps(body, ensure_ascii=False,
                                separators=(",", ":")).encode()

                # 凭证约每 3 次调用失效。403 时同步刷新凭证并重试，
                # 让调用方完全无感知（对外的体感就是普通 API）。
                st, data, tries = 0, b"", 0
                while tries < 3:
                    st, data = _call(nb)
                    if st != 403:
                        break
                    tries += 1
                    log({"stage": "retry", "attempt": tries})
                    try:
                        NEED.write_text("1")
                    except Exception:
                        pass
                    # 等 watchdog 把新凭证抓进来
                    t0 = time.time()
                    while time.time() - t0 < 40:
                        if not NEED.exists():
                            break
                        time.sleep(0.5)

                log({"stage": "call", "status": st, "len": len(nb),
                     "tries": tries})
                self._send(st, data)
            except Exception:
                log({"stage": "api_err", "err": traceback.format_exc()[:600]})
                self._send(500, b'{"error":"internal"}')

        def log_message(self, *a):
            pass

    def _serve():
        srv = ThreadingHTTPServer(("127.0.0.1", 4512), H)
        log({"stage": "listening", "port": 4512})
        srv.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()
    log({"stage": "server_started", "port": 4512})

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


def _trigger_client_message() -> bool:
    """用 CDP 在客户端自动发一条消息，用来刷新凭证。"""
    try:
        import websocket
        import urllib.request

        raw = urllib.request.urlopen("http://127.0.0.1:9222/json/list",
                                     timeout=8).read()
        tgt = None
        for t in json.loads(raw):
            if t.get("type") == "page" and "marvis" in (t.get("url") or ""):
                tgt = t
                break
        if not tgt:
            log("CDP: 找不到 page target")
            return False
        ws = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=30)
        n = [0]

        def call(method, **params):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == n[0]:
                    return m

        def ev(expr):
            r = call("Runtime.evaluate", expression=expr, returnByValue=True,
                     awaitPromise=True)
            return r.get("result", {}).get("result", {}).get("value")

        call("Page.bringToFront")
        # Win32 强制置前，否则点击拿不到焦点
        try:
            subprocess.run([sys.executable,
                            str(Path(__file__).resolve().parent / "win_focus.py")],
                           capture_output=True, timeout=20)
            time.sleep(0.5)
        except Exception:
            pass

        pos = ev("""(() => {
          const el = document.querySelector('.ProseMirror');
          if (!el) return '';
          const r = el.getBoundingClientRect();
          return Math.round(r.x + r.width / 2) + ',' + Math.round(r.y + r.height / 2);
        })()""")
        if not pos:
            log("CDP: 找不到输入框")
            ws.close()
            return False
        cx, cy = [int(v) for v in pos.split(",")]
        for tp in ("mousePressed", "mouseReleased"):
            call("Input.dispatchMouseEvent", type=tp, x=cx, y=cy,
                 button="left", clickCount=1)
        time.sleep(0.35)
        call("Input.insertText", text="。")
        time.sleep(0.4)
        # 注意：这条消息会进入对话上下文，可能影响后续回复。
        # 用最短的标点降低干扰。
        r = ev("""(() => {
          const b = document.querySelector('[class*="footer-action-send"]');
          if (!b) return 'no-btn';
          b.click(); return 'clicked';
        })()""")
        ws.close()
        log(f"CDP 触发刷新: {r}")
        return r == "clicked"
    except Exception:
        log("CDP 异常: " + __import__("traceback").format_exc()[:400])
        return False


def watchdog() -> None:
    """后台线程：凭证失效就自动刷新。同一时刻只允许一次刷新。"""
    busy = {"refreshing": False}
    while True:
        try:
            if NEED.exists() and not busy["refreshing"]:
                busy["refreshing"] = True
                log("检测到凭证失效，自动刷新中…")
                time.sleep(1.0)
                try:
                    if _trigger_client_message():
                        t0 = time.time()
                        while time.time() - t0 < 40:
                            if not NEED.exists():
                                log("凭证已刷新")
                                break
                            time.sleep(0.5)
                        else:
                            log("刷新超时，稍后重试")
                finally:
                    busy["refreshing"] = False
        except Exception:
            busy["refreshing"] = False
            log("watchdog 异常: " + __import__("traceback").format_exc()[:300])
        time.sleep(1.5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    a = ap.parse_args()
    if not a.start:
        ap.print_help()
        return
    LOG.write_text("", encoding="utf-8")
    try:
        if NEED.exists():
            NEED.unlink()
    except Exception:
        pass
    do_inject()
    threading.Thread(target=watchdog, daemon=True).start()
    log(f"服务地址 http://127.0.0.1:{PORT}/v1/chat/completions")
    log("watchdog 已启动（403 会自动刷新凭证）")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
