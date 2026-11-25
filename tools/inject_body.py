#!/usr/bin/env python3
"""inject_body.py — 在 MarvisAgent 进程内劫持它自己的 httpx，替换请求 body

这是决定性实验：
  官方客户端的 chat 请求走它自己的连接（经 Clash 代理），TLS、会话、
  连接状态全部真实。我们只把发出去的 JSON body 换掉。
  - 若返回 200 → 风控判定在连接/会话层，与 body 无关，
    我们就有了可编程通道（后续做 bridge）。
  - 若仍 403 → 风控确实校验 body 内容（112KB 工具定义等）。

替换源：tools/inject-body.json（存在就替换，不存在就只记录）

用法（需管理员）：
  python tools/inject_body.py --start / --run / --stop
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "body.log"
CAP = ROOT / "tools" / "body-cap.jsonl"
PIDFILE = ROOT / "tools" / "body.pid"

PYCODE = r'''
import json, pathlib, traceback
BASE = pathlib.Path(r"D:\Documents\Marvis2API-Win")
CAP = BASE / "tools" / "body-cap.jsonl"
REPL = BASE / "tools" / "inject-body.json"

def log(rec):
    try:
        with open(CAP, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False)[:30000] + "\n")
    except Exception:
        pass

try:
    import httpx

    # 支持重复注入：始终基于最初的原函数重新打补丁，
    # 这样改完脚本再注入一次就生效，不需要重启客户端。
    orig = getattr(httpx, "_marvis_orig_send", None)
    if orig is None:
        orig = httpx.AsyncClient.send
        httpx._marvis_orig_send = orig

    FULL = BASE / "tools" / "full-body.bin"

    async def send_patched(self, request, **kwargs):
        url = ""
        try:
            url = str(request.url)
        except Exception:
            pass
        is_chat = "/v1/chat/completions" in url
        try:
            if is_chat:
                org = request.content
                log({"stage": "req", "url": url, "len": len(org)})
                # 存一份完整 body，供后续原样重放（验证是否 body 决定放行）
                if len(org) > 50000:
                    try:
                        FULL.write_bytes(org)
                        log({"stage": "dumped", "len": len(org)})
                    except Exception as e:
                        log({"stage": "dump_err", "err": repr(e)[:200]})
            resp = await orig(self, request, **kwargs)
            if is_chat:
                # 流式响应不能直接读 .text，会抛 ResponseNotRead。
                # 只记状态码，足够判断放行与否。
                log({"stage": "resp", "status": resp.status_code,
                     "stream": bool(getattr(resp, "is_stream_consumed", False))
                     or resp.headers.get("content-type", "").startswith("text/event-stream")})
            return resp
        except Exception as e:
            if is_chat:
                log({"stage": "err", "err": repr(e)[:400]})
            raise

    httpx.AsyncClient.send = send_patched
    httpx._marvis_patched = True
    log({"stage": "patched", "repatched": orig is not None})
except Exception:
    log({"stage": "fatal", "err": traceback.format_exc()[:1200]})
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
        tail = CAP.read_text(encoding="utf-8", errors="replace").splitlines()[-4:]
        for ln in tail:
            log("  cap| " + ln[:260])


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
        out = open(ROOT / "tools" / "body.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
