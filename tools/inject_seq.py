#!/usr/bin/env python3
"""inject_seq.py — 在 MarvisAgent 进程内按真实顺序连发请求

关键假设：客户端一次对话会先发 Marvis-Scene=memory（reqScene=1），
成功后在服务端建立上下文，再发 agent_chat（reqScene=0）才被放行。
之前单独发 agent_chat 一律 4100404，正是因为缺了前置的 memory 请求。

本脚本在 MarvisAgent 进程内：
  1) 先发 memory + subScene=mem_read_rag_rewrite + reqScene=1
  2) 再发 agent_chat + reqScene=0，复用同一 conversation_id
  3) 记录两次结果，判断是否放行

用法（需管理员）：
  python tools/inject_seq.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "seq.log"
PIDFILE = ROOT / "tools" / "seq.pid"
RESULT = ROOT / "tools" / "seq-result.json"

PYCODE = r'''
import json, time, uuid, traceback
out = {"steps": []}
try:
    import tomllib, httpx, ctypes as C
    cfg = tomllib.load(open(r"D:\Documents\Marvis2API-Win\config.toml","rb"))["marvis"]
    url = cfg["base_url"].rstrip("/") + cfg["chat_path"]

    d = C.CDLL(r"D:\Program Files\Tencent\Marvis\Application\1.60.3400.35\win32-native\beacon_wrapper.dll")
    d.beacon_init.argtypes=[]; d.beacon_init.restype=C.c_int; d.beacon_init()
    d.beacon_get_uskey.argtypes=[C.c_char_p]*3+[C.c_int]; d.beacon_get_uskey.restype=C.c_int

    def uskey():
        b=C.create_string_buffer(8192)
        r=d.beacon_get_uskey(b"marvis_client", b"{}", C.cast(b,C.c_char_p), 8192)
        return b.value.decode("utf-8","replace") if r>0 else ""

    conv = "conv_" + uuid.uuid4().hex

    def send(scene, reqscene, subscene=None, tag="main"):
        rid = "resp_" + uuid.uuid4().hex
        ext = {"qimei36": cfg["qimei36"], "osVersion": cfg["os_version"],
               "guid": cfg["device_guid"], "uskey": uskey(),
               "nonce": str(uuid.uuid4()), "reqScene": reqscene,
               "taskType": "user", "clientVersion": "1.60.3400.795",
               "clientPlatform": "Windows", "clientPlatformVersion": "1.60.3400.35"}
        if subscene: ext["subScene"] = subscene
        ext["isIoa"] = False
        h = {
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "AsyncOpenAI/Python 2.32.0",
            "X-Stainless-Lang": "python", "X-Stainless-Package-Version": "2.32.0",
            "X-Stainless-OS": "Windows", "X-Stainless-Arch": "other:amd64",
            "X-Stainless-Runtime": "CPython", "X-Stainless-Runtime-Version": "3.11.9",
            "X-Stainless-Async": "async:asyncio", "X-Stainless-Raw-Response": "true",
            "x-stainless-retry-count": "0", "x-stainless-read-timeout": "600.0",
            "Authorization": "Bearer placeholder",
            "Ual-Access-Openid": cfg["openid"],
            "Ual-Access-Access-Token": cfg["access_token"],
            "Ual-Access-Login-Type": cfg["login_type"],
            "Ual-Access-Guid": cfg["device_guid"],
            "X-Device-GUID": cfg["device_guid"],
            "Ual-Access-MarvisExt": json.dumps(ext, ensure_ascii=False, separators=(",",":")),
            "Marvis-AgentTag": tag, "Marvis-Scene": scene,
            "Marvis-Conversation-ID": conv,
            "Marvis-Response-ID": rid,
            "Ual-Access-Requestid": rid,
            "Marvis-Protocol-Version": "1000",
        }
        body = {"model": "main-auto",
                "messages": [{"role":"user","content":"hi"}], "stream": False}
        t0=time.time()
        try:
            r = httpx.post(url, headers=h, json=body, timeout=90)
            step = {"scene": scene, "reqScene": reqscene, "subScene": subscene,
                    "status": r.status_code, "elapsed": round(time.time()-t0,2),
                    "text": r.text[:500]}
        except Exception:
            step = {"scene": scene, "error": traceback.format_exc()[:500]}
        out["steps"].append(step)
        return step

    # 按客户端真实顺序
    send("memory", 1, "mem_read_rag_rewrite")
    time.sleep(0.3)
    send("agent_chat", 0)
except Exception:
    out["fatal"] = traceback.format_exc()[:1500]

with open(r"D:\Documents\Marvis2API-Win\tools\seq-result.json","w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
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
  send({ok:true, rc:rc});
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
    js_code = JS_TMPL.replace("__CODE__", '"' + escaped + '"')

    script = session.create_script(js_code)
    done = {"v": False}

    def on_msg(m, data):
        log("MSG: " + str(m)[:300])
        done["v"] = True

    script.on("message", on_msg)
    script.load()
    for _ in range(60):
        if done["v"]:
            break
        time.sleep(0.5)
    time.sleep(3)
    try:
        script.unload()
    except Exception:
        pass
    session.detach()
    log("完成")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    a = ap.parse_args()

    if a.start:
        LOG.write_text("", encoding="utf-8")
        if RESULT.exists():
            RESULT.unlink()
        out = open(ROOT / "tools" / "seq.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
