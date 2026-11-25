#!/usr/bin/env python3
"""hook_evp.py — hook libcrypto 的 EVP_EncryptUpdate，在加密前拿明文

前一轮结论：ws2_32!send 拿到的是 TLS 密文（17 03 03 = application_data），
说明 SSL_write 的加密已完成。_ssl.pyd 静态链接 OpenSSL，hook 导出符号无效。

正解：hook libcrypto-3.dll 的 EVP_EncryptUpdate —— 这是对称加密的最后入口，
入参就是明文（TLS record 的 payload 前身）。

用法：python tools/hook_evp.py --name MarvisAgent.exe --start / --stop
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tools" / "evp.log"
PIDFILE = ROOT / "tools" / "evp.pid"

JS = r"""
(function () {
  const hooked = [], info = [];
  const crypto = Process.findModuleByName('libcrypto-3.dll')
              || Process.findModuleByName('libcrypto-1_1-x64.dll');
  if (!crypto) { send({kind:'ready', hooked:[], info:['未找到 libcrypto']}); return; }
  info.push('libcrypto: ' + crypto.path);

  const fns = ['EVP_EncryptUpdate','EVP_CipherUpdate','EVP_EncryptUpdate_internal'];
  info.push('candidates: ' + fns.join(','));

  // int EVP_EncryptUpdate(EVP_CIPHER_CTX *ctx, unsigned char *out, int *outl,
  //                       const unsigned char *in, int inl);
  fns.forEach(function (fname) {
   const fn = crypto.findExportByName(fname);
   if (!fn) { info.push(fname + ': no export'); return; }
   Interceptor.attach(fn, {
    onEnter(args) {
      try {
        const inl = args[4].toInt32();
        if (inl <= 0 || inl > 1000000) return;
        const n = Math.min(inl, 6000);
        const u8 = new Uint8Array(args[3].readByteArray(n));
        let printable = 0;
        for (let i = 0; i < u8.length; i++) if (u8[i] >= 32 && u8[i] < 127) printable++;
        const ratio = printable / u8.length;
        // 只关心可打印比例高的（明文 HTTP），密文通常 < 40%
        if (ratio > 0.3) {
          send({kind:'evp', fn: fname, inl: inl, ratio: ratio,
                text: String.fromCharCode.apply(null, u8.slice(0, 3000))});
        }
      } catch (e) {}
    }
   });
   hooked.push(fname + ' @' + fn);
  });
  send({kind:'ready', hooked:hooked, info:info});
})();
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_fg(name: str) -> None:
    import frida

    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name.lower() == name.lower()]
    if not procs:
        log(f"FATAL: no {name}")
        return
    proc = procs[0]
    log(f"attach {proc.name} pid={proc.pid}")
    session = dev.attach(proc.pid)
    script = session.create_script(JS)
    n = {"c": 0}

    def on_msg(m, data):
        if m.get("type") != "send":
            return
        p = m["payload"]
        if p.get("kind") == "ready":
            log("HOOKED: " + str(p.get("hooked")))
            if p.get("info"):
                log("INFO: " + str(p.get("info")))
            return
        n["c"] += 1
        if n["c"] > 30:
            return
        txt = p.get("text", "")
        mark = " <<<" if ("HTTP/" in txt or "ual-access" in txt.lower()
                          or "POST " in txt[:10] or "GET " in txt[:10]) else ""
        log(f"EVP #{n['c']} inl={p.get('inl')} ratio={p.get('ratio'):.0%}{mark}")
        for ln in txt.splitlines()[:45]:
            log("  | " + ln[:400])

    script.on("message", on_msg)
    script.load()
    log("监听中")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="MarvisAgent.exe")
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
        out = open(ROOT / "tools" / "evp.boot.log", "wb")
        p = subprocess.Popen([sys.executable, __file__, "--run", "--name", a.name],
                             cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                             creationflags=0x00000008, stdin=subprocess.DEVNULL)
        PIDFILE.write_text(str(p.pid))
        print(f"started pid={p.pid}")
        return
    run_fg(a.name)


if __name__ == "__main__":
    main()
