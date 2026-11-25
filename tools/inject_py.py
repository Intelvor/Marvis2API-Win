#!/usr/bin/env python3
"""inject_py.py — 在 MarvisAgent 进程内注入 Python 代码，用它的 httpx 发请求

核心思路：
  前面所有尝试都失败，说明服务端校验的不是请求内容，而是连接本身
  （TLS 会话、进程、栈特征）。唯一能复刻的就是让请求从 MarvisAgent
  自己的 Python 环境里发出去。

做法：Frida attach MarvisAgent，通过 PyGILState_Ensure + PyRun_SimpleString
在进程内执行 Python 代码，调用它自己的 httpx 发一次请求。

用法（需管理员）：
  python tools/inject_py.py --start   # 后台
  python tools/inject_py.py --run     # 前台调试
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "inject.log"
PIDFILE = ROOT / "tools" / "inject.pid"

# 在目标进程里执行的 Python 代码：用文件做输出，避免返回值麻烦
PYCODE = r'''
import json, time, traceback
try:
    import httpx
    from pathlib import Path
    import tomllib

    cfg = tomllib.load(open(r"D:\Documents\Marvis2API-Win\config.toml","rb"))["marvis"]
    url = cfg["base_url"].rstrip("/") + cfg["chat_path"]

    # 尽量复用当前进程里已存在的客户端/凭证
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "AsyncOpenAI/Python 2.32.0",
        "X-Stainless-Lang": "python",
        "X-Stainless-Package-Version": "2.32.0",
        "X-Stainless-OS": "Windows",
        "X-Stainless-Arch": "other:amd64",
        "X-Stainless-Runtime": "CPython",
        "X-Stainless-Runtime-Version": "3.11.9",
        "X-Stainless-Async": "async:asyncio",
        "X-Stainless-Raw-Response": "true",
        "x-stainless-retry-count": "0",
        "x-stainless-read-timeout": "600.0",
        "Authorization": "Bearer placeholder",
        "Ual-Access-Openid": cfg["openid"],
        "Ual-Access-Access-Token": cfg["access_token"],
        "Ual-Access-Login-Type": cfg["login_type"],
        "Ual-Access-Guid": cfg["device_guid"],
        "X-Device-GUID": cfg["device_guid"],
        "Marvis-AgentTag": "main",
        "Marvis-Conversation-ID": "conv_" + __import__("uuid").uuid4().hex,
        "Marvis-Response-ID": "resp_" + __import__("uuid").uuid4().hex,
        "Marvis-Scene": "memory",
        "Marvis-Protocol-Version": "1000",
    }
    ext = {"qimei36": cfg["qimei36"], "osVersion": cfg["os_version"],
           "guid": cfg["device_guid"], "nonce": str(__import__("uuid").uuid4()),
           "reqScene": 1, "taskType": "user", "clientVersion": "1.60.3400.795",
           "clientPlatform": "Windows", "clientPlatformVersion": "1.60.3400.35",
           "isIoa": False}
    # uskey 交给进程内的 SDK 生成（如果可用）
    try:
        import ctypes as C
        d = C.CDLL(r"D:\Program Files\Tencent\Marvis\Application\1.60.3400.35\win32-native\beacon_wrapper.dll")
        d.beacon_init.argtypes=[]; d.beacon_init.restype=C.c_int; d.beacon_init()
        d.beacon_get_uskey.argtypes=[C.c_char_p]*3+[C.c_int]; d.beacon_get_uskey.restype=C.c_int
        b=C.create_string_buffer(8192)
        if d.beacon_get_uskey(b"marvis_client", b"{}", C.cast(b,C.c_char_p), 8192)>0:
            ext["uskey"]=b.value.decode("utf-8","replace")
    except Exception:
        ext["uskey"]="CsDVDCER"+"0"*100
    headers["Ual-Access-MarvisExt"]=json.dumps(ext,ensure_ascii=False,separators=(",",":"))

    body={"model":"main-auto","messages":[{"role":"user","content":"hi"}],"stream":False}
    t0=time.time()
    r=httpx.post(url, headers=headers, json=body, timeout=90)
    out={"status":r.status_code,"elapsed":round(time.time()-t0,2),"text":r.text[:800]}
except Exception:
    out={"error": traceback.format_exc()[:1200]}

with open(r"D:\Documents\Marvis2API-Win\tools\inject-result.json","w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
'''

JS = r"""
(function () {
  const py311 = Process.findModuleByName('python311.dll');
  if (!py311) { send({ok:false, err:'python311.dll not loaded'}); return; }
  const PyGILState_Ensure = py311.findExportByName('PyGILState_Ensure');
  const PyGILState_Release = py311.findExportByName('PyGILState_Release');
  const PyRun_SimpleString = py311.findExportByName('PyRun_SimpleString');
  if (!PyGILState_Ensure || !PyRun_SimpleString) {
    send({ok:false, err:'missing python C-API exports'}); return;
  }
  const ensure = new NativeFunction(PyGILState_Ensure, 'int', []);
  const release = new NativeFunction(PyGILState_Release, 'void', ['int']);
  const run = new NativeFunction(PyRun_SimpleString, 'int', ['pointer']);

  const code = CODE_PLACEHOLDER;
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
        log("FATAL: 未找到 MarvisAgent.exe")
        return
    proc = procs[0]
    log(f"attach {proc.name} pid={proc.pid}")

    session = dev.attach(proc.pid)
    js_code = JS.replace("CODE_PLACEHOLDER", repr(PYCODE).replace("'", '"', 1)[::1])
    # 更稳的做法：用 JSON 字符串嵌入
    js_code = JS.replace("CODE_PLACEHOLDER", '"' + PYCODE
                         .replace("\\", "\\\\").replace('"', '\\"')
                         .replace("\n", "\\n") + '"')

    script = session.create_script(js_code)
    done = {"v": False}

    def on_msg(m, data):
        log("MSG: " + str(m)[:400])
        done["v"] = True

    script.on("message", on_msg)
    script.load()
    for _ in range(40):
        if done["v"]:
            break
        time.sleep(0.5)
    time.sleep(2)
    script.unload()
    session.detach()
    log("完成，结果见 tools/inject-result.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    a = ap.parse_args()

    if a.start:
        LOG.write_text("", encoding="utf-8")
        res = ROOT / "tools" / "inject-result.json"
        if res.exists():
            res.unlink()
        out = open(ROOT / "tools" / "inject.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
