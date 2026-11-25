#!/usr/bin/env python3
"""hook_full.py — 完整抓取 MarvisAgent 的全部 TLS 明文（双向，不截断）

与 hook_chain.py 的区别：
  - 每个 EVP 调用完整落盘（最大 256KB），不截断
  - 用 EVP_CIPHER_CTX_encrypting 区分加密/解密方向（发送 vs 接收）
  - 输出 JSONL：tools/full.jsonl

用法（需管理员）：python tools/hook_full.py --start / --run / --stop
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "full.jsonl"
PIDFILE = ROOT / "tools" / "full.pid"
BOOT = ROOT / "tools" / "full.boot.log"

JS = r"""
(function () {
  const hooked = [];

  // 分块构造字符串，避免 String.fromCharCode.apply 爆栈
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

    let encFn = null;
    try {
      const e = m.findExportByName('EVP_CIPHER_CTX_encrypting');
      if (e) encFn = new NativeFunction(e, 'int', ['pointer']);
    } catch (err) {}

    const fns = [['EVP_CipherUpdate', null], ['EVP_EncryptUpdate', 'out'],
                 ['EVP_DecryptUpdate', 'in']];
    fns.forEach(function (pair) {
      const fname = pair[0], forced = pair[1];
      const addr = m.findExportByName(fname);
      if (!addr) return;
      Interceptor.attach(addr, {
        onEnter(args) {
          try {
            const inl = args[4].toInt32();
            if (inl <= 0 || inl > 262144) return;
            const u8 = new Uint8Array(args[3].readByteArray(inl));
            let pr = 0;
            for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) pr++;
            const ratio = pr / u8.length;
            if (ratio < 0.55) return;   // 密文丢弃

            let dir = forced || 'unknown';
            if (!forced && encFn) {
              try { dir = encFn(args[0]) ? 'out' : 'in'; } catch (e) {}
            }
            send({kind:'plain', fn:fname, dir:dir, len:inl,
                  ratio:ratio, txt: toStr(u8)});
          } catch (e) {}
        }
      });
      hooked.push(mn + '!' + fname);
    });
  });

  send({kind:'ready', hooked:hooked});
})();
"""


def run_fg() -> None:
    import frida

    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name.lower() == "marvisagent.exe"]
    if not procs:
        print("FATAL: no MarvisAgent.exe", flush=True)
        return
    proc = procs[0]
    print(f"attach {proc.name} pid={proc.pid}", flush=True)
    session = dev.attach(proc.pid)
    script = session.create_script(JS)

    cnt = {"n": 0, "out": 0, "in": 0}
    f = OUT.open("w", encoding="utf-8")

    def on_msg(m, data):
        if m.get("type") != "send":
            return
        p = m["payload"]
        if p.get("kind") == "ready":
            print("HOOKED: " + str(p.get("hooked")), flush=True)
            return
        cnt["n"] += 1
        d = p.get("dir", "unknown")
        if d in cnt:
            cnt[d] += 1
        txt = p.get("txt", "")
        rec = {"n": cnt["n"], "dir": d, "fn": p.get("fn"),
               "len": p.get("len"), "txt": txt}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        first = txt.splitlines()[0][:110] if txt else ""
        print(f"#{cnt['n']} {d:8} len={p.get('len'):7} {first}", flush=True)

    script.on("message", on_msg)
    script.load()
    print("监听中", flush=True)
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
        if OUT.exists():
            OUT.unlink()
        out = open(BOOT, "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run"], cwd=str(ROOT),
                             stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg()


if __name__ == "__main__":
    main()
