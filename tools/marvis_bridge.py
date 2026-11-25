#!/usr/bin/env python3
"""marvis_bridge.py — 端到端桥接：外部 OpenAI 请求 → 客户端搭载 → 模型回复

已确证的两条可用链路：
  A. 请求体就地替换（tools/inject_body.py）：改客户端手里的 Request 得 200
  B. CDP 驱动客户端发消息（tools/cdp_input.py）：tiptap 编辑器 + send 按钮

本脚本把两者接起来：
  1. Frida 注入 MarvisAgent，劫持 httpx.AsyncClient.send
     - 有任务时：替换 body 为任务内容，并记录响应流
     - 无任务时：正常放行
  2. 本地 HTTP 服务（默认 127.0.0.1:4605 之外的新端口 4895）提供 OpenAI 接口
     - 收到请求 → 存入任务槽 → 用 CDP 触发客户端发一条消息
     - 客户端的请求被就地换体 → 上游返回 → 抓取响应 → 回传给调用方

注意：必须带 CDP 端口启动客户端。启动方式见 tools/start-cdp-admin.ps1。

用法（需管理员注入 + 普通权限跑服务）：
  python tools/marvis_bridge.py --inject     # 注入（需管理员）
  python tools/marvis_bridge.py --serve      # 起服务
  python tools/marvis_bridge.py --all        # 两步一起（注入需提权）
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
LOG = ROOT / "tools" / "bridge2.log"
CAP = ROOT / "tools" / "bridge2-cap.jsonl"
SLOT = ROOT / "tools" / "bridge2-slot.json"
RESP = ROOT / "tools" / "bridge2-resp.json"
PIDFILE = ROOT / "tools" / "bridge2.pid"
PORT = 4895

PYCODE = r'''
import json, pathlib, traceback
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "bridge2-cap.jsonl"
SLOT = BASE / "tools" / "bridge2-slot.json"
RESP = BASE / "tools" / "bridge2-resp.json"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:60000] + "\n")
    except Exception:
        pass

try:
    import httpx

    # 精确复刻已实证可用的 inject_body 路径：
    # 在客户端自己这一次 send() 调用内，把 request 换成新对象再交给原函数。
    # 关键点：headers 里必须 pop 掉 content-length，让 httpx 按新 body 重算
    # （手动塞会撞上 "Too much data for declared Content-Length"）。
    # _marvis_orig_send 只在**第一次**注入时记录真正的原生 send。
    # 后续重注入若直接复用，可能复用到被污染的包装函数（递归）。
    # 用 __closure__ 判定：原生 send 无闭包，包装函数有。
    orig = getattr(httpx, "_marvis_orig_send", None)
    if orig is None or getattr(orig, "__closure__", None) is not None:
        # 当前保存的不是原生函数 —— 说明被污染了。
        # 从类 dict 里找不是我们自己造的那个（名字为 send 且无闭包）。
        cand = None
        cur = httpx.AsyncClient.send
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
        log({"stage": "orig_recaptured",
             "is_native": getattr(orig, "__closure__", None) is None})
    else:
        log({"stage": "orig_reused", "is_native": True})

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        if "/v1/chat/completions" not in url:
            return await orig(self, request, **kwargs)

        job = None
        try:
            if SLOT.exists():
                job = json.loads(SLOT.read_text(encoding="utf-8"))
        except Exception:
            job = None

        if job and job.get("pending"):
            # 关键结论：把 body 换成极简体（200 字节）会被风控拒绝，
            # 保留完整结构（system + tools + 参数）就放行。
            # 所以做法是：以客户端当次真实 body 为模板，只替换
            # 最后一条 user 消息和末尾的 query。
            try:
                scene = request.headers.get("marvis-scene", "?")
                job2 = dict(job)
                job2["seen"] = list(job.get("seen", [])) + [scene]
                if scene and scene != "memory":
                    job2["pending"] = False
                SLOT.write_text(json.dumps(job2, ensure_ascii=False),
                                encoding="utf-8")
            except Exception:
                scene = "?"
            try:
                raw = request.content
                try:
                    tpl = json.loads(raw)
                except Exception:
                    tpl = None
                if not isinstance(tpl, dict) or "messages" not in tpl:
                    # 解析不了就原样放行，避免打坏客户端
                    log({"stage": "skip_unparsable", "scene": scene,
                         "len": len(raw)})
                    return await orig(self, request, **kwargs)

                want = job["body"].get("messages") or []
                new_msgs = list(tpl.get("messages") or [])
                if want:
                    # 保留 system 与历史，只把最后一条 user 换成我们的内容
                    pure = [m for m in want if m.get("role") != "system"]
                    if pure:
                        if new_msgs and new_msgs[-1].get("role") == "user":
                            new_msgs[-1] = {"role": "user",
                                            "content": pure[-1]["content"]}
                        else:
                            new_msgs.append({"role": "user",
                                             "content": pure[-1]["content"]})
                tpl["messages"] = new_msgs
                if "thinking_enabled" in tpl:
                    tpl["thinking_enabled"] = True
                nb = json.dumps(tpl, ensure_ascii=False,
                                separators=(",", ":")).encode()
                h = dict(request.headers)
                h.pop("content-length", None)
                h.pop("Content-Length", None)
                newreq = httpx.Request(request.method, request.url,
                                       headers=h, content=nb)
                log({"stage": "swapped", "scene": scene,
                     "orig": len(raw), "len": len(nb),
                     "job": job.get("tag")})
                resp = await orig(self, newreq)
                text = ""
                try:
                    if resp.status_code == 200:
                        raw = await resp.aread()
                        text = raw.decode("utf-8", "replace")
                    else:
                        raw = await resp.aread()
                        text = raw.decode("utf-8", "replace")
                except Exception:
                    text = "<read failed: %s>" % traceback.format_exc()[:300]
                try:
                    RESP.write_text(json.dumps(
                        {"status": resp.status_code, "text": text[:200000],
                         "tag": job.get("tag")}, ensure_ascii=False),
                        encoding="utf-8")
                except Exception:
                    pass
                log({"stage": "resp", "status": resp.status_code,
                     "len": len(text), "tag": job.get("tag")})
                return resp
            except Exception:
                log({"stage": "swap_err", "err": traceback.format_exc()[:800]})
                raise

        # 没有任务时正常放行，并把响应记录一下（便于对照）
        resp = await orig(self, request, **kwargs)
        log({"stage": "passthru", "status": resp.status_code,
             "len": len(request.content),
             "scene": request.headers.get("marvis-scene", "?")})
        return resp

    httpx.AsyncClient.send = send_patched
    log({"stage": "patched", "slot": str(SLOT)})
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
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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


def _cdp_send_text(text: str) -> bool:
    """用 CDP 模拟真实鼠标点击输入框 + Input.insertText + 点发送。

    要点（都是实测踩出来的）：
      1. el.focus() 在 Electron 里不生效，activeElement 仍是 BODY。
         必须用 Input.dispatchMouseEvent 在输入框中心点一下。
      2. ProseMirror 不吃 textContent / execCommand（execCommand 返回 false）。
         用 CDP 的 Input.insertText，走浏览器原生输入通道。
      3. 发送按钮 class 含 semi-aiChatInput-footer-action-send。
    """
    try:
        import websocket
    except Exception:
        log("缺 websocket-client，无法 CDP 触发")
        return False
    try:
        import urllib.request

        raw = urllib.request.urlopen("http://127.0.0.1:9222/json/list",
                                     timeout=8).read()
        tgt = None
        for t in json.loads(raw):
            if t.get("type") == "page" and "marvis" in (t.get("url") or ""):
                tgt = t
                break
        if not tgt:
            log("找不到 page target")
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

        # Electron 的 bringToFront 只切 tab 可见性，不抢系统焦点，
        # 导致鼠标点击后 activeElement 仍是 BODY。
        # 必须用 Win32 强制置前（topmost 抖动可绕过前台锁）。
        try:
            import subprocess as _sp
            _sp.run([sys.executable,
                     str(Path(__file__).resolve().parent / "win_focus.py")],
                    capture_output=True, timeout=20)
            time.sleep(0.5)
        except Exception:
            pass

        # 1) 真实鼠标点击输入框中心，拿到焦点
        pos = ev("""(() => {
          const el = document.querySelector('.ProseMirror');
          if (!el) return '';
          const r = el.getBoundingClientRect();
          return Math.round(r.x + r.width / 2) + ',' + Math.round(r.y + r.height / 2);
        })()""")
        if not pos:
            log("找不到 .ProseMirror")
            ws.close()
            return False
        cx, cy = [int(v) for v in pos.split(",")]
        for tp in ("mousePressed", "mouseReleased"):
            call("Input.dispatchMouseEvent", type=tp, x=cx, y=cy,
                 button="left", clickCount=1)
        time.sleep(0.35)

        focused = ev("""(() => {
          const el = document.querySelector('.ProseMirror');
          return (document.activeElement === el) ? 'ok' : 'not-focused';
        })()""")

        # 2) 原生输入通道写入文本
        call("Input.insertText", text=text)
        time.sleep(0.4)
        got = ev("document.querySelector('.ProseMirror').innerText || ''")

        # 3) 点发送
        sent = ev("""(() => {
          const b = document.querySelector('[class*="footer-action-send"]');
          if (!b) return 'no-btn';
          b.click();
          return 'clicked';
        })()""")
        ws.close()
        log(f"CDP 触发：focus={focused} textlen={len(got or '')} send={sent}")
        return (got or "") != ""
    except Exception:
        log("CDP 异常：" + __import__("traceback").format_exc()[:400])
        return False


def run_job(body: dict, tag: str, timeout: float = 180.0) -> dict:
    """把任务放进槽，用 CDP 触发搭载，等结果。

    注：现在已有 inject_free 的自由调用通道（复用客户端当前有效的
    response-id，无需 CDP 触发）。本函数作为回退方案保留。
    """
    if RESP.exists():
        RESP.unlink()
    SLOT.write_text(json.dumps({"pending": True, "tag": tag, "body": body},
                               ensure_ascii=False), encoding="utf-8")
    if not _cdp_send_text("你好"):
        return {"error": "cdp trigger failed"}
    t0 = time.time()
    while time.time() - t0 < timeout:
        if RESP.exists():
            try:
                r = json.loads(RESP.read_text(encoding="utf-8"))
                if r.get("tag") == tag:
                    try:
                        SLOT.unlink()
                    except Exception:
                        pass
                    return r
            except Exception:
                pass
        time.sleep(0.3)
    return {"error": "timeout"}


def extract_text(raw: str) -> str:
    """从 SSE 流里取出 assistant 文本。"""
    parts = []
    for ln in raw.splitlines():
        if not ln.startswith("data:"):
            continue
        d = ln[5:].strip()
        if not d or d == "[DONE]":
            continue
        try:
            j = json.loads(d)
        except Exception:
            continue
        for ch in (j.get("choices") or []):
            c = (ch.get("delta") or {}).get("content")
            if c:
                parts.append(c)
    return "".join(parts)


def do_serve() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, st, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(st)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"ok": True, "port": PORT})

        def do_POST(self):
            try:
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                req = json.loads(raw)
            except Exception:
                self._send(400, {"error": "bad json"})
                return

            msgs = req.get("messages") or []
            body = {
                "model": req.get("model") or "main-auto",
                "messages": msgs,
                "max_tokens": int(req.get("max_tokens") or 32000),
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": float(req.get("temperature", 1)),
                "thinking_enabled": True,
            }
            tag = "t%d" % int(time.time() * 1000)
            r = run_job(body, tag)
            if "error" in r:
                self._send(502, {"error": r["error"]})
                return
            text = extract_text(r.get("text", ""))
            self._send(200, {
                "id": "chatcmpl-" + tag,
                "object": "chat.completion",
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0},
            })

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    log(f"服务已起：http://127.0.0.1:{PORT}/v1/chat/completions")
    srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--test", help="直接跑一次任务，不启服务")
    a = ap.parse_args()

    if a.inject:
        do_inject()
    elif a.serve:
        do_serve()
    elif a.all:
        LOG.write_text("", encoding="utf-8")
        do_inject()
        P = subprocess.Popen([sys.executable, __file__, "--serve"], cwd=str(ROOT),
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(P.pid))
        print(f"服务 pid={P.pid}")
    elif a.test:
        body = {"model": "main-auto",
                "messages": [{"role": "user", "content": a.test}],
                "max_tokens": 32000, "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": 1, "thinking_enabled": True}
        r = run_job(body, "test")
        print(json.dumps(r, ensure_ascii=False)[:400])
        if "text" in r:
            print("--- 提取文本 ---")
            print(extract_text(r["text"])[:600])
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
