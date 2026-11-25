#!/usr/bin/env python3
"""hook_v3.py — 抓 /v3/* 请求的真实明文头

模型列表由 /v3/pcyyb_ai_assistant_model_gateway 提供。
之前用推算的签名发都 403，需要看客户端真实发的是什么。

hook EVP_CipherUpdate（唯一有效的明文入口），过滤 /v3/ 路径。

用法（需管理员）：python tools/hook_v3.py --start / --run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "v3.log"
OUT = ROOT / "tools" / "v3-plain.txt"
PIDFILE = ROOT / "tools" / "v3.pid"

JS = r"""
(function () {
  const hooked = [];
  function toStr(u8) {
    let s = '';
    for (let i = 0; i < u8.length; i += 4096) {
      s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + 4096, u8.length)));
    }
    return s;
  }
  ['libcrypto-3.dll', 'libcrypto-1_1-x64.dll'].forEach(function (mn) {
    const m = Process.findModuleByName(mn);
    if (!m) return;
    ['EVP_CipherUpdate', 'EVP_EncryptUpdate'].forEach(function (fn) {
      const a = m.findExportByName(fn);
      if (!a) return;
      Interceptor.attach(a, {
        onEnter(args) {
          try {
            const inl = args[4].toInt32();
            if (inl <= 0 || inl > 262144) return;
            const u8 = new Uint8Array(args[3].readByteArray(inl));
            let pr = 0;
            for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
            const ratio = pr / u8.length;
            if (ratio < 0.55) return;
            const txt = toStr(u8);
            // 只关心 /v3/ 与 model 相关
            if (txt.indexOf('/v3/') < 0 && txt.indexOf('marvis_scene') < 0
                && txt.indexOf('"model"') < 0) return;
            send({kind: 'plain', len: inl, txt: txt});
          } catch (e) {}
        }
      });
      hooked.push(mn + '!' + fn);
    });
  });
  send({kind: 'ready', hooked: hooked});
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
    script = session.create_script(JS)
    f = OUT.open("w", encoding="utf-8")
    cnt = {"n": 0}

    def on_msg(m, data):
        if m.get("type") != "send":
            return
        p = m["payload"]
        if p.get("kind") == "ready":
            log("HOOKED: " + str(p.get("hooked")))
            return
        cnt["n"] += 1
        txt = p.get("txt", "")
        f.write("\n" + "=" * 70 + f"\n# {cnt['n']} len={p.get('len')}\n")
        f.write(txt)
        f.write("\n")
        f.flush()
        first = txt.splitlines()[0][:110] if txt else ""
        log(f"#{cnt['n']} len={p.get('len')} {first}")

    script.on("message", on_msg)
    script.load()
    log("监听中")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--start", action="store_true")
    a = ap.parse_args()
    if a.start:
        LOG.write_text("", encoding="utf-8")
        out = open(ROOT / "tools" / "v3.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
